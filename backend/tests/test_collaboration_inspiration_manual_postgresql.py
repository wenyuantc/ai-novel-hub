"""本人灵感草稿应用在 PostgreSQL 父行锁后重读协作授权。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.models import Project, ProjectCollaborator
from app.services import project_lifecycle
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_inspiration_manual import _personal_draft
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("revoke", [True, False])
async def test_apply_rechecks_member_after_waiting_for_project_lock(postgres_collaboration_apps, revoke):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, _ = await _setup(owner, guest)
        session_id = await _personal_draft(guest, "个人草稿")
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
                pending = asyncio.create_task(guest.post(
                    f"/api/v1/inspiration/sessions/{session_id}/apply",
                    json={"project_id": project["id"], "fields": ["theme"]},
                ))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 8)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            response = await asyncio.wait_for(pending, 8)
            assert response.status_code == (404 if revoke else 403), response.text
            async with owner_app.state.sessionmaker() as db:
                assert (await db.get(Project, project["id"])).theme == project["theme"]
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
