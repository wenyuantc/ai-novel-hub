"""本人在 B 取消 A 的执行轮次；两个应用仅共享数据库。"""

import asyncio

import pytest
from sqlalchemy import select

from app.models import Chapter, GenerationLog, Task, TaskStatistic
from tests.conftest import _new_client
from tests.helpers import Gate, task_params
from tests.schema_helpers import isolated_worker_database
from tests.test_tasks import _generate, _setup


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


@pytest.mark.parametrize("kind", [
    "chapter.generate", "chapter.analyze", "wizard.world", "wizard.characters", "wizard.outline", "wizard.careers",
])
async def test_peer_cancel_stops_original_model(app, second_app, client, fake_llm, kind):
    _, base, (chapter,) = await _setup(client, content="原稿")
    gate = Gate()
    fake_llm.push([gate, "必须由原执行者停止"])
    if kind == "chapter.generate":
        response = await _generate(client, chapter)
    elif kind == "chapter.analyze":
        response = await client.post(f"/api/v1/chapters/{chapter['id']}/analyze")
    else:
        response = await client.post(f"{base}/wizard/{kind.split('.')[1]}", json={})
    assert response.status_code == 202, response.text
    ref = response.json()
    try:
        await gate.wait_reached()
        await second_app.state.task_engine.start()
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            r = await peer.post(f"/api/v1/tasks/{ref['id']}/cancel", params=task_params(ref))
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "cancelled", r.text
        assert fake_llm.aborted == 1
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, ref["id"])
            assert task.cancel_requested_at is None and task.execution_key is None
            assert (await db.get(TaskStatistic, ref["task_key"])).status == "cancelled"
            if kind == "chapter.generate":
                saved = await db.get(Chapter, chapter["id"])
                assert (saved.content, saved.status, saved.generation_claim_key) == ("原稿", "draft", None)
                logs = list(await db.scalars(select(GenerationLog.status).where(GenerationLog.task_id == ref["id"])))
                assert logs == ["cancelled"]
    finally:
        gate.release()


@pytest.mark.parametrize("lost_worker", [False, True])
async def test_timed_out_request_survives_and_is_consumed_by_owner_or_adopter(
    app, second_app, client, fake_llm, monkeypatch, lost_worker,
):
    from app.services.task_engine import owner_cancellation
    from tests.helpers import wait_task, wait_until
    from tests.worker_helpers import expire_worker, freeze_heartbeat

    source, observer = app.state.task_engine, second_app.state.task_engine
    _, _, (chapter,) = await _setup(client, content="原稿")
    gate, enabled = Gate(), asyncio.Event()
    scan = owner_cancellation.scan

    async def delayed_delivery(engine):
        if engine is not source or enabled.is_set():
            await scan(engine)

    monkeypatch.setattr(owner_cancellation, "scan", delayed_delivery)
    fake_llm.push([gate, "迟到的输出"])
    response = await _generate(client, chapter)
    ref = response.json()
    try:
        await gate.wait_reached()
        await observer.start()
        observer.cancel_wait_seconds = 0.05
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            stamp = None
            for _ in range(2):
                response = await peer.post(f"/api/v1/tasks/{ref['id']}/cancel", params=task_params(ref))
                assert response.status_code == 200 and response.json()["status"] == "running", response.text
                async with second_app.state.sessionmaker() as db:
                    row = await db.get(Task, ref["id"])
                    assert row.cancel_requested_at is not None
                    if stamp is not None:
                        assert stamp == row.cancel_requested_at
                    stamp = row.cancel_requested_at
            assert not gate.released.is_set() and not fake_llm.aborted
            if lost_worker:
                await freeze_heartbeat(source)
                await expire_worker(source)
                await observer.recover()
            else:
                enabled.set()
                source.notify()
            done = await wait_task(peer, ref)
            assert done["status"] == "cancelled", done
    finally:
        gate.release()
    await wait_until(lambda: ref["id"] not in source._running)
    async with second_app.state.sessionmaker() as db:
        row = await db.get(Task, ref["id"])
        assert (row.status, row.execution_key, row.cancel_requested_at) == ("cancelled", None, None)
        assert (await db.get(TaskStatistic, ref["task_key"])).status == "cancelled"
        saved = await db.get(Chapter, chapter["id"])
        assert (saved.content, saved.generation_claim_key) == ("原稿", None)


