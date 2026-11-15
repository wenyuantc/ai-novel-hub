"""可见性人工变更的真实 PostgreSQL 撤权、项目行和出站门禁竞争。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.ai.providers import OpenAICompatProvider
from app.models import Character, Project, ProjectCollaborator, SettingTextField, SettingTimeline
from app.models.setting_fields import SettingFieldDefinition
from app.services import ai_dispatch, project_lifecycle
from app.services.ai_dispatch import VisibilityChangedError, guard_provider, read_visibility_stamp
from app.services.audit_writer import AuditOperation
from tests import pg_support
from tests.helpers import create_project
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row
from tests.test_setting_fields import _definition, _fields, _versions

OPERATIONS = ("settings", "create_hidden", "update_hidden", "legacy_visibility", "legacy_binding")


@pytest.fixture
async def postgres_visibility_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    # PostgreSQL advisory locks 不按 schema 隔离：并行运行须用专库，标准库仅供串行回退。
    url = pg_support.collab_test_url(
        "AINOVEL_COLLAB_VISIBILITY_POSTGRES_TEST_URL", "collab_visibility", purpose="可见性竞争测试",
    )
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _prepare(owner, base, operation):
    created = await owner.post(f"{base}/characters", json={"name": "原角色", "profile": "不应改变"})
    assert created.status_code == 201, created.text
    character = created.json()
    entry = f"{base}/setting-entries/character/{character['id']}"
    if operation == "settings":
        return "PATCH", f"{entry}/ai-settings", {"expected_ai_visibility_revision": 0, "ai_mode": "never"}, 200
    if operation == "create_hidden":
        return "POST", f"{base}/setting-field-definitions", {
            "expected_fields_revision": 0, "label": "秘密", "field_type": "text",
            "applicable_types": ["character"], "ai_hidden": True,
        }, 201
    definition = await _definition(owner, base, field_type="text", ai_hidden=operation == "legacy_binding")
    did = definition["item"]["id"]
    if operation == "update_hidden":
        return "PATCH", f"{base}/setting-field-definitions/{did}", {
            "expected_fields_revision": 1, "ai_hidden": True,
        }, 200
    fields = await _fields(owner, base, character)
    legacy = await owner.post(f"{base}/setting-timelines/{fields['timeline_id']}/text-fields", json={
        "expected_revision": fields["timeline_revision"], "label": "身份", "base_value": "原值",
    })
    assert legacy.status_code == 201, legacy.text
    field_id = legacy.json()["item"]["id"]
    path = f"{entry}/legacy-fields/{field_id}"
    if operation == "legacy_visibility":
        return "PATCH", f"{path}/ai-visibility", {
            "expected_timeline_revision": legacy.json()["timeline_revision"],
            "expected_ai_visibility_revision": 0, "ai_hidden": True,
        }, 200
    current = await _fields(owner, base, character)
    return "POST", f"{path}/binding", {**_versions(current), "definition_id": did}, 200


async def _cases(owner, guest):
    """一套隔离 schema 中为每项操作建独立作品，避免重复建立全部索引。"""
    project, user, base = await _setup(owner, guest)
    for index, operation in enumerate(OPERATIONS):
        if index:
            project = await create_project(owner, title=f"可见性竞争作品 {index}")
            base = f"/api/v1/projects/{project['id']}"
            invitation = await owner.post(f"{base}/collaborators", json={"username": "pg-guest", "role": "editor"})
            assert invitation.status_code == 201, invitation.text
            accepted = await guest.post(
                f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
                json={"expected_revision": invitation.json()["revision"]},
            )
            assert accepted.status_code == 200, accepted.text
        yield operation, project, user, base


async def _state(app, project_id):
    async with app.state.sessionmaker() as db:
        project = (await db.execute(select(
            Project.fields_revision, Project.ai_visibility_revision, Project.ai_history_epoch,
        ).where(Project.id == project_id))).one()
        characters = (await db.execute(select(Character.id, Character.ai_mode).where(
            Character.project_id == project_id,
        ).order_by(Character.id))).all()
        definitions = (await db.execute(select(SettingFieldDefinition.id, SettingFieldDefinition.ai_hidden).where(
            SettingFieldDefinition.project_id == project_id,
        ).order_by(SettingFieldDefinition.id))).all()
        fields = (await db.execute(select(
            SettingTextField.id, SettingTextField.definition_id, SettingTextField.ai_hidden,
        ).join(SettingTimeline, SettingTimeline.id == SettingTextField.timeline_id).where(
            SettingTimeline.project_id == project_id,
        ).order_by(SettingTextField.id))).all()
        return tuple(project), characters, definitions, fields


def _gated_commit(original, actor_id, holder, reached, release):
    async def gated(self, *args, **kwargs):
        if self.actor_id == actor_id and self.values["action"] == "project.update":
            await self.db.flush()
            holder["pid"] = await self.db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
        return await original(self, *args, **kwargs)

    return gated


@pytest.mark.parametrize("revoke", [True, False])
async def test_visibility_write_rechecks_project_row_after_revoke_or_downgrade(
    postgres_visibility_apps, revoke,
):
    owner_app, guest_app = postgres_visibility_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        async for operation, project, user, base in _cases(owner, guest):
            method, path, payload, _ = await _prepare(owner, base, operation)
            before = await _state(owner_app, project["id"])
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
                    pending = asyncio.create_task(guest.request(method, path, json=payload))
                    await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, pid), 6)
                    await _assert_blocked_on_project_row(guest_app.state.sessionmaker, pid)
                    await holder.commit()
                result = await asyncio.wait_for(pending, 6)
                assert result.status_code == (404 if revoke else 403), f"{operation}: {result.text}"
                assert await _state(owner_app, project["id"]) == before, operation
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_visibility_write_commits_before_waiting_revoke(postgres_visibility_apps, monkeypatch):
    owner_app, guest_app = postgres_visibility_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        original = AuditOperation.commit
        async for operation, _project, user, base in _cases(owner, guest):
            method, path, payload, success = await _prepare(owner, base, operation)
            reached, release = asyncio.Event(), asyncio.Event()
            holder = {}
            monkeypatch.setattr(AuditOperation, "commit", _gated_commit(original, user["id"], holder, reached, release))
            writing = asyncio.create_task(guest.request(method, path, json=payload))
            revoking = None
            try:
                await asyncio.wait_for(reached.wait(), 6)
                revoking = asyncio.create_task(owner.delete(
                    f"{base}/collaborators/{user['id']}", params={"expected_revision": 2},
                ))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 6)
                release.set()
                result = await asyncio.wait_for(writing, 6)
                assert result.status_code == success, f"{operation}: {result.text}"
                assert (await asyncio.wait_for(revoking, 6)).status_code == 204
                assert (await guest.get(base)).status_code == 404
            finally:
                release.set()
                for task in (writing, revoking):
                    if task is not None and not task.done():
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task


async def test_visibility_write_waits_for_real_provider_send(
    postgres_visibility_apps, monkeypatch,
):
    owner_app, guest_app = postgres_visibility_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
        async for operation, project, _, base in _cases(owner, guest):
            method, path, payload, success = await _prepare(owner, base, operation)
            before = await _state(owner_app, project["id"])
            async with owner_app.state.sessionmaker() as db:
                stamp = await read_visibility_stamp(db, project["id"])
            async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
                provider = guard_provider(
                    OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, owner_app.state.sessionmaker,
                )
                sending = asyncio.create_task(_collect(provider.generate_stream(
                    [{"role": "user", "content": "设定"}], "test",
                )))
                try:
                    await asyncio.wait_for(received.wait(), 5)
                    blocked = await asyncio.wait_for(guest.request(method, path, json=payload), 3)
                    assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", (
                        f"{operation}: {blocked.text}"
                    )
                    assert await _state(owner_app, project["id"]) == before, operation
                    headers.set()
                    body.set()
                    assert await asyncio.wait_for(sending, 5) == ["ok"]
                    saved = await guest.request(method, path, json=payload)
                    assert saved.status_code == success, f"{operation}: {saved.text}"
                    with pytest.raises(VisibilityChangedError):
                        await _collect(provider.generate_stream([{"role": "user", "content": "设定"}], "test"))
                    assert len(requests) == 1
                finally:
                    headers.set()
                    body.set()
                    if not sending.done():
                        sending.cancel()
                    await asyncio.gather(sending, return_exceptions=True)
