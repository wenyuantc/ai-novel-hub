"""用户配额：四作品入口、严格配置及真正任务准入，不以DB running冒充占用。"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import Project
from tests.helpers import Gate, create_ai_config, create_project, register, wait_task
from tests.rbac_helpers import set_quota
from tests.test_book_import import _start, upload
from tests.test_system_settings import save_settings


async def test_project_quota_counts_recycle_and_lower_keeps_existing(client, system_admin):
    user = await register(client)
    initial = (await system_admin.get(f"/api/v1/admin/users/{user['id']}/quotas")).json()
    assert initial["revision"] == 0
    assert initial["project_count"] == {
        "config": {"mode": "inherit"}, "effective_limit": None, "source": "default", "used": 0,
    }
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "limited", "value": 1}})
    project = await create_project(client)
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    blocked = await client.post("/api/v1/projects", json={"title": "不能靠回收绕过"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "quota_exceeded"
    assert blocked.json()["metric"] == "project_count"
    current = await set_quota(system_admin, user["id"], {"project_count": {"mode": "limited", "value": 0}})
    assert current["project_count"]["used"] == 1
    assert (await client.post("/api/v1/projects", json={"title": "零额度"})).status_code == 409
    recycled = (await system_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"][0]
    assert (await system_admin.post(f"/api/v1/admin/recycle-bin/projects/{project['id']}/restore", json={
        "deleted_at": recycled["deleted_at"], "confirm": True,
    })).status_code == 204
    assert (
        await client.patch(f"/api/v1/projects/{project['id']}", json={"title": "零额度仍可维护"})
    ).status_code == 200
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    recycled = (await system_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"][0]
    assert (await system_admin.post(f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge", json={
        "deleted_at": recycled["deleted_at"], "title": recycled["title"], "confirm": True,
    })).status_code == 204
    current = await set_quota(system_admin, user["id"], {"project_count": {"mode": "limited", "value": 1}})
    assert current["project_count"]["used"] == 0
    await create_project(client)
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "unlimited"}})
    await create_project(client)
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "inherit"}})
    await create_project(client)


@pytest.mark.parametrize("config", [
    {"mode": "limited", "value": True}, {"mode": "limited", "value": -1},
    {"mode": "limited", "value": 2_147_483_648}, {"mode": "inherit", "value": 0},
    {"mode": "unlimited", "value": None}, {"mode": "limited"}, {"mode": "custom"},
])
async def test_quota_rejects_ambiguous_or_unbounded_config(client, system_admin, config):
    user = await register(client)
    path = f"/api/v1/admin/users/{user['id']}/quotas"
    current = (await system_admin.get(path)).json()
    response = await system_admin.patch(path, json={
        "revision": current["revision"], "authorization_revision": current["authorization_revision"],
        "changes": {"project_count": config},
    })
    assert response.status_code == 422, response.text
    assert (await system_admin.get(path)).json()["revision"] == 0


async def test_four_project_entries_share_zero_quota_and_preview_survives(client, system_admin, app):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    backup = await client.get(f"/api/v1/projects/{project['id']}/backup")
    assert backup.status_code == 200
    preview = await client.post("/api/v1/projects/import/preview", files={
        "file": ("backup.json", backup.content, "application/json"),
    })
    assert preview.status_code == 200, preview.text
    token = preview.json()["token"]
    inspiration = await client.post("/api/v1/inspiration/sessions", json={"idea": "星河与少年"})
    assert inspiration.status_code == 201, inspiration.text
    book = await upload(client)
    assert book.status_code == 201, book.text
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "limited", "value": 0}})
    responses = [
        await client.post("/api/v1/projects", json={"title": "普通创建"}),
        await client.post("/api/v1/projects/import", json={"token": token}),
        await client.post(
            f"/api/v1/inspiration/sessions/{inspiration.json()['id']}/convert", json={"title_override": "星河"}
        ),
        await _start(client, book.json()),
    ]
    assert [response.status_code for response in responses] == [409] * 4, [r.text for r in responses]
    assert all(response.json()["code"] == "quota_exceeded" for response in responses)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == user["id"])) == 1
    # 配额失败不是导入消费；原预览令牌、拆书revision、灵感会话均可重试。
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "unlimited"}})
    assert (await client.post("/api/v1/projects/import", json={"token": token})).status_code == 201
    assert (await client.post(
        f"/api/v1/inspiration/sessions/{inspiration.json()['id']}/convert", json={"title_override": "星河"},
    )).status_code == 201


async def test_super_admin_still_obeys_project_quota(system_admin):
    user = (await system_admin.get("/api/v1/auth/me")).json()
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "limited", "value": 0}})
    response = await system_admin.post("/api/v1/projects", json={"title": "超管无豁免"})
    assert response.status_code == 409 and response.json()["code"] == "quota_exceeded"


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
async def test_task_zero_queues_then_raise_runs_and_lower_does_not_kill(
    app, client, system_admin, fake_llm, monkeypatch,
):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    chapters = []
    for index in range(2):
        chapters.append((await client.post(
            f"/api/v1/projects/{project['id']}/chapters", json={"title": str(index)},
        )).json())
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 0}})
    engine = app.state.task_engine
    original_dispatch = engine._dispatch
    dispatched = asyncio.Event()

    async def observed_dispatch():
        await original_dispatch()
        dispatched.set()

    monkeypatch.setattr(engine, "_dispatch", observed_dispatch)
    gates = [Gate(), Gate()]
    for gate in gates:
        fake_llm.push([gate, "真实正文"])
    tasks = []
    try:
        for chapter in chapters:
            response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
            assert response.status_code == 202, response.text
            tasks.append(response.json())
        dispatched.clear()
        engine.notify()
        await asyncio.wait_for(dispatched.wait(), 5)
        assert engine.admitted_count(user["id"]) == 0 and not fake_llm.calls
        for task in tasks:
            assert (await client.get(f"/api/v1/tasks/{task['id']}")).json()["status"] == "queued"
        await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 2}})
        await gates[0].wait_reached()
        await gates[1].wait_reached()
        assert engine.admitted_count(user["id"]) == 2
        current = await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 0}})
        assert current["task_concurrency"]["used"] == 2 and fake_llm.aborted == 0
    finally:
        for gate in gates:
            gate.release()
    for task in tasks:
        assert (await wait_task(client, task["id"]))["status"] == "succeeded"


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
async def test_unlimited_task_quota_still_respects_global_cap(app, client, system_admin, fake_llm):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    await save_settings(system_admin, task_max_concurrency=1, task_max_per_user=1)
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "unlimited"}})
    gates = [Gate(), Gate()]
    for gate in gates:
        fake_llm.push([gate, "正文"])
    tasks = []
    try:
        for index in range(2):
            chapter = (await client.post(
                f"/api/v1/projects/{project['id']}/chapters", json={"title": str(index)},
            )).json()
            tasks.append((await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})).json())
        await gates[0].wait_reached()
        assert app.state.task_engine.admitted_count(user["id"]) == 1
        assert len(fake_llm.calls) == 1
        gates[0].release()
        await gates[1].wait_reached()
        assert app.state.task_engine.admitted_count(user["id"]) == 1
    finally:
        for gate in gates:
            gate.release()
    for task in tasks:
        assert (await wait_task(client, task["id"]))["status"] == "succeeded"


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
async def test_explicit_task_limit_replaces_default_and_inherit_uses_latest_default(
    app, client, system_admin, fake_llm, monkeypatch,
):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    await save_settings(system_admin, task_max_concurrency=4, task_max_per_user=1)
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 2}})
    gates = [Gate(), Gate(), Gate()]
    for gate in gates:
        fake_llm.push([gate, "正文"])
    tasks = []
    engine = app.state.task_engine
    dispatched = asyncio.Event()
    original = engine._dispatch

    async def dispatch():
        await original()
        dispatched.set()

    monkeypatch.setattr(engine, "_dispatch", dispatch)
    try:
        # 先固定故事结构再测试并发额度。交错建章会推进全局规划版本，
        # 使已开始构建的旧Scene快照安全失效；那不是本例要测的quota变化。
        chapters = [(await client.post(
            f"/api/v1/projects/{project['id']}/chapters", json={"title": str(index)},
        )).json() for index in range(3)]
        for index in range(3):
            chapter = chapters[index]
            response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
            assert response.status_code == 202, response.text
            tasks.append(response.json())
        await gates[0].wait_reached()
        await gates[1].wait_reached()
        assert engine.admitted_count(user["id"]) == 2 and len(fake_llm.calls) == 2
        await save_settings(system_admin, task_max_per_user=3)
        dispatched.clear()
        engine.notify()
        await asyncio.wait_for(dispatched.wait(), 5)
        assert len(fake_llm.calls) == 2 and not gates[2].reached.is_set()
        quota = await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "inherit"}})
        assert quota["task_concurrency"]["effective_limit"] == 3
        assert quota["task_concurrency"]["source"] == "system"
        await gates[2].wait_reached()
        await save_settings(system_admin, task_max_per_user=1)
        assert engine.admitted_count(user["id"]) == 3 and fake_llm.aborted == 0
    finally:
        for gate in gates:
            gate.release()
    for task in tasks:
        assert (await wait_task(client, task["id"]))["status"] == "succeeded"