@pytest.mark.parametrize("cleanup_confirmed", [True, False])
async def test_cancel_committed_after_orphan_cleanup_is_seen_by_terminal_transaction(
    app, second_app, client, monkeypatch, cleanup_confirmed,
):
    """C1 已接管并清理，取消才落库；终态按锁内事实写入，不能再做一次回退。"""
    from uuid import uuid4

    from app.core.errors import ConflictError
    from app.services import task_statistics_service
    from app.services.task_engine.registry import get_handler
    from tests.helpers import register

    user = await register(client)
    engine, peer_engine = app.state.task_engine, second_app.state.task_engine
    await engine.stop()
    await engine.lease.register()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], kind="outline.continue", status="running", execution_key=str(uuid4()))
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        task_id, key, execution = task.id, task.statistics_key, task.execution_key
    reached, release = asyncio.Event(), asyncio.Event()
    aborts = []
    handler = get_handler("outline.continue")
    original_abort, complete = handler.on_abort, engine.complete_state

    async def observed_abort(ctx, row, **options):
        aborts.append(row.statistics_key)
        if not cleanup_confirmed:
            raise ConflictError("原资源身份无法核对")
        return await original_abort(ctx, row, **options)

    async def hold(row, **options):
        if row.id == task_id and options["expected_status"] == "running":
            reached.set()
            await release.wait()
        return await complete(row, **options)

    monkeypatch.setitem(vars(handler), "on_abort", observed_abort)
    monkeypatch.setattr(engine, "complete_state", hold)
    peer_engine.cancel_wait_seconds = 0.05
    recovery = asyncio.create_task(engine.recover())
    try:
        await asyncio.wait_for(reached.wait(), 8)
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            response = await peer.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": key})
            assert response.status_code == 200 and response.json()["status"] == "running"
        async with second_app.state.sessionmaker() as db:
            pending = await db.get(Task, task_id)
            assert pending.worker_key == engine.lease.worker_key and pending.cancel_requested_at is not None
    finally:
        release.set()
        await asyncio.wait_for(recovery, 8)
    async with app.state.sessionmaker() as db:
        settled = await db.get(Task, task_id)
        expected = ("cancelled", None, None) if cleanup_confirmed else ("interrupted", None, execution)
        assert (settled.status, settled.cancel_requested_at, settled.execution_key) == expected
        assert (await db.get(TaskStatistic, key)).status == expected[0]
    assert aborts == [key]


async def test_peer_sees_unproven_exit_only_for_that_execution(app, second_app, client, fake_llm, monkeypatch):
    from app.services import generation_service
    from app.services.task_engine.registry import get_handler
    from tests.helpers import wait_until

    _, _, (first, other) = await _setup(client, chapters=2)
    handler = get_handler("chapter.generate")
    original = handler.build_job
    exited = asyncio.Event()

    async def uncertain(ctx, row, **options):
        if row.chapter_id == first["id"]:
            exited.set()
            raise generation_service.ChapterCommitUncertain("结果待核对")
        return await original(ctx, row, **options)

    monkeypatch.setitem(vars(handler), "build_job", uncertain)
    ref = (await _generate(client, first)).json()
    await asyncio.wait_for(exited.wait(), 5)
    owner = app.state.task_engine
    await wait_until(lambda: ref["id"] not in owner._running)
    other_gate = Gate()
    fake_llm.push([other_gate, "另一章"])
    other_ref = (await _generate(client, other)).json()
    try:
        await other_gate.wait_reached()
        await second_app.state.task_engine.start()
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            unknown = await peer.post(f"/api/v1/tasks/{ref['id']}/cancel", params=task_params(ref))
            assert unknown.status_code == 409, unknown.text
            cancelled = await peer.post(f"/api/v1/tasks/{other_ref['id']}/cancel", params=task_params(other_ref))
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
        async with second_app.state.sessionmaker() as db:
            kept = await db.get(Task, ref["id"])
            assert kept.status == "running" and kept.execution_key and kept.cancel_requested_at is None
    finally:
        other_gate.release()


