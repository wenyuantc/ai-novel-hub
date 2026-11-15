"""独立 PostgreSQL 验证规划预览响应前的权限和快照重验。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import update

from app.models import Chapter, Project, User
from app.services import planning_context_service
from tests import pg_support
from tests.test_collaboration_access_postgresql import _setup


@pytest.fixture
async def postgres_planning_preview_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(
        "AINOVEL_COLLAB_PLANNING_PREVIEW_POSTGRES_TEST_URL", "collab_planning", purpose="规划预览测试",
    )
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


@pytest.mark.parametrize("kind", ["chapter", "scene"])
async def test_postgres_planning_preview_rechecks_after_concurrent_changes(
    postgres_planning_preview_apps, monkeypatch, kind,
):
    owner_app, guest_app = postgres_planning_preview_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, guest_user, base = await _setup(owner, guest)
        chapter_response = await owner.post(f"{base}/chapters", json={"title": "第一章", "content": "正文"})
        assert chapter_response.status_code == 201, chapter_response.text
        chapter_id = chapter_response.json()["id"]
        scenes = await owner.get(f"{base}/chapters/{chapter_id}/scenes")
        assert scenes.status_code == 200, scenes.text
        source_id = chapter_id if kind == "chapter" else scenes.json()["items"][0]["id"]
        url = f"{base}/planning/units/context-preview"
        params = {"source_kind": kind, "source_id": source_id}
        assert (await guest.get(url, params=params)).status_code == 200
        original = planning_context_service.load_planning_context
        changes = ["planning_revision", "fields_revision", "ai_visibility_revision"]
        if kind == "scene":
            changes.append("content_revision")
        changes += ["role", "reinvite", "token", "revoke"]
        revision = 2
        for change in changes:
            ready, release = asyncio.Event(), asyncio.Event()

            async def gated(*args, _ready=ready, _release=release, **kwargs):
                value = await original(*args, **kwargs)
                _ready.set()
                await _release.wait()
                return value

            monkeypatch.setattr(planning_context_service, "load_planning_context", gated)
            pending = asyncio.create_task(guest.get(url, params=params))
            try:
                await asyncio.wait_for(ready.wait(), 15)
                if change == "role":
                    response = await owner.patch(f"{base}/collaborators/{guest_user['id']}", json={
                        "expected_revision": revision, "role": "viewer",
                    })
                    assert response.status_code == 200, response.text
                    revision = response.json()["revision"]
                elif change == "reinvite":
                    response = await owner.delete(f"{base}/collaborators/{guest_user['id']}", params={
                        "expected_revision": revision,
                    })
                    assert response.status_code == 204, response.text
                    invited = await owner.post(f"{base}/collaborators", json={"username": "pg-guest", "role": "viewer"})
                    assert invited.status_code == 201, invited.text
                    path = f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept"
                    accepted = await guest.post(path, json={"expected_revision": invited.json()["revision"]})
                    assert accepted.status_code == 200, accepted.text
                    revision = accepted.json()["revision"]
                elif change == "revoke":
                    response = await owner.delete(f"{base}/collaborators/{guest_user['id']}", params={
                        "expected_revision": revision,
                    })
                    assert response.status_code == 204, response.text
                else:
                    async with owner_app.state.sessionmaker() as db:
                        if change == "token":
                            await db.execute(update(User).where(User.id == guest_user["id"]).values(
                                token_version=User.token_version + 1,
                            ))
                        else:
                            model, identity = ((Chapter, chapter_id) if change == "content_revision"
                                               else (Project, project["id"]))
                            column = getattr(model, change)
                            await db.execute(update(model).where(model.id == identity).values({change: column + 1}))
                        await db.commit()
                release.set()
                response = await asyncio.wait_for(pending, 15)
                status = 404 if change == "revoke" else 401 if change == "token" else 409
                assert response.status_code == status, (change, response.text)
                if change == "token":
                    login = await guest.post("/api/v1/auth/login", json={
                        "username": "pg-guest", "password": "secret123",
                    })
                    assert login.status_code == 200, login.text
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
