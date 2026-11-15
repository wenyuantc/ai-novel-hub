"""分析响应在 PostgreSQL 跨连接提交后重验本人、成员及原任务轮次。"""

import asyncio
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text, update

from app.core.deps import get_db
from app.models import ChapterAnalysis, ProjectCollaborator, Task, User
from app.services import analysis_service, rich_document_ai, writeback_service
from tests.test_collaboration_access_postgresql import _setup
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture

# 复用隔离 schema 和显式本机专用库检查；未提供测试 URL 时跳过。
postgres_analysis_apps = _postgres_fixture


@pytest.mark.parametrize(("endpoint", "transition", "isolation"), [
    ("project-status", "revision", "READ COMMITTED"),
    ("chapter-status", "downgrade", "READ COMMITTED"),
    ("history", "renew_grant", "READ COMMITTED"),
    ("detail", "reinvite", "READ COMMITTED"),
    ("detail", "replace", "READ COMMITTED"),
    ("project-status", "queued", "READ COMMITTED"),
    ("chapter-status", "finished", "READ COMMITTED"),
    ("detail", "finished_downgrade", "READ COMMITTED"),
    ("history", "revoke", "READ COMMITTED"),
    ("history", "revoke_no_ref", "READ COMMITTED"),
    ("detail", "token", "READ COMMITTED"),
    ("project-status", "queued", "REPEATABLE READ"),
    ("history", "revoke", "REPEATABLE READ"),
    ("detail", "renew_grant", "REPEATABLE READ"),
])
async def test_analysis_final_snapshot_after_concurrent_commit(
    postgres_analysis_apps, monkeypatch, endpoint, transition, isolation,
):
    owner_app, guest_app = postgres_analysis_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        chapter = await owner.post(f"{base}/chapters", json={"title": "分析竞态", "content": "章节正文"})
        assert chapter.status_code == 201, chapter.text
        chapter_id = chapter.json()["id"]
        async with owner_app.state.sessionmaker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
            ))
            invitation, revision = member.invitation_id, member.revision
            task = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter_id,
                        kind="chapter.analyze", status="queued", input={"_collaboration_grant": {
                            "invitation_id": invitation, "revision": revision,
                        }})
            db.add(task)
            await db.flush()
            analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter_id, chapter_no=1,
                                       task_id=task.id, content_hash="0" * 64, summary="保留的分析正文")
            db.add(analysis)
            await db.flush()
            task_id, analysis_id, task_key = task.id, analysis.id, task.statistics_key
            if transition == "revoke_no_ref":
                await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=None))
            await db.commit()

        baseline = await guest.get(f"/api/v1/analyses/{analysis_id}")
        assert baseline.status_code == 200, baseline.text
        assert baseline.json()["summary"] == "保留的分析正文"
        assert baseline.json()["task_id"] == (None if transition == "revoke_no_ref" else task_id)

        # 该覆盖在认证查询之前设置请求事务隔离，最终复验仍须独立开启新事务。
        async def isolated_db():
            async with guest_app.state.sessionmaker() as db:
                await db.connection(execution_options={"isolation_level": isolation})
                yield db

        guest_app.dependency_overrides[get_db] = isolated_db
        if endpoint in {"project-status", "chapter-status"}:
            target, attr = rich_document_ai, "analysis_fingerprints"
        elif endpoint == "history":
            target, attr = analysis_service, "_visible_task_ids"
        else:
            target, attr = writeback_service, "list_changes"
        original = getattr(target, attr)
        ready, release = asyncio.Event(), asyncio.Event()
        request_info = {}

        async def gated(db, *args, **kwargs):
            result = await original(db, *args, **kwargs)
            request_info["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            request_info["isolation"] = await db.scalar(text("SHOW transaction_isolation"))
            ready.set()
            await release.wait()
            return result

        monkeypatch.setattr(target, attr, gated)
        path = {
            "project-status": f"{base}/analysis/statuses",
            "chapter-status": f"/api/v1/chapters/{chapter_id}/analysis-status",
            "history": f"/api/v1/chapters/{chapter_id}/analyses",
            "detail": f"/api/v1/analyses/{analysis_id}",
        }[endpoint]
        pending = asyncio.create_task(guest.get(path))
        try:
            await asyncio.wait_for(ready.wait(), 15)
            assert request_info["isolation"] == isolation.lower()
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(text("SELECT pg_backend_pid()")) != request_info["pid"]
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                task = await db.get(Task, task_id)
                if transition in {"revision", "renew_grant", "reinvite"}:
                    member.revision += 1
                    if transition == "reinvite":
                        member.invitation_id = str(uuid4())
                    if transition != "revision":
                        task.input = {"_collaboration_grant": {
                            "invitation_id": member.invitation_id, "revision": member.revision,
                        }}
                elif transition in {"downgrade", "finished_downgrade"}:
                    member.role = "viewer"
                    member.revision += 1
                    if transition == "finished_downgrade":
                        task.status = "succeeded"
                elif transition in {"revoke", "revoke_no_ref"}:
                    member.status = "revoked"
                    member.revision += 1
                elif transition == "queued":
                    task.queued_at += timedelta(seconds=1)
                elif transition == "finished":
                    task.status = "succeeded"
                elif transition == "token":
                    await db.execute(update(User).where(User.id == user["id"]).values(
                        token_version=User.token_version + 1,
                    ))
                elif transition == "replace":
                    await db.delete(task)
                    await db.flush()
                    replacement = Task(id=task_id, user_id=user["id"], project_id=project["id"],
                                       chapter_id=chapter_id, kind="chapter.analyze", status="queued",
                                       input={"_collaboration_grant": {
                                           "invitation_id": invitation, "revision": revision,
                                       }})
                    db.add(replacement)
                    await db.flush()
                    assert replacement.statistics_key != task_key
                await db.commit()
            release.set()
            response = await asyncio.wait_for(pending, 15)
            if transition in {"revoke", "revoke_no_ref", "token"}:
                assert response.status_code == (401 if transition == "token" else 404), response.text
            else:
                assert response.status_code == 200, response.text
                if endpoint in {"project-status", "chapter-status"}:
                    state = response.json()[0] if endpoint == "project-status" else response.json()
                    assert state["status"] != "analyzing" and state["active_task_id"] is None
                elif endpoint == "history":
                    assert response.json()[0]["task_id"] is None
                else:
                    assert response.json()["task_id"] is None
                    assert response.json()["summary"] == "保留的分析正文"
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            guest_app.dependency_overrides.pop(get_db, None)


async def test_postgres_legacy_author_task_hides_reference_preserving_analysis(postgres_analysis_apps):
    owner_app, guest_app = postgres_analysis_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        owner_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        chapter = await owner.post(f"{base}/chapters", json={"title": "旧分析", "content": "正文"})
        assert chapter.status_code == 201, chapter.text
        chapter_id = chapter.json()["id"]
        async with owner_app.state.sessionmaker() as db:
            task = Task(user_id=owner_id, project_id=project["id"], chapter_id=chapter_id,
                        kind="chapter.analyze", status="queued")
            db.add(task)
            await db.flush()
            task_id = task.id
            await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=None))
            analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter_id, chapter_no=1,
                                       task_id=task_id, content_hash="0" * 64, summary="原作者分析正文")
            db.add(analysis)
            await db.flush()
            analysis_id = analysis.id
            await db.commit()
        state = await owner.get(f"/api/v1/chapters/{chapter_id}/analysis-status")
        assert state.status_code == 200 and state.json()["active_task_id"] is None
        detail = await owner.get(f"/api/v1/analyses/{analysis_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["task_id"] is None and detail.json()["summary"] == "原作者分析正文"
        async with owner_app.state.sessionmaker() as db:
            assert (await db.get(ChapterAnalysis, analysis_id)).task_id == task_id
            assert (await db.get(Task, task_id)).statistics_key is None


@pytest.mark.parametrize(("endpoint", "isolation"), [
    ("project-status", "READ COMMITTED"),
    ("detail", "REPEATABLE READ"),
])
async def test_analysis_rechecks_after_postgres_http_reinvite(
    postgres_analysis_apps, monkeypatch, endpoint, isolation,
):
    """请求已读取旧任务后，另一连接真实撤销、重邀并接受，再返回分析。"""
    from app.services import collaboration_service

    owner_app, guest_app = postgres_analysis_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        chapter = await owner.post(f"{base}/chapters", json={"title": "重邀分析", "content": "章节正文"})
        assert chapter.status_code == 201, chapter.text
        chapter_id = chapter.json()["id"]
        async with owner_app.state.sessionmaker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
            ))
            invitation, revision = member.invitation_id, member.revision
            grant = {"invitation_id": invitation, "revision": revision}
            task = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter_id,
                        kind="chapter.analyze", status="queued", input={"_collaboration_grant": grant})
            db.add(task)
            await db.flush()
            analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter_id, chapter_no=1,
                                       task_id=task.id, content_hash="0" * 64, summary="重邀前的共享分析")
            db.add(analysis)
            await db.flush()
            task_id, analysis_id = task.id, analysis.id
            await db.commit()
        initial = await guest.get(f"/api/v1/analyses/{analysis_id}")
        assert initial.status_code == 200 and initial.json()["task_id"] == task_id, initial.text

        async def isolated_db():
            async with guest_app.state.sessionmaker() as db:
                await db.connection(execution_options={"isolation_level": isolation})
                yield db

        guest_app.dependency_overrides[get_db] = isolated_db
        target, attr = ((rich_document_ai, "analysis_fingerprints") if endpoint == "project-status"
                        else (writeback_service, "list_changes"))
        original = getattr(target, attr)
        original_audit = collaboration_service.add_success
        ready, release = asyncio.Event(), asyncio.Event()
        read_info = {}
        mutations = []

        async def gated(db, *args, **kwargs):
            result = await original(db, *args, **kwargs)
            read_info["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            read_info["isolation"] = await db.scalar(text("SHOW transaction_isolation"))
            ready.set()
            await release.wait()
            return result

        async def record_mutation_connection(db, snapshot):
            result = await original_audit(db, snapshot)
            if snapshot.action in {"revoke", "invite", "accept"}:
                mutations.append((snapshot.action, await db.scalar(text("SELECT pg_backend_pid()"))))
            return result

        monkeypatch.setattr(target, attr, gated)
        monkeypatch.setattr(collaboration_service, "add_success", record_mutation_connection)
        path = f"{base}/analysis/statuses" if endpoint == "project-status" else f"/api/v1/analyses/{analysis_id}"
        pending = asyncio.create_task(guest.get(path))
        try:
            await asyncio.wait_for(ready.wait(), 15)
            assert read_info["isolation"] == isolation.lower()
            revoked = await owner.delete(f"{base}/collaborators/{user['id']}", params={"expected_revision": revision})
            assert revoked.status_code == 204, revoked.text
            invited = await owner.post(f"{base}/collaborators", json={"username": "pg-guest", "role": "editor"})
            assert invited.status_code == 201, invited.text
            new = invited.json()
            assert new["invitation_id"] != invitation
            accepted = await guest.post(
                f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept",
                json={"expected_revision": new["revision"]},
            )
            assert accepted.status_code == 200, accepted.text
            assert [action for action, _ in mutations] == ["revoke", "invite", "accept"]
            assert all(pid != read_info["pid"] for _, pid in mutations)
            release.set()
            response = await asyncio.wait_for(pending, 15)
            assert response.status_code == 200, response.text
            if endpoint == "project-status":
                assert response.json()[0]["active_task_id"] is None
                assert response.json()[0]["status"] != "analyzing"
            else:
                assert response.json()["task_id"] is None
                assert response.json()["summary"] == "重邀前的共享分析"
            async with owner_app.state.sessionmaker() as db:
                task = await db.get(Task, task_id)
                assert task.status == "queued" and task.input["_collaboration_grant"] == grant
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            guest_app.dependency_overrides.pop(get_db, None)