@pytest.mark.parametrize("committed", [False, True], ids=["write-lost", "reply-lost"])
async def test_local_signal_requires_a_committed_request(app, client, fake_llm, monkeypatch, committed):
    from app.services import task_service
    from tests.helpers import wait_task

    _, _, (chapter,) = await _setup(client)
    gate = Gate()
    fake_llm.push([gate, "若请求未提交应能保存"])
    ref = (await _generate(client, chapter)).json()
    try:
        await gate.wait_reached()
        async with app.state.sessionmaker() as db:
            user_id = await db.scalar(select(Task.user_id).where(Task.id == ref["id"]))
            commit = db.commit

            async def fail_commit():
                if committed:
                    await commit()
                raise ConnectionError("取消请求的提交回执丢失")

            monkeypatch.setattr(db, "commit", fail_commit)
            cancel = task_service.cancel_task(db, app.state.task_engine, user_id, ref["id"],
                                              expected_task_key=ref["task_key"])
            if committed:
                assert (await cancel).status == "cancelled"
            else:
                with pytest.raises(ConnectionError):
                    await cancel
        assert fake_llm.aborted == int(committed)
    finally:
        gate.release()
    expected = "cancelled" if committed else "succeeded"
    assert (await wait_task(client, ref))["status"] == expected


async def test_http_worker_finishes_only_requested_orphan_after_disconnect(app, client, fake_llm, monkeypatch):
    from uuid import uuid4

    from app.services import task_statistics_service
    from tests.helpers import register, wait_task

    actor = await register(client)
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker.begin() as db:
        rows = [Task(user_id=actor["id"], kind="outline.continue", status="running",
                     execution_key=str(uuid4())) for _ in range(2)]
        for row in rows:
            db.add(row)
            await task_statistics_service.record_created(db, row)
    target, other = rows
    gate = Gate()
    recover = engine._recover_adopted

    async def hold(task):
        assert task.statistics_key == target.statistics_key
        gate.reached.set()
        await gate.released.wait()
        return await recover(task)

    monkeypatch.setattr(engine, "_recover_adopted", hold)
    ref = {"id": target.id, "task_key": target.statistics_key}
    request = asyncio.create_task(client.post(f"/api/v1/tasks/{target.id}/cancel", params=task_params(ref)))
    try:
        await gate.wait_reached()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        async with app.state.sessionmaker() as db:
            owned = await db.get(Task, target.id)
            assert owned.worker_key == engine.request_worker.worker_key and owned.cancel_requested_at is not None
    finally:
        gate.release()
        await asyncio.gather(request, return_exceptions=True)
    assert (await wait_task(client, ref))["status"] == "cancelled"
    async with app.state.sessionmaker() as db:
        untouched = await db.get(Task, other.id)
        assert (untouched.status, untouched.worker_key, untouched.cancel_requested_at) == ("running", None, None)
    assert not fake_llm.calls


