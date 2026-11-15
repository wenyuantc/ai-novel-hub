"""细纲同步隐式删章不得中断他人的任务。"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import Chapter, ChapterPlan, OutlineSnapshot, Task
from app.services import ai_dispatch
from tests.rbac_helpers import assign_roles, create_role
from tests.test_collaboration_planning_access import _collaborators


async def _setup(app, client, make_client):
    base, viewer, editor, _, users = await _collaborators(client, make_client)
    owner = (await client.get("/api/v1/auth/me")).json()
    outline = await client.post(f"{base}/outlines", json={"title": "原大纲", "chapter_no": 1})
    assert outline.status_code == 201, outline.text
    path = f"{base}/outlines/{outline.json()['id']}/plans"
    plans = await client.put(path, json={"plans": [{"title": "甲细纲"}, {"title": "乙细纲"}]})
    assert plans.status_code == 200, plans.text
    built = await client.post(f"{path}/build")
    assert built.status_code == 200, built.text
    await app.state.task_engine.stop()
    return base, viewer, editor, users, owner, path, built.json()["plans"]


def _keep(plan):
    return {"id": plan["id"], "title": plan["title"]}


@pytest.mark.parametrize("kind,status,scope", [
    ("chapter.analyze", "queued", "chapter"),
    ("chapter.generate", "paused", "chapter"),
    ("chapter.generate", "paused", "review"),
    ("chapter.batch", "running", "batch"),
    ("chapter.batch", "paused", "batch"),
    ("chapter.batch", "queued", "invalid_batch"),
    ("agent.turn", "running", "unknown"),
])
async def test_sync_cannot_delete_chapter_used_by_other_tasks(app, client, make_client, kind, status, scope):
    base, viewer, editor, _, owner, path, plans = await _setup(app, client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    chapter_id = plans[0]["chapter_id"]
    payload = ({"chapter_id": chapter_id} if scope in {"chapter", "review"} else {"chapter_ids": [chapter_id]}
               if scope == "batch" else {"chapter_ids": "unknown"} if scope == "invalid_batch" else {})
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project_id,
                    chapter_id=chapter_id if scope in {"chapter", "review"} else None,
                    kind=kind, status=status, input=payload,
                    checkpoint={"rich_review": {"phase": "awaiting_review"}} if scope == "review" else None)
        db.add(task)
        await db.flush()
        task_id = task.id
        await db.commit()
    assert (await viewer.put(path, json={"plans": [_keep(plans[1])]})).status_code == 403
    response = await editor.put(path, json={"plans": [_keep(plans[1])]})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "chapter_in_use"
    async with app.state.sessionmaker() as db:
        assert await db.get(Chapter, chapter_id) is not None
        assert (await db.get(ChapterPlan, plans[0]["id"])).chapter_id == chapter_id
        task = await db.get(Task, task_id)
        assert task.status == status and task.chapter_id == (chapter_id if scope in {"chapter", "review"} else None)
        assert await db.scalar(select(func.count()).select_from(OutlineSnapshot)) == 0


@pytest.mark.parametrize("role", ["owner", "admin", "admin_collaborator"])
async def test_sync_preserves_owner_and_admin_delete_behavior(app, client, make_client, system_admin, role):
    base, _, editor, users, owner, path, plans = await _setup(app, client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=owner["id"], project_id=project_id, chapter_id=plans[0]["chapter_id"],
                    kind="chapter.analyze", status="queued", input={"chapter_id": plans[0]["chapter_id"]}))
        await db.commit()
    if role == "admin_collaborator":
        delegated = await create_role(system_admin, "规划管理", ["projects.read", "projects.edit"])
        await assign_roles(system_admin, users["editor"]["id"], [delegated["id"]])
    actor = {"owner": client, "admin": system_admin, "admin_collaborator": editor}[role]
    response = await actor.put(path, json={"plans": [_keep(plans[1])]})
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(Chapter, plans[0]["chapter_id"]) is None


async def test_non_deleting_sync_does_not_acquire_send_gate(app, client, make_client, monkeypatch):
    base, _, editor, _, owner, path, plans = await _setup(app, client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=owner["id"], project_id=project_id, chapter_id=plans[0]["chapter_id"],
                    kind="chapter.analyze", status="queued", input={"chapter_id": plans[0]["chapter_id"]}))
        await db.commit()

    async def fail_gate(*_args, **_kwargs):
        raise AssertionError("不删章节的细纲同步不应获取发送写门禁")

    monkeypatch.setattr(ai_dispatch, "lock_visibility_write", fail_gate)
    response = await editor.put(path, json={"plans": [_keep(plan) for plan in plans]})
    assert response.status_code == 200, response.text


async def test_sync_may_delete_chapter_unrelated_to_proven_other_task(app, client, make_client):
    base, _, editor, _, owner, path, plans = await _setup(app, client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=owner["id"], project_id=project_id, chapter_id=plans[1]["chapter_id"],
                    kind="chapter.analyze", status="queued", input={"chapter_id": plans[1]["chapter_id"]}))
        await db.commit()
    response = await editor.put(path, json={"plans": [_keep(plans[1])]})
    assert response.status_code == 200, response.text


async def test_sync_may_delete_chapter_used_by_actors_own_task(app, client, make_client):
    base, _, editor, users, _, path, plans = await _setup(app, client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=users["editor"]["id"], project_id=project_id, chapter_id=plans[0]["chapter_id"],
                    kind="chapter.analyze", status="queued", input={"chapter_id": plans[0]["chapter_id"]}))
        await db.commit()
    response = await editor.put(path, json={"plans": [_keep(plans[1])]})
    assert response.status_code == 200, response.text


async def test_sync_multi_chapter_delete_is_atomic_when_one_is_in_use(app, client, make_client):
    base, _, editor, _, owner, path, plans = await _setup(app, client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=owner["id"], project_id=project_id, chapter_id=plans[1]["chapter_id"],
                    kind="chapter.analyze", status="queued", input={"chapter_id": plans[1]["chapter_id"]}))
        await db.commit()
    response = await editor.put(path, json={"plans": []})
    assert response.status_code == 409 and response.json()["code"] == "chapter_in_use", response.text
    async with app.state.sessionmaker() as db:
        for plan in plans:
            assert await db.get(Chapter, plan["chapter_id"]) is not None
            assert (await db.get(ChapterPlan, plan["id"])).chapter_id == plan["chapter_id"]
        assert await db.scalar(select(func.count()).select_from(OutlineSnapshot)) == 0


@pytest.mark.parametrize("was_admin", [False, True])
async def test_sync_rechecks_changed_admin_source_before_mutation(app, client, make_client, system_admin, monkeypatch,
                                                           was_admin):
    from app.services import plan_service

    _, _, editor, users, _, path, plans = await _setup(app, client, make_client)
    role = await create_role(system_admin, "变动中的规划管理者", ["projects.read", "projects.edit"])
    if was_admin:
        await assign_roles(system_admin, users["editor"]["id"], [role["id"]])
    reached, release = asyncio.Event(), asyncio.Event()
    original = plan_service.require_project

    async def wait_before_lock(db, actor_id, project_id, capability, **kwargs):
        if actor_id == users["editor"]["id"] and kwargs.get("for_update"):
            assert kwargs.get("visibility_write") is not was_admin
            reached.set()
            await release.wait()
        return await original(db, actor_id, project_id, capability, **kwargs)

    monkeypatch.setattr(plan_service, "require_project", wait_before_lock)
    pending = asyncio.create_task(editor.put(path, json={"plans": [_keep(plans[1])]}))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await assign_roles(system_admin, users["editor"]["id"], [] if was_admin else [role["id"]])
        release.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == 409 and response.json()["code"] == "collaboration_busy", response.text
        async with app.state.sessionmaker() as db:
            assert await db.get(Chapter, plans[0]["chapter_id"]) is not None
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
