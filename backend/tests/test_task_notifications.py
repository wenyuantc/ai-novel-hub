"""后台任务收尾、检查双收件人与历史概要的跨层回归。"""

from uuid import uuid4

import pytest

from app.ai.providers import AIProviderError
from app.models import ConsistencyRun, Task
from app.services import task_statistics_service as statistics
from tests.consistency_helpers import comparison
from tests.helpers import create_project, register, task_params, wait_task
from tests.test_collaboration_outline_entry_e4g3 import _change, _member, _terminal, _world
from tests.test_consistency_collaboration import BROKEN, USED, _chapters, _finding, _start, _sword
from tests.test_tasks import _generate, _setup

BOX = "/api/v1/notifications"


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def _items(client):
    response = await client.get(BOX)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _unread(client):
    return (await client.get(f"{BOX}/unread-count")).json()["count"]


async def _check(app, actor, world, *, status="succeeded"):
    response = await _start(actor, world)
    assert response.status_code == 202, response.text
    row = await _terminal(app, response.json()["id"])
    assert row.status == status, row.error
    return row


async def test_checks_link_each_run_and_respect_recipient_preferences(app, client, make_client, fake_llm):
    guest, third = make_client(), make_client()
    world = await _world(client, guest)
    await register(third, "third-reader")
    invite = await client.post(f"{world.base}/collaborators", json={"username": "third-reader", "role": "viewer"})
    member = invite.json()
    await third.post(f"/api/v1/collaboration/invitations/project/{member['invitation_id']}/accept",
                     json={"expected_revision": member["revision"]})
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    fake_llm.push(_sword("佩剑在山门前断成两截"))
    fake_llm.push(_sword("拔出佩剑"))
    fake_llm.push(comparison())
    first = await _check(app, guest, world)
    fake_llm.push(_finding(chapters))
    second = await _check(app, guest, world)
    assert [first.result["created"], second.result["created"]] == [0, 1]
    first_id, second_id = first.result["run_id"], second.result["run_id"]

    owner_items, editor_items = await _items(client), await _items(guest)
    assert [item["kind"] for item in owner_items] == [item["kind"] for item in editor_items] == ["consistency"] * 2
    assert await _items(third) == []
    for item, run_id in zip(owner_items, [second_id, first_id], strict=True):
        target = await client.post(f"{BOX}/{item['id']}/open")
        assert target.json() == {"kind": "consistency", "project_id": world.project["id"], "run_id": run_id}
    assert await _unread(client) == 0
    assert await _unread(guest) == 2
    # 旧检查中保存的提供方错误可能含请求上下文。历史概要只呈现安全字段。
    async with app.state.sessionmaker() as db:
        run = await db.get(ConsistencyRun, first_id)
        run.stats = {**run.stats, "warnings": ["provider-private-diagnostic"],
                     "skipped_chapters": [{"chapter_id": chapters[1]["id"], "state": "failed",
                                           "error": "provider-private-diagnostic"}]}
        await db.commit()
    path = f"{world.base}/consistency/runs/{first_id}"
    notice_params = {"notification_id": owner_items[1]["id"]}
    assert (await client.get(path, params=notice_params)).status_code == 200
    assert (await guest.get(path, params=notice_params)).status_code == 404
    assert (await client.get(path, params={"notification_id": owner_items[0]["id"]})).status_code == 404
    assert (await client.get(path, params={"notification_id": str(uuid4())})).status_code == 404
    for reader in (client, guest, third):
        history = await reader.get(path)
        assert history.status_code == 200 and history.json()["stats"]["created"] == 0
        assert "task_key" not in history.json() and "provider-private-diagnostic" not in history.text
    assert (await client.get(path)).json()["task_id"] is None
    assert (await guest.get(path)).json()["task_id"] == first.id
    other = await create_project(client)
    assert (await client.get(f"/api/v1/projects/{other['id']}/consistency/runs/{first_id}")).status_code == 404

    await client.put(f"{BOX}/preferences/consistency", json={"enabled": False})
    fake_llm.push(comparison())
    await _check(app, guest, world)
    assert len(await _items(client)) == 2
    assert len(await _items(guest)) == 3
    await client.put(f"{BOX}/preferences/consistency", json={"enabled": True})
    fake_llm.push(comparison())
    await _check(app, client, world)
    assert len(await _items(client)) == 3
    assert len(await _items(guest)) == 3

    # 检查失败进入发起人的普通任务提醒；重试用另一任务 UUID，成功后仅进检查类。
    fake_llm.push("非 JSON")
    failed = await _check(app, guest, world, status="failed")
    failed_notice = (await _items(guest))[0]
    assert failed_notice["kind"] == "task"
    assert (await guest.post(f"{BOX}/{failed_notice['id']}/open")).json() == {
        "kind": "task", "task_id": failed.id, "task_key": failed.statistics_key,
    }
    assert len(await _items(client)) == 3
    fake_llm.push(comparison())
    retried = await guest.post(f"/api/v1/tasks/{failed.id}/retry", params={"task_key": failed.statistics_key})
    assert retried.status_code == 202, retried.text
    assert (await _terminal(app, retried.json()["id"])).status == "succeeded"
    assert (await _items(guest))[0]["kind"] == "consistency"
    assert len(await _items(client)) == 4

    await _change(app, client, world, "revoked")
    assert (await guest.get(path)).status_code == 404
    assert await _items(guest) == []
    assert (await guest.post(f"{BOX}/{editor_items[0]['id']}/open")).status_code == 404