async def test_http_worker_must_wait_for_original_lease_before_taking_over(
    app, second_app, client, fake_llm, monkeypatch,
):
    from app.services.task_engine import owner_cancellation
    from tests.helpers import wait_until
    from tests.worker_helpers import expire_worker, freeze_heartbeat

    source, receiver = app.state.task_engine, second_app.state.task_engine
    assert receiver._loop_task is None
    _, _, (chapter,) = await _setup(client, content="原稿")
    model = Gate()
    fake_llm.push([model, "不能越过围栏保存"])
    original_scan = owner_cancellation.scan

    async def delay_source(engine):
        if engine is not source:
            await original_scan(engine)

    monkeypatch.setattr(owner_cancellation, "scan", delay_source)
    ref = (await _generate(client, chapter)).json()
    url = f"/api/v1/tasks/{ref['id']}/cancel"
    try:
        await model.wait_reached()
        async with _new_client(second_app) as peer:
            peer.cookies.update(client.cookies)
            receiver.cancel_wait_seconds = 0.1
            current = await peer.post(url, params=task_params(ref))
            assert current.status_code == 200 and current.json()["status"] == "running", current.text
            await wait_until(lambda: not receiver.request_worker._recoveries)
            async with second_app.state.sessionmaker() as db:
                assert (await db.get(Task, ref["id"])).worker_key == source.lease.worker_key
            assert fake_llm.aborted == 0
            await freeze_heartbeat(source)
            await expire_worker(source)
            receiver.cancel_wait_seconds = 5
            stopped = await peer.post(url, params=task_params(ref))
            assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled", stopped.text
    finally:
        model.release()
    await wait_until(lambda: ref["id"] not in source._running)
    async with second_app.state.sessionmaker() as db:
        row = await db.get(Task, ref["id"])
        assert row.worker_key == receiver.request_worker.worker_key and row.cancel_requested_at is None
        saved = await db.get(Chapter, chapter["id"])
        assert (saved.content, saved.generation_claim_key) == ("原稿", None)


async def test_http_adopter_hands_disconnected_request_to_admin(
    app, second_app, client, system_admin, monkeypatch,
):
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services import task_statistics_service
    from tests.helpers import register, wait_task
    from tests.test_admin_task_adversarial import CANCEL_BODY

    actor = await register(client)
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker.begin() as db:
        row = Task(user_id=actor["id"], kind="outline.continue", status="running", execution_key=str(uuid4()))
        db.add(row)
        await task_statistics_service.record_created(db, row)
    ref = {"id": row.id, "task_key": row.statistics_key}
    adopted = Gate()
    original_commit = AsyncSession.commit

    async def hold_confirmation(db):
        await original_commit(db)
        if asyncio.current_task() in engine.request_worker._recoveries.values() and not adopted.reached.is_set():
            adopted.reached.set()
            await adopted.released.wait()

    monkeypatch.setattr(AsyncSession, "commit", hold_confirmation)
    request = asyncio.create_task(client.post(f"/api/v1/tasks/{row.id}/cancel", params=task_params(ref)))
    try:
        await adopted.wait_reached()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        async with _new_client(second_app) as admin:
            admin.cookies.update(system_admin.cookies)
            accepted = await admin.post(f"/api/v1/admin/tasks/{row.statistics_key}/cancel", json=CANCEL_BODY)
        assert accepted.status_code == 202, accepted.text
        await engine.scan_admin_cancellations()
        await engine.wait_admin_cancellations()
        async with app.state.sessionmaker() as db:
            still = await db.get(Task, row.id)
            assert still.worker_key == engine.request_worker.worker_key and still.status == "running"
            assert still.admin_cancel_operation_id == accepted.json()["operation_id"]
    finally:
        adopted.release()
        await asyncio.gather(request, return_exceptions=True)
    assert (await wait_task(client, ref))["status"] == "interrupted"
    async with app.state.sessionmaker() as db:
        final = await db.get(Task, row.id)
        assert final.admin_cancel_operation_id is None and final.cancel_requested_at is None


