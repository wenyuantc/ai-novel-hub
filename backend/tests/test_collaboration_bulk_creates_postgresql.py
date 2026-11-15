"""组合写入在 PostgreSQL 项目行锁后的授权重读和事务边界。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import event, select, text

from app.core.errors import BadRequestError, NotFoundError
from app.models import Chapter, ProjectCollaborator
from app.schemas.relationship import CharacterWithRelations
from app.services import project_lifecycle, relationship_service
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_bulk_creates import _case, _rows
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("kind", ["career", "organization", "relationship", "character"])
@pytest.mark.parametrize("revoke", [True, False])
async def test_bulk_write_rechecks_grant_after_project_lock(postgres_collaboration_apps, kind, revoke):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        ids = []
        for name in ("甲", "乙"):
            response = await owner.post(f"{base}/characters", json={"name": name})
            assert response.status_code == 201, response.text
            ids.append(response.json()["id"])
        career = await owner.post(f"{base}/careers", json={"name": "主修", "stages": [{"name": "初阶"}]})
        assert career.status_code == 201, career.text
        path, payload = _case(kind, "撤权之后", ids, career.json()["id"])
        before = await _rows(owner_app)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                if revoke:
                    member.status = "revoked"
                else:
                    member.role = "viewer"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(guest.post(base + path, json=payload))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == (404 if revoke else 403), result.text
            assert await _rows(owner_app) == before
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_character_nested_savepoint_failure_cannot_commit_partial_postgresql(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        first = await owner.post(f"{base}/careers", json={"name": "主修", "stages": [{"name": "初阶"}]})
        second = await owner.post(f"{base}/careers", json={"name": "副修", "kind": "sub", "stages": [{"name": "初阶"}]})
        assert first.status_code == second.status_code == 201
        payload = CharacterWithRelations(**{
            "character": {"name": "不得留存"},
            "careers": [
                {"career_id": first.json()["id"]},
                {"career_id": second.json()["id"], "stage_id": first.json()["stages"][0]["id"]},
            ],
        })
        before = await _rows(owner_app)
        async with guest_app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError):
                await relationship_service.create_character_with_relations(
                    db, user["id"], project["id"], payload, manual=True,
                )
            await db.commit()
        assert await _rows(owner_app) == before


async def test_character_revoke_after_lock_never_flushes_pending_entity(postgres_collaboration_apps):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, _ = await _setup(owner, guest)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder, guest_app.state.sessionmaker() as writer:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                member.status = "revoked"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                stale = await writer.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                assert stale.status == "accepted"
                chapter = Chapter(project_id=project["id"], title="撤权后不得写入")
                writer.add(chapter)
                flushes = []
                event.listen(writer.sync_session, "before_flush", lambda *_: flushes.append(True))
                pending = asyncio.create_task(relationship_service.create_character_with_relations(
                    writer, user["id"], project["id"],
                    CharacterWithRelations(character={"name": "撤权后人物"}), manual=True,
                ))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
                with pytest.raises(NotFoundError):
                    await asyncio.wait_for(pending, 6)
                assert flushes == [] and chapter.id is None and chapter in writer.new
                await writer.rollback()
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