async def test_first_terminal_transaction_owns_delivery(app, client):
    user = await register(client)
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], kind="outline.expand", status="queued")
        db.add(task)
        await statistics.record_created(db, task)
        await db.commit()
        task_id = task.id
        task.status = "failed"
        await statistics.record_state_change(db, task, "failed")
        await db.rollback()
    assert await _items(client) == []
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task.status = "succeeded"
        await statistics.record_state_change(db, task, "succeeded")
        await db.commit()
        await statistics.record_state_change(db, task, "succeeded")
        await db.commit()
    notice, = await _items(client)
    target = (await client.post(f"{BOX}/{notice['id']}/open")).json()
    assert target["task_key"] == task.statistics_key
    detail = await client.get(f"/api/v1/tasks/{task_id}", params={"task_key": target["task_key"]})
    assert detail.status_code == 200 and detail.json()["status"] == "succeeded"

async def test_failed_task_and_successful_retry_open_distinct_tasks(app, client, fake_llm):
    _, _, (chapter,) = await _setup(client)
    fake_llm.push(AIProviderError("测试服务不可用"))
    first = await wait_task(client, (await _generate(client, chapter)).json())
    assert first["status"] == "failed"
    fake_llm.push("重试写入正文")
    retry = await client.post(f"/api/v1/tasks/{first['id']}/retry", params=task_params(first))
    assert retry.status_code == 202, retry.text
    second = await wait_task(client, retry.json())
    assert second["status"] == "succeeded"
    notifications = await _items(client)
    assert len(notifications) == 2
    for notice, task in zip(notifications, [second, first], strict=True):
        assert notice["kind"] == "task"
        assert (await client.post(f"{BOX}/{notice['id']}/open")).json() == {
            "kind": "task", "task_id": task["id"], "task_key": task["task_key"],
        }


async def test_reinvitation_does_not_authorize_old_check_completion(app, client, make_client):
    guest = make_client()
    world = await _world(client, guest)
    old = await _member(app, world)
    await _change(app, client, world, "reinvite")
    # 模拟旧执行收据迟到：统计仍须收尾，但新邀请不能授权投递给发起人或作者。
    async with app.state.sessionmaker() as db:
        task = Task(user_id=world.guest["id"], project_id=world.project["id"], kind="consistency.check",
                    status="succeeded", input={"_collaboration_grant": {
                        "invitation_id": old.invitation_id, "revision": old.revision,
                    }})
        db.add(task)
        await statistics.record_created(db, task)
        run = ConsistencyRun(task_id=task.id, project_id=task.project_id, actor_id=task.user_id, status="succeeded")
        db.add(run)
        await db.flush()
        task.result = {"run_id": run.id}
        await statistics.record_state_change(db, task, "succeeded")
        await db.commit()
        owner_task = Task(user_id=world.owner["id"], project_id=world.project["id"], kind="consistency.check",
                          status="succeeded", result={"run_id": run.id})
        db.add(owner_task)
        await statistics.record_created(db, owner_task)
        await statistics.record_state_change(db, owner_task, "succeeded")
        await db.commit()
    assert await _items(client) == []
    assert await _items(guest) == []