async def test_http_adopter_preserves_unknown_result_discovered_after_acceptance(app, client):
    from uuid import uuid4

    from app.services import task_statistics_service
    from tests.helpers import register, wait_until

    actor = await register(client)
    project = (await client.post("/api/v1/projects", json={"title": "待核对"})).json()
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker.begin() as db:
        task = Task(user_id=actor["id"], project_id=project["id"], kind="outline.continue", status="running",
                    execution_key=str(uuid4()), cancel_requested_at=task_statistics_service.now_utc())
        db.add(task)
        await task_statistics_service.record_created(db, task)
    await engine.record_abort_failure(task.project_id, None, task=task)
    engine.request_worker.recover_cancel(task.id, task.statistics_key, task.queued_at, task.execution_key)
    await wait_until(lambda: not engine.request_worker._recoveries)
    async with app.state.sessionmaker() as db:
        uncertain = await db.get(Task, task.id)
        assert uncertain.status == "running" and uncertain.execution_key == task.execution_key
        assert uncertain.recovered_at is not None and uncertain.worker_key is None
        assert uncertain.cancel_requested_at is None


@pytest.mark.parametrize("disconnected", [False, True])
async def test_http_adopter_keeps_committed_round_when_request_cannot_confirm(
    app, client, monkeypatch, disconnected,
):
    from uuid import uuid4

    from app.services import task_service, task_statistics_service
    from app.services.task_engine import owner_cancellation
    from tests.helpers import register, wait_task

    actor = await register(client)
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker.begin() as db:
        task = Task(user_id=actor["id"], kind="outline.continue", status="running", execution_key=str(uuid4()))
        db.add(task)
        await task_statistics_service.record_created(db, task)
    async with app.state.sessionmaker() as request_db:
        commit = request_db.commit

        async def lose_confirmation():
            await commit()
            if disconnected:
                raise asyncio.CancelledError
            raise ConnectionError("提交回执丢失")

        async def cannot_read(*args, **kwargs):
            raise ConnectionError("请求连接的确认读失败")

        monkeypatch.setattr(request_db, "commit", lose_confirmation)
        monkeypatch.setattr(owner_cancellation, "_original", cannot_read)
        with pytest.raises(asyncio.CancelledError if disconnected else ConnectionError):
            await task_service.cancel_task(request_db, engine, actor["id"], task.id,
                                           expected_task_key=task.statistics_key)
    ref = {"id": task.id, "task_key": task.statistics_key}
    assert (await wait_task(client, ref))["status"] == "cancelled"


async def test_http_worker_can_drain_only_the_requested_project_without_a_scheduler(app, client):
    from uuid import uuid4

    from app.models import Project
    from app.services import task_statistics_service
    from tests.helpers import create_project, register

    actor = await register(client)
    project, other = await create_project(client), await create_project(client)
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker.begin() as db:
        rows = [Task(user_id=actor["id"], project_id=item["id"], kind="outline.continue", status="running",
                     execution_key=str(uuid4())) for item in (project, other)]
        for row in rows:
            db.add(row)
            await task_statistics_service.record_created(db, row)
    result = await client.delete(f"/api/v1/projects/{project['id']}")
    assert result.status_code == 204, result.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).deleted_at is not None
        stopped = await db.get(Task, rows[0].id)
        assert stopped.status == "cancelled" and stopped.worker_key == engine.request_worker.worker_key
        untouched = await db.get(Task, rows[1].id)
        assert untouched.status == "running" and untouched.worker_key is None


