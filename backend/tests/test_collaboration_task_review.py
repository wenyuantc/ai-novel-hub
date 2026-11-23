"""个人任务审查：输出投影、认证竞争和不可复用的任务身份。"""

import asyncio
import json
from uuid import uuid4

import pytest

from app.models import Task, User
from app.services import personal_projection, task_service
from tests.helpers import create_ai_config, register
from tests.test_collaboration_task_privacy import _setup, _tasks


async def test_task_output_and_first_stream_snapshot_project_private_json(app, client, make_client):
    guest = make_client()
    owner, user, project, member = await _setup(app, client, guest)
    config = await create_ai_config(client)
    tasks = await _tasks(app, owner['id'], user['id'], project['id'], member['invitation_id'], member['revision'])
    task_id, task_key = tasks['valid']
    original = json.dumps({'ai_config_id': config['id'], 'task_id': tasks['owner'][0],
                           '_project_model': {'config_id': config['id']}, 'ordinary': '正文'})
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task.output_text = original
        await db.commit()
    response = await guest.get(f'/api/v1/tasks/{task_id}/output', params={'task_key': task_key})
    assert response.status_code == 200, response.text
    expected = {'ai_config_id': None, 'task_id': None, 'ordinary': '正文'}
    assert json.loads(response.json()['text']) == expected
    engine = app.state.task_engine
    engine.ensure_hub(task_id, statistics_key=task_key, status='running', text=original)
    stream = engine.subscribe(task_id, statistics_key=task_key, actor_id=user['id'], token_version=0,
                              active_grant_required=True)
    try:
        event, payload = await anext(stream)
        assert event == 'snapshot'
        assert json.loads(payload['text']) == expected
    finally:
        await stream.aclose()


async def test_personal_task_cannot_cancel_after_original_token_is_revoked(app, client, monkeypatch):
    await app.state.task_engine.stop()
    user = await register(client, 'task-token-owner')
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user['id'], kind='book.import', status='queued')
        db.add(task)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key
    reached, release = asyncio.Event(), asyncio.Event()
    original = task_service.get_owned_task

    async def gated(*args, **kwargs):
        result = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(task_service, 'get_owned_task', gated)
    request = asyncio.create_task(client.post(f'/api/v1/tasks/{task_id}/cancel', params={'task_key': task_key}))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with app.state.sessionmaker() as db:
            (await db.get(User, user['id'])).token_version += 1
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 401, response.text
        async with app.state.sessionmaker() as db:
            actual = await db.get(Task, task_id)
            assert actual.statistics_key == task_key and actual.status == 'queued'
    finally:
        release.set()
        if not request.done():
            request.cancel()


async def test_cancel_response_does_not_adopt_reused_task_id(app, client, make_client, monkeypatch):
    guest = make_client()
    owner, user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner['id'], user['id'], project['id'], member['invitation_id'], member['revision'])
    task_id, old_key = tasks['valid']
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        # 排队章节任务缺原 _chapter_claim 时取消按设计 409 失败关闭；本用例只验证同号复用，改用无资源占用的类型。
        row.status, row.kind = 'queued', 'outline.continue'
        await db.commit()
    original = personal_projection.project_payload
    new_key = str(uuid4())
    swapped = False

    async def replace_after_projection(*args, **kwargs):
        nonlocal swapped
        result = await original(*args, **kwargs)
        if not swapped:
            swapped = True
            async with app.state.sessionmaker() as db:
                prior = await db.get(Task, task_id)
                await db.delete(prior)
                await db.flush()
                db.add(Task(id=task_id, statistics_key=new_key, user_id=user['id'], project_id=project['id'],
                            kind='chapter.generate', status='succeeded', title='同号新任务',
                            input={'_collaboration_grant': {'invitation_id': member['invitation_id'],
                                                            'revision': member['revision']}}))
                await db.commit()
        return result

    monkeypatch.setattr(personal_projection, 'project_payload', replace_after_projection)
    response = await guest.post(f'/api/v1/tasks/{task_id}/cancel', params={'task_key': old_key})
    assert response.status_code == 404, response.text
    async with app.state.sessionmaker() as db:
        replacement = await db.get(Task, task_id)
        assert replacement.statistics_key == new_key != old_key and replacement.status == 'succeeded'


async def test_task_without_stable_uuid_is_hidden_until_repaired(app, client, make_client):
    from sqlalchemy import update

    guest = make_client()
    owner, user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner['id'], user['id'], project['id'], member['invitation_id'], member['revision'])
    task_id = tasks['finished'][0]
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=None))
        await db.commit()
    params = {'task_key': tasks['finished'][1]}
    assert (await guest.get(f'/api/v1/tasks/{task_id}', params=params)).status_code == 404
    assert (await guest.get(f'/api/v1/tasks/{task_id}/output', params=params)).status_code == 404
    assert task_id not in [item['id'] for item in (await guest.get('/api/v1/tasks')).json()]


@pytest.mark.parametrize('change', ['project', 'execution_round'])
async def test_stream_does_not_switch_project_or_execution_round(app, client, make_client, change):
    from datetime import timedelta

    from tests.helpers import create_project

    guest = make_client()
    owner, user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner['id'], user['id'], project['id'], member['invitation_id'], member['revision'])
    task_id, task_key = tasks['valid']
    other = await create_project(guest, title='本人另一书') if change == 'project' else None
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task_id, statistics_key=task_key, status='running')
    stream = engine.subscribe(task_id, statistics_key=task_key, actor_id=user['id'], token_version=0,
                              active_grant_required=True)
    try:
        assert (await anext(stream))[0] == 'snapshot'
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, task_id)
            if other is not None:
                task.project_id = other['id']
            else:
                task.queued_at += timedelta(seconds=1)
            await db.commit()
        hub.append('原项目或执行轮次的内容')
        event, payload = await anext(stream)
        assert event == 'error' and payload['code'] == 'permission_denied'
        assert '原项目或执行轮次的内容' not in str(payload)
    finally:
        await stream.aclose()


