"""作品设定人工写入与撤权竞争的 PostgreSQL 父行屏障。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.models import Career, Character, Foreshadow, Organization, ProjectCollaborator, SettingTimeline
from app.services import project_lifecycle
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("resource", ["character", "visibility", "timeline", "foreshadow", "career", "organization"])
@pytest.mark.parametrize("revoke", [True, False])
async def test_manual_story_write_rechecks_after_project_lock(postgres_collaboration_apps, resource, revoke):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        if resource in {"character", "visibility", "timeline"}:
            item = (await owner.post(f"{base}/characters", json={"name": "原角色", "profile": "原基础"})).json()
            if resource == "character":
                model, field, expected = Character, "name", "原角色"
                url, payload = f"{base}/characters/{item['id']}", {"name": "撤权后不能写"}
            elif resource == "visibility":
                async with owner_app.state.sessionmaker() as db:
                    (await db.get(Character, item['id'])).ai_mode = None
                    await db.commit()
                model, field, expected = Character, "always_in_context", False
                url, payload = f"{base}/characters/{item['id']}", {"always_in_context": True}
            else:
                timeline = (await owner.get(
                    f"{base}/setting-timelines", params={"character_id": item['id']},
                )).json()["items"][0]
                model, field, expected = SettingTimeline, "baseline", "原基础"
                item = timeline
                url = f"{base}/setting-timelines/{item['id']}/baseline"
                payload = {"expected_revision": item['revision'], "changes": {"profile": "撤权后不能写"}}
        elif resource == "foreshadow":
            item = (await owner.post(f"{base}/foreshadows", json={"title": "原伏笔"})).json()
            model, field, expected = Foreshadow, "title", "原伏笔"
            url, payload = f"{base}/foreshadows/{item['id']}", {"title": "撤权后不能写"}
        elif resource == "career":
            item = (await owner.post(f"{base}/careers", json={
                "name": "原职业", "stages": [{"name": "初阶"}],
            })).json()
            model, field, expected = Career, "name", "原职业"
            url, payload = f"{base}/careers/{item['id']}", {"name": "撤权后不能写"}
        else:
            item = (await owner.post(f"{base}/organizations", json={"name": "原组织"})).json()
            model, field, expected = Organization, "name", "原组织"
            url, payload = f"{base}/organizations/{item['id']}", {"name": "撤权后不能写"}
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project['id'])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project['id'], ProjectCollaborator.user_id == user['id'],
                ))
                if revoke:
                    member.status = "revoked"
                else:
                    member.role = "viewer"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(guest.patch(url, json=payload))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                await holder.commit()
            result = await asyncio.wait_for(pending, 6)
            assert result.status_code == (404 if revoke else 403), result.text
            async with owner_app.state.sessionmaker() as db:
                value = getattr(await db.get(model, item['id']), field)
                assert (value['profile'] if resource == "timeline" else value) == expected
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