@pytest.mark.parametrize("same_claim", [True, False], ids=["original-claim", "replaced-claim"])
async def test_project_batch_takeover_requires_matching_deletion_and_chapter_claim(
    app, client, make_client, same_claim,
):
    from uuid import uuid4

    from app.core.errors import ConflictError
    from app.models import Project
    from app.services import batch_service, chapter_claim, project_lifecycle, task_service
    from tests.helpers import create_project
    from tests.test_chapter_claim_core import _world

    info, execution = await _world(app, client, make_client, batch=True)
    engine = app.state.task_engine
    other = await create_project(client)
    foreign = project_lifecycle.DeletionIntent(other["id"], info["actor_id"], info["actor_id"],
                                              str(uuid4()), engine.request_worker.worker_key)
    async with app.state.sessionmaker.begin() as db:
        project = await db.get(Project, other["id"])
        project.deletion_started_at = project_lifecycle.now_utc()
        project.deletion_token, project.deletion_worker_key = foreign.token, foreign.worker_key
        task = await db.get(Task, info["task_id"])
        chapter = await db.get(Chapter, info["chapter_id"])
        checkpoint = batch_service.new_checkpoint({chapter.id: chapter}, [chapter.id])
        task.checkpoint = {**checkpoint, "_batch_attempt": str(uuid4())}
        await db.flush()
        claim = await chapter_claim.claim_batch_chapter(db, **info, execution_key=execution)
    for intent in (None, foreign):
        async with app.state.sessionmaker() as db:
            if intent is not None:
                db.info["project_deletion_intent"] = intent
            with pytest.raises(ConflictError) as refused:
                await task_service.stop_project_tasks(db, engine, info["project_id"])
            assert refused.value.extra["code"] == "project_tasks_not_stopped"
    async with app.state.sessionmaker.begin() as db:
        task = await db.get(Task, info["task_id"])
        chapter = await db.get(Chapter, info["chapter_id"])
        assert task.cancel_requested_at is None and task.worker_key is None
        assert task.checkpoint["current"]["_chapter_claim"]["claim_key"] == claim.claim_key
        assert chapter.status == "generating" and chapter.generation_claim_key == claim.claim_key
        current_claim = claim.claim_key if same_claim else str(uuid4())
        chapter.generation_claim_key = current_claim
    result = await client.delete(f"/api/v1/projects/{info['project_id']}")
    assert result.status_code == (204 if same_claim else 409), result.text
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, info["chapter_id"])
        project = await db.get(Project, info["project_id"])
        if same_claim:
            assert project.deleted_at is not None
            assert chapter.status == "draft" and chapter.generation_claim_key is None
            assert (await db.get(Task, info["task_id"])).status == "cancelled"
        else:
            assert project.deleted_at is None
            assert chapter.status == "generating" and chapter.generation_claim_key == current_claim


async def test_http_waiter_returns_committed_terminal_if_adopters_first_proof_fails(app, client, monkeypatch):
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services import task_statistics_service
    from tests.helpers import register

    actor = await register(client)
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker.begin() as db:
        row = Task(user_id=actor["id"], kind="outline.continue", status="running", execution_key=str(uuid4()))
        db.add(row)
        await task_statistics_service.record_created(db, row)
    original_commit, original_scalar = AsyncSession.commit, AsyncSession.scalar
    lost_reply, lost_proof = False, False

    async def lose_terminal_reply(db):
        nonlocal lost_reply
        terminal = any(isinstance(task, Task) and task.statistics_key == row.statistics_key
                       and task.status == "cancelled" for task in db.identity_map.values())
        await original_commit(db)
        if terminal and not lost_reply:
            lost_reply = True
            raise ConnectionError("终态已经提交，确认未返回")

    async def lose_adopter_proof(db, *args, **kwargs):
        nonlocal lost_proof
        if lost_reply and not lost_proof and asyncio.current_task() in engine.request_worker._recoveries.values():
            lost_proof = True
            raise ConnectionError("接管者的首次确认读失败")
        return await original_scalar(db, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", lose_terminal_reply)
    monkeypatch.setattr(AsyncSession, "scalar", lose_adopter_proof)
    response = await client.post(f"/api/v1/tasks/{row.id}/cancel", params={"task_key": row.statistics_key})
    assert response.status_code == 200 and response.json()["status"] == "cancelled", response.text
    assert lost_reply and lost_proof
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, row.id)
        stat = await db.get(TaskStatistic, row.statistics_key)
        assert task.status == stat.status == "cancelled" and stat.finished_event_at is not None
