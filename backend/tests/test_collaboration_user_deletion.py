"""管理员删除用户时不能丢失其在别人作品中尚未收尾的任务。"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import AdminAuditLog, Chapter, Project, ProjectCollaborator, Task, TaskStatistic, User
from app.services import task_statistics_service
from tests.helpers import create_project, register


async def _collaboration(app, owner, guest):
    await app.state.task_engine.stop()
    await register(owner, "delete-owner")
    user = await register(guest, "delete-guest")
    project = await create_project(owner, title="不应暴露的作品名称")
    base = f"/api/v1/projects/{project['id']}"
    chapter = await owner.post(f"{base}/chapters", json={"title": "不应暴露的章节名称", "content": "原正文"})
    assert chapter.status_code == 201, chapter.text
    invite = await owner.post(f"{base}/collaborators", json={"username": "delete-guest", "role": "editor"})
    assert invite.status_code == 201, invite.text
    invitation = invite.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return user, project, chapter.json(), accepted.json()


@pytest.mark.parametrize("status", ["queued", "running", "paused", "unrecognized"])
@pytest.mark.parametrize("disabled", [False, True])
async def test_open_shared_project_task_blocks_user_deletion_with_safe_audit(
    app, client, make_client, system_admin, status, disabled,
):
    guest = make_client()
    user, project, chapter, member = await _collaboration(app, client, guest)
    if disabled:
        response = await system_admin.patch(f"/api/v1/admin/users/{user['id']}", json={"is_active": False})
        assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        actual_chapter = await db.get(Chapter, chapter["id"])
        actual_chapter.status = "generating"
        task = Task(
            user_id=user["id"], project_id=project["id"], chapter_id=chapter["id"],
            kind="chapter.generate", status="queued" if status == "unrecognized" else status,
            title="不应暴露的任务名称",
            input={"_collaboration_grant": {
                "invitation_id": member["invitation_id"], "revision": member["revision"],
            }},
        )
        db.add(task)
        await task_statistics_service.record_created(db, task)
        # 模拟旧任务行损坏；统计表受独立 CHECK 保护，不能先写入未知状态。
        task.status = status
        await db.commit()
        task_id, task_key = task.id, task.statistics_key

    rejected = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "user_has_pending_tasks"
    assert set(rejected.json()) == {"detail", "code"}
    for secret in ("不应暴露的作品名称", "不应暴露的章节名称", "不应暴露的任务名称", task_key):
        assert secret not in rejected.text

    async with app.state.sessionmaker() as db:
        actual_user = await db.get(User, user["id"])
        actual_project = await db.get(Project, project["id"])
        actual_chapter = await db.get(Chapter, chapter["id"])
        actual_task = await db.get(Task, task_id)
        stat = await db.get(TaskStatistic, task_key)
        membership = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
        ))
        assert actual_user is not None and actual_user.is_active is (not disabled)
        assert actual_project is not None and actual_project.title == "不应暴露的作品名称"
        assert actual_chapter.status == "generating" and actual_chapter.content == "原正文"
        assert actual_task is not None and actual_task.status == status and actual_task.statistics_key == task_key
        assert stat is not None and stat.removed_at is None
        assert membership is not None and membership.status == "accepted"
        logs = list(await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.action == "user.delete", AdminAuditLog.target_id == user["id"],
        )))
        assert len(logs) == 1 and logs[0].result == "failure" and logs[0].error_code == "conflict"
        assert logs[0].project_id is None and logs[0].chapter_id is None and logs[0].version_ref is None
        rendered = json.dumps(logs[0].summary, ensure_ascii=False)
        assert "不应暴露" not in rendered and task_key not in rendered


async def test_finished_shared_tasks_and_projectless_work_keep_existing_delete_behavior(app, client, make_client,
                                                                                    system_admin):
    guest = make_client()
    user, project, chapter, _ = await _collaboration(app, client, guest)
    async with app.state.sessionmaker() as db:
        tasks = [
            Task(user_id=user["id"], project_id=project["id"], kind="chapter.generate", status=status)
            for status in ("succeeded", "failed", "cancelled", "interrupted")
        ]
        # 个人、无作品任务维持原有删除与统计语义。
        tasks.append(Task(user_id=user["id"], project_id=None, kind="book.import", status="running"))
        db.add_all(tasks)
        for task in tasks:
            await task_statistics_service.record_created(db, task)
        await db.commit()
        keys = [task.statistics_key for task in tasks]

    response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
    assert response.status_code == 204, response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is None
        assert await db.get(Project, project["id"]) is not None
        assert (await db.get(Chapter, chapter["id"])).content == "原正文"
        assert (await db.scalars(select(Task).where(Task.user_id == user["id"]))).all() == []
        statistics = list(await db.scalars(select(TaskStatistic).where(TaskStatistic.key.in_(keys))))
        assert len(statistics) == len(keys) and all(row.removed_at is not None for row in statistics)


@pytest.mark.parametrize("status", ["queued", "cancelled"])
@pytest.mark.parametrize("executing", [False, True])
async def test_projectless_task_only_blocks_deletion_when_execution_is_unsettled(
    app, client, system_admin, status, executing,
):
    await app.state.task_engine.stop()
    user = await register(client, "delete-personal-task")
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=None, chapter_id=None, kind="book.import", status=status,
                    execution_key=str(uuid4()) if executing else None)
        db.add(task)
        await db.commit()
        task_id = task.id
    response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
    async with app.state.sessionmaker() as db:
        if executing:
            assert response.status_code == 409 and response.json()["code"] == "user_has_pending_tasks"
            assert await db.get(User, user["id"]) is not None
            assert (await db.get(Task, task_id)).execution_key is not None
        else:
            assert response.status_code == 204, response.text
            assert await db.get(User, user["id"]) is None and await db.get(Task, task_id) is None
        audit = await db.scalar(select(AdminAuditLog).where(
            AdminAuditLog.action == "user.delete", AdminAuditLog.target_id == user["id"],
        ))
        assert audit is not None and audit.result == ("failure" if executing else "success")


@pytest.mark.parametrize("status", ["queued", "running", "paused"])
async def test_missing_project_id_cannot_hide_an_open_chapter_task(app, client, make_client, system_admin, status):
    guest = make_client()
    user, project, chapter, _ = await _collaboration(app, client, guest)
    async with app.state.sessionmaker() as db:
        # 旧任务损坏导致 project_id 丢失，仍须保留有效 chapter_id 的占用信息。
        task = Task(user_id=user["id"], project_id=None, chapter_id=chapter["id"],
                    kind="chapter.generate", status=status)
        db.add(task)
        await db.commit()
        task_id = task.id

    response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
    assert response.status_code == 409 and response.json()["code"] == "user_has_pending_tasks"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).chapter_id == chapter["id"]
        assert await db.get(User, user["id"]) is not None
        assert await db.get(Project, project["id"]) is not None
