"""真实 PostgreSQL 上章节、场景、富文档与撤权的锁后授权。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.core.errors import ConflictError
from app.models import Chapter, ChapterVersion, ProjectCollaborator, ProjectCover, Scene, SceneSection
from app.services import chapter_service, project_lifecycle
from app.services.audit_writer import AuditOperation
from tests.cover_fixtures import PNG
from tests.test_collaboration_access_postgresql import _assert_project_row_locked, _setup, _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row
from tests.test_rich_document_api import add_section, save_payload

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def _chapter(owner, base):
    response = await owner.post(f"{base}/chapters", json={"title": "竞争章节", "content": "原始正文"})
    assert response.status_code == 201, response.text
    chapter = response.json()
    url = f"{base}/chapters/{chapter['id']}"
    scene = (await owner.get(f"{url}/scenes")).json()["items"][0]
    version = (await owner.get(f"{url}/versions")).json()[0]
    return chapter, scene, version, url


@pytest.mark.parametrize(("kind", "downgrade"), [
    ("chapter", False), ("scene", False), ("document", False), ("version", False),
    ("restore", False), ("cover", False), ("chapter", True), ("document", True),
])
async def test_revocation_before_manual_content_write_rechecks_project_row(
    postgres_collaboration_apps, kind, downgrade,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        chapter, scene, version, chapter_url = await _chapter(owner, base)
        document_url = f"{chapter_url}/scenes/{scene['id']}/document"
        document = save_payload((await owner.get(document_url)).json())
        add_section(document, enabled=True)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                if downgrade:
                    member.role = "viewer"
                else:
                    member.status = "revoked"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                if kind == "chapter":
                    call = guest.patch(chapter_url, json={"title": "撤权后不能写"})
                elif kind == "scene":
                    call = guest.patch(f"{chapter_url}/scenes/{scene['id']}", json={
                        "expected_content_revision": 0, "title": "撤权后不能写",
                    })
                elif kind == "document":
                    call = guest.put(document_url, json=document)
                elif kind == "version":
                    call = guest.patch(f"{chapter_url}/versions/{version['id']}", json={"starred": True})
                elif kind == "restore":
                    call = guest.post(f"{chapter_url}/versions/{version['id']}/restore", json={
                        "expected_content_revision": 0,
                    })
                else:
                    call = guest.put(f"{base}/cover", files={"file": ("cover.png", PNG)})
                pending = asyncio.create_task(call)
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            response = await asyncio.wait_for(pending, 6)
            assert response.status_code == (403 if downgrade else 404), response.text
            if downgrade:
                assert response.json()["code"] == "project_readonly"
            async with owner_app.state.sessionmaker() as db:
                assert (await db.get(Chapter, chapter["id"])).title == "竞争章节"
                assert (await db.get(Scene, scene["id"])).title == scene["title"]
                assert not (await db.get(ChapterVersion, version["id"])).starred
                assert await db.get(ProjectCover, project["id"]) is None
                assert await db.scalar(select(SceneSection.id)) is None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_manual_chapter_commit_before_revoke_preserves_result(postgres_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        chapter, _, _, chapter_url = await _chapter(owner, base)
        reached, release = asyncio.Event(), asyncio.Event()
        projection_reached, projection_release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = AuditOperation.commit
        original_projection = chapter_service.manual_chapter_views

        async def gated(self, *args, **kwargs):
            if self.values["action"] == "chapter.update" and self.actor_id == user["id"]:
                await self.db.flush()
                holder["pid"] = await self.db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return await original(self, *args, **kwargs)

        async def gated_projection(db, actor_id, project_id, chapters, *, full):
            if actor_id == user["id"]:
                projection_reached.set()
                await projection_release.wait()
            return await original_projection(db, actor_id, project_id, chapters, full=full)

        monkeypatch.setattr(AuditOperation, "commit", gated)
        monkeypatch.setattr(chapter_service, "manual_chapter_views", gated_projection)
        write = asyncio.create_task(guest.patch(chapter_url, json={"title": "先提交的章节"}))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 6)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project["id"])
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": 2,
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 6)
            release.set()
            await asyncio.wait_for(projection_reached.wait(), 6)
            assert (await asyncio.wait_for(revoke, 6)).status_code == 204
            projection_release.set()
            result = await asyncio.wait_for(write, 6)
            assert result.status_code == 404, result.text
            async with owner_app.state.sessionmaker() as db:
                assert (await db.get(Chapter, chapter["id"])).title == "先提交的章节"
            assert (await guest.get(chapter_url)).status_code == 404
        finally:
            release.set()
            projection_release.set()
            for task in (write, revoke):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_postgres_pending_content_checks_fresh_revision(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        chapter, _, _, _ = await _chapter(owner, base)
        async with guest_app.state.sessionmaker() as stale, owner_app.state.sessionmaker() as writer:
            old = await stale.get(Chapter, chapter["id"])
            old.content = "旧版待写正文"
            current = await writer.get(Chapter, chapter["id"])
            current.content, current.content_revision = "新提交正文", current.content_revision + 1
            await writer.commit()
            with pytest.raises(ConflictError) as error:
                await chapter_service.get_manual_chapter(
                    stale, user["id"], project["id"], chapter["id"], "content.edit", for_update=True,
                )
            assert error.value.extra["code"] == "stale_scene_revision"
            await stale.rollback()
        async with owner_app.state.sessionmaker() as db:
            assert (await db.get(Chapter, chapter["id"])).content == "新提交正文"