async def test_bulk_clear_personal_tasks_rechecks_token_before_write(app, client, monkeypatch):
    await app.state.task_engine.stop()
    user = await register(client, 'task-clear-token')
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user['id'], kind='book.import', status='succeeded')
        db.add(task)
        await db.commit()
        task_id = task.id

    reached, release = asyncio.Event(), asyncio.Event()
    original = task_service._lock_accessible_history_projects

    async def blocked(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(task_service, '_lock_accessible_history_projects', blocked)
    request = asyncio.create_task(client.delete('/api/v1/tasks', params={'finished': True}))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with app.state.sessionmaker() as db:
            (await db.get(User, user['id'])).token_version += 1
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 401, response.text
        async with app.state.sessionmaker() as db:
            assert await db.get(Task, task_id) is not None
    finally:
        release.set()
        if not request.done():
            request.cancel()


async def test_personal_task_list_reports_revoked_token_after_initial_query(app, client, monkeypatch):
    await app.state.task_engine.stop()
    user = await register(client, 'task-list-token')
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=user['id'], kind='book.import', status='queued', title='不得在旧令牌下输出'))
        await db.commit()
    reached, release = asyncio.Event(), asyncio.Event()
    original = task_service._queued_ids

    async def blocked(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(task_service, '_queued_ids', blocked)
    request = asyncio.create_task(client.get('/api/v1/tasks'))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with app.state.sessionmaker() as db:
            (await db.get(User, user['id'])).token_version += 1
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 401 and '不得在旧令牌下输出' not in response.text
    finally:
        release.set()
        if not request.done():
            request.cancel()


async def test_bulk_clear_denial_does_not_flush_unrelated_pending_writes(app, client):
    from sqlalchemy import event

    from app.core.errors import UnauthorizedError
    from app.models import Chapter
    from app.services.authorization_service import AuthenticatedActor
    from tests.helpers import create_project

    user = await register(client, 'task-clear-pending')
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=user['id'], kind='book.import', status='succeeded'))
        await db.commit()
        db.info['authenticated_actor'] = AuthenticatedActor(user['id'], 999)
        pending = Chapter(project_id=project['id'], title='不能提前写入')
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, 'before_flush', lambda *_: flushes.append(True))
        with pytest.raises(UnauthorizedError):
            await task_service.clear_finished(db, user['id'])
        assert not flushes and pending.id is None
        await db.rollback()


@pytest.mark.parametrize('with_project', [False, True])
async def test_cancel_does_not_target_resumed_round(app, client, monkeypatch, with_project):
    from app.services.task_engine.registry import get_handler
    from tests.helpers import create_project

    await app.state.task_engine.stop()
    user = await register(client, 'task-cancel-round')
    project = await create_project(client) if with_project else None
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user['id'], project_id=project['id'] if project else None,
                    kind='book.import', status='paused')
        db.add(task)
        await db.commit()
        task_id, task_key, initial_round = task.id, task.statistics_key, task.queued_at
    assert initial_round is not None
    reached, release = asyncio.Event(), asyncio.Event()
    original = task_service.get_owned_task

    blocked_once = False

    async def blocked(*args, **kwargs):
        nonlocal blocked_once
        task = await original(*args, **kwargs)
        if not blocked_once:
            blocked_once = True
            reached.set()
            await release.wait()
        return task

    async def no_abort(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_service, 'get_owned_task', blocked)
    # 此用例只验证任务控制并发；拆书来源及模型参数在单独业务测试中验证。
    monkeypatch.setattr(get_handler('book.import'), 'revalidate_resume', no_abort)
    request = asyncio.create_task(client.post(f'/api/v1/tasks/{task_id}/cancel', params={'task_key': task_key}))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        resumed = await client.post(f'/api/v1/tasks/{task_id}/resume', params={'task_key': task_key})
        assert resumed.status_code == 202, resumed.text
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 409 and response.json()['code'] == 'task_round_changed', response.text
        async with app.state.sessionmaker() as db:
            resumed = await db.get(Task, task_id)
            assert resumed.statistics_key == task_key and resumed.status == 'queued'
            assert resumed.queued_at != initial_round
    finally:
        release.set()
        if not request.done():
            request.cancel()


async def test_running_cancel_binds_task_uuid_and_queued_round(app):
    from datetime import UTC, datetime, timedelta

    engine = app.state.task_engine
    await engine.stop()
    execution = asyncio.create_task(asyncio.Event().wait())
    task_id, task_key = 12345, str(uuid4())
    current_round = datetime.now(UTC)
    engine._running[task_id] = execution
    engine._running_statistics_keys[task_id] = task_key
    engine._running_rounds[task_id] = current_round
    try:
        await engine.cancel_running(
            task_id, statistics_key=task_key, expected_queued_at=current_round - timedelta(seconds=1),
        )
        assert not execution.done()
        await engine.cancel_running(task_id, statistics_key=task_key, expected_queued_at=current_round)
        assert execution.cancelled()
    finally:
        engine._running.pop(task_id, None)
        engine._running_statistics_keys.pop(task_id, None)
        engine._running_rounds.pop(task_id, None)
        engine._cancel_requested.discard(task_id)
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
