"""人工记忆与关键词上传按原邀请轮次处理 PostgreSQL 跨连接变更。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import func, select

from app.models import KbDocument, StoryMemory, Task
from app.services import kb_service, memory_service
from tests.test_collaboration_access_postgresql import _setup

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize(("phase", "operation"), [("before", "memory"), ("after", "memory"), ("before", "upload")])
@pytest.mark.parametrize("transition", ["revoke", "downgrade", "reinvite"])
async def test_original_write_grant_survives_no_revocation_or_reinvite(
    postgres_collaboration_apps, monkeypatch, phase, operation, transition,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _, user, base = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        if phase == "before":
            original = kb_service.capture_content_write_actor

            async def gated_capture(db, actor_id, project_id):
                identity = await original(db, actor_id, project_id)
                reached.set()
                await release.wait()
                return identity

            monkeypatch.setattr(kb_service, "capture_content_write_actor", gated_capture)
        else:
            original = memory_service.create_memory

            async def gated_create(*args, **kwargs):
                result = await original(*args, **kwargs)
                reached.set()
                await release.wait()
                return result

            monkeypatch.setattr(memory_service, "create_memory", gated_create)
        if operation == "memory":
            pending = asyncio.create_task(guest.post(f"{base}/memories", json={
                "chapter_no": 1, "content": "原邀请记忆",
            }))
        else:
            pending = asyncio.create_task(guest.post(f"{base}/kb/documents", files={
                "file": ("原邀请.txt", "原邀请资料".encode()),
            }))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            if transition in {"revoke", "reinvite"}:
                changed = await owner.delete(f"{base}/collaborators/{user['id']}", params={"expected_revision": 2})
                assert changed.status_code == 204, changed.text
            if transition == "downgrade":
                changed = await owner.patch(f"{base}/collaborators/{user['id']}", json={
                    "expected_revision": 2, "role": "viewer",
                })
                assert changed.status_code == 200, changed.text
            if transition == "reinvite":
                again = await owner.post(f"{base}/collaborators", json={"username": "pg-guest", "role": "editor"})
                assert again.status_code == 201, again.text
                new_invitation = again.json()
                accepted = await guest.post(
                    f"/api/v1/collaboration/invitations/project/{new_invitation['invitation_id']}/accept",
                    json={"expected_revision": new_invitation["revision"]},
                )
                assert accepted.status_code == 200, accepted.text
            release.set()
            result = await asyncio.wait_for(pending, 8)
            assert result.status_code == (404 if transition == "revoke" else 403), result.text
            assert "原邀请记忆" not in result.text and "原邀请资料" not in result.text
            async with owner_app.state.sessionmaker() as db:
                count = await db.scalar(select(func.count(StoryMemory.id)))
                assert count == (1 if phase == "after" else 0)
                assert await db.scalar(select(func.count(KbDocument.id))) == 0
                assert await db.scalar(select(func.count(Task.id))) == 0
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
