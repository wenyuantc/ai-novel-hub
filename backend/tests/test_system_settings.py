"""六项白名单、来源/恢复、真实注册、权限、并发编辑及安全审计。"""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.models import AdminAuditLog, SystemSettings
from app.schemas.audit import AuditSummary
from app.schemas.system_settings import MAX_OPEN_API_RATE_LIMIT_PER_MINUTE, SYSTEM_SETTING_KEYS
from app.services.audit_writer import AuditOperation
from tests.helpers import make_settings, register

URL = "/api/v1/admin/system-settings"


async def save_settings(admin, **changes):
    current = (await admin.get(URL)).json()
    result = await admin.patch(URL, json={"revision": current["revision"], "changes": changes})
    assert result.status_code == 200, result.text
    return result.json()


def items(body):
    return {row["key"]: row for row in body["items"]}


async def test_safe_read_is_pure_and_sources_are_accurate(app, system_admin):
    response = await system_admin.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["revision"] == 0 and body["updated_at"] is None
    assert set(items(body)) == set(SYSTEM_SETTING_KEYS)
    data = items(body)
    assert data["registration_enabled"]["source"] == "deployment"
    assert data["auto_analyze_enabled"]["value"] is False
    assert data["auto_analyze_enabled"]["default_value"] is True
    assert data["mcp_enabled"]["source"] == "default"
    assert all(not row["overridden"] and not row["requires_restart"] for row in body["items"])
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(SystemSettings.id))) == 0
        assert await db.scalar(select(func.count(AdminAuditLog.id))) == 0
    for secret in (app.state.settings.jwt_secret, app.state.settings.encryption_key, "database_url", "admin_password"):
        assert secret not in response.text


async def test_false_partial_and_reset_immediately_control_real_registration(app, system_admin, make_client):
    original = app.state.settings
    body = await save_settings(system_admin, registration_enabled=False, task_max_per_user=7)
    data = items(body)
    assert data["registration_enabled"]["value"] is False
    assert data["registration_enabled"]["source"] == "admin"
    assert data["task_max_per_user"]["value"] == 7  # 大于全局仍合法，实际受全局限制。
    assert app.state.settings is original is app.state.task_engine.ctx.settings
    anonymous = make_client()
    public = await anonymous.get("/api/v1/auth/config")
    assert public.json() == {"registration_enabled": False, "mcp_enabled": True}
    assert public.headers["cache-control"] == "no-store"
    blocked = await anonymous.post("/api/v1/auth/register", json={"username": "new_user", "password": "secret123"})
    assert blocked.status_code == 403
    assert (await system_admin.get("/api/v1/auth/me")).status_code == 200
    created = await system_admin.post("/api/v1/admin/users", json={"username": "managed_user", "password": "secret123"})
    assert created.status_code == 201
    body = await save_settings(system_admin, registration_enabled=None)
    assert items(body)["registration_enabled"]["value"] is True
    assert items(body)["registration_enabled"]["source"] == "deployment"
    assert items(body)["task_max_per_user"]["value"] == 7
    assert (await anonymous.post(
        "/api/v1/auth/register", json={"username": "new_user", "password": "secret123"},
    )).status_code == 201
    async with app.state.sessionmaker() as db:
        row = await db.get(SystemSettings, 1)
        assert row.overrides == {"task_max_per_user": 7}
        logs = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "system_settings.update")))
        assert len(logs) == 2 and all(log.actor_username == "settings_admin" for log in logs)
        reset = logs[-1].summary["settings_changes"][0]
        assert reset["before_override"] is False and reset["after_override"] is None
        assert reset["before_effective"] is False and reset["after_effective"] is True
        assert reset["after_source"] == "deployment"


@pytest.mark.parametrize("bad", [
    {"task_max_concurrency": 0}, {"task_max_per_user": -1}, {"open_api_rate_limit_per_minute": 0},
    {"task_max_concurrency": True}, {"task_max_per_user": 1.5}, {"open_api_rate_limit_per_minute": "5"},
    {"open_api_rate_limit_per_minute": MAX_OPEN_API_RATE_LIMIT_PER_MINUTE + 1},
    {"open_api_rate_limit_per_minute": 10**30},
    {"registration_enabled": 0}, {"mcp_enabled": "false"}, {"auto_analyze_enabled": "true"},
    {"database_url": "credential-injection"}, {"jwt_secret": "sensitive-input"}, {},
])
async def test_invalid_batch_is_rejected_without_any_write(app, system_admin, bad):
    result = await system_admin.patch(URL, json={"revision": 0, "changes": bad})
    assert result.status_code == 422, result.text
    assert "sensitive-input" not in result.text
    assert app.state.settings.registration_enabled is True
    async with app.state.sessionmaker() as db:
        assert await db.get(SystemSettings, 1) is None
        assert await db.scalar(select(func.count(AdminAuditLog.id))) == 0


async def test_one_invalid_field_does_not_apply_other_valid_fields(app, system_admin):
    result = await system_admin.patch(URL, json={
        "revision": 0, "changes": {"registration_enabled": False, "task_max_concurrency": 0},
    })
    assert result.status_code == 422
    assert app.state.settings.registration_enabled is True
    assert (await system_admin.get(URL)).json()["revision"] == 0


async def test_rate_limit_overflow_rejects_whole_batch_without_changing_runtime_or_storage(app, system_admin):
    result = await system_admin.patch(URL, json={
        "revision": 0,
        "changes": {"registration_enabled": False, "open_api_rate_limit_per_minute": 10**30},
    })
    assert result.status_code == 422
    assert app.state.settings.registration_enabled is True
    assert app.state.settings.open_api_rate_limit_per_minute == 120
    async with app.state.sessionmaker() as db:
        assert await db.get(SystemSettings, 1) is None
        assert await db.scalar(select(func.count(AdminAuditLog.id))) == 0


@pytest.mark.parametrize("limit", [MAX_OPEN_API_RATE_LIMIT_PER_MINUTE + 1, 10**30])
def test_unexecutable_deployment_rate_limit_fails_fast_without_echoing_input(tmp_path, limit):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as failed:
        make_settings(tmp_path, open_api_rate_limit_per_minute=limit)
    assert "open_api_rate_limit_per_minute" in str(failed.value)
    assert str(limit) not in str(failed.value)
    assert make_settings(
        tmp_path, open_api_rate_limit_per_minute=MAX_OPEN_API_RATE_LIMIT_PER_MINUTE,
    ).open_api_rate_limit_per_minute == MAX_OPEN_API_RATE_LIMIT_PER_MINUTE


async def test_all_management_routes_require_admin(app, client, system_admin, make_client):
    body = {"revision": 0, "changes": {"registration_enabled": False}}
    for method in ("GET", "PATCH"):
        assert (await client.request(method, URL, json=body if method == "PATCH" else None)).status_code == 401
    await register(client, "ordinary")
    for method in ("GET", "PATCH"):
        assert (await client.request(method, URL, json=body if method == "PATCH" else None)).status_code == 403
    public = await make_client().get("/api/v1/auth/config")
    assert set(public.json()) == {"registration_enabled", "mcp_enabled"}
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(AdminAuditLog.id))) == 0


async def test_concurrent_edits_have_one_winner_and_stale_revision_409(system_admin):
    results = await asyncio.gather(*(
        system_admin.patch(URL, json={"revision": 0, "changes": {"task_max_concurrency": value}})
        for value in (5, 6)
    ))
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(result.json() for result in results if result.status_code == 200)
    current = (await system_admin.get(URL)).json()
    assert current == winner
    assert current["revision"] == 1


async def test_overrides_survive_new_application_and_reset_tracks_new_deployment(
    app, system_admin, tmp_path, fake_llm,
):
    await save_settings(system_admin, registration_enabled=False, task_max_concurrency=9)
    settings = make_settings(tmp_path, registration_enabled=True, task_max_concurrency=3)
    second = create_app(settings, fake_llm.factory, start_task_engine=False)
    try:
        snapshot = await second.state.system_settings.load()
        assert items(snapshot.model_dump())["registration_enabled"]["value"] is False
        assert settings.task_max_concurrency == 9
        assert second.state.settings is second.state.task_engine.ctx.settings
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://testserver") as c:
            assert (await c.post(
                "/api/v1/auth/login", json={"username": "settings_admin", "password": "secret123"},
            )).status_code == 200
            reset = await save_settings(c, task_max_concurrency=None)
            assert items(reset)["task_max_concurrency"]["value"] == 3
    finally:
        await second.state.system_settings.close()
        await second.state.engine.dispose()


async def test_audit_failure_rolls_back_without_publishing_or_leaking(app, system_admin, monkeypatch, caplog):
    original = AuditOperation._event

    def broken(self, result, **kwargs):
        if self.values["action"] == "system_settings.update" and result == "success":
            raise RuntimeError("SECRET_SQL_PARAMETER")
        return original(self, result, **kwargs)

    monkeypatch.setattr(AuditOperation, "_event", broken)
    result = await system_admin.patch(URL, json={"revision": 0, "changes": {"registration_enabled": False}})
    assert result.status_code == 500 and "SECRET_SQL_PARAMETER" not in result.text + caplog.text
    assert app.state.settings.registration_enabled is True
    async with app.state.sessionmaker() as db:
        assert await db.get(SystemSettings, 1) is None
        logs = list(await db.scalars(select(AdminAuditLog)))
        assert [row.result for row in logs] == ["failure"]
        assert logs[0].summary["settings_changes"] == []


async def test_commit_confirmation_lost_reloads_success_and_never_adds_opposite_audit(
    app, system_admin, monkeypatch, caplog,
):
    original = AsyncSession.commit
    lost = False

    async def commit(db):
        nonlocal lost
        target = any(isinstance(row, AdminAuditLog) and row.action == "system_settings.update"
                     for row in db.identity_map.values())
        await original(db)
        if target and not lost:
            lost = True
            raise RuntimeError("SECRET_ACK_FAILURE")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    result = await system_admin.patch(URL, json={"revision": 0, "changes": {"registration_enabled": False}})
    assert result.status_code == 500 and lost
    assert "SECRET_ACK_FAILURE" not in result.text + caplog.text
    assert app.state.settings.registration_enabled is False
    current = (await system_admin.get(URL)).json()
    assert current["revision"] == 1 and items(current)["registration_enabled"]["value"] is False
    async with app.state.sessionmaker() as db:
        assert [row.result for row in await db.scalars(select(AdminAuditLog))] == ["success"]


async def test_cancelled_http_caller_does_not_split_commit_and_publish(app, system_admin, monkeypatch):
    original = AuditOperation.commit
    reached, release = asyncio.Event(), asyncio.Event()

    async def commit(self, *args, **kwargs):
        if self.values["action"] == "system_settings.update":
            reached.set()
            await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AuditOperation, "commit", commit)
    pending = asyncio.create_task(system_admin.patch(
        URL, json={"revision": 0, "changes": {"registration_enabled": False}},
    ))
    await asyncio.wait_for(reached.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    await app.state.system_settings.close()
    assert app.state.settings.registration_enabled is False
    async with app.state.sessionmaker() as db:
        assert (await db.get(SystemSettings, 1)).overrides == {"registration_enabled": False}
        assert await db.scalar(select(func.count(AdminAuditLog.id))) == 1


async def test_uncertain_commit_blocks_further_writes_until_background_recovery(
    app, system_admin, monkeypatch,
):
    manager = app.state.system_settings
    commit_original, reload_original = AsyncSession.commit, manager._reload
    committed, allow_read = False, False
    recovered = asyncio.Event()

    async def commit(db):
        nonlocal committed
        target = any(isinstance(row, AdminAuditLog) and row.action == "system_settings.update"
                     for row in db.identity_map.values())
        await commit_original(db)
        if target and not committed:
            committed = True
            raise RuntimeError("提交确认丢失")

    async def reload():
        if committed and not allow_read:
            raise RuntimeError("数据库暂不可确认")
        output = await reload_original()
        if committed:
            recovered.set()
        return output

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(manager, "_reload", reload)
    failed = await system_admin.patch(URL, json={"revision": 0, "changes": {"registration_enabled": False}})
    assert failed.status_code == 500 and manager._uncertain
    blocked = await system_admin.patch(URL, json={"revision": 1, "changes": {"registration_enabled": True}})
    assert blocked.status_code == 500
    allow_read = True
    await asyncio.wait_for(recovered.wait(), 5)
    assert app.state.settings.registration_enabled is False and not manager._uncertain
    assert (await system_admin.get(URL)).json()["revision"] == 1
    async with app.state.sessionmaker() as db:
        assert (await db.get(SystemSettings, 1)).overrides == {"registration_enabled": False}
        assert [row.result for row in await db.scalars(select(AdminAuditLog))] == ["success"]


async def test_bad_persistent_data_fails_safe_instead_of_enabling_defaults(app, system_admin):
    from datetime import UTC, datetime

    async with app.state.sessionmaker() as db:
        db.add(SystemSettings(
            id=1, revision=1, overrides={"mcp_enabled": "false"},
            last_operation_id="00000000-0000-0000-0000-000000000001", updated_at=datetime.now(UTC),
        ))
        await db.commit()
    result = await system_admin.get(URL)
    assert result.status_code == 500
    assert "mcp_enabled" not in result.text


def test_setting_audit_summary_is_typed_bounded_and_action_scoped():
    from pydantic import ValidationError

    change = dict(
        key="mcp_enabled", before_override=None, after_override=False, before_effective=True,
        after_effective=False, before_source="default", after_source="admin",
    )
    assert AuditSummary(settings_changes=[change]).settings_changes[0].after_effective is False
    for invalid in ({**change, "key": "jwt_secret"}, {**change, "after_effective": 1}, {**change, "raw": "secret"}):
        with pytest.raises(ValidationError):
            AuditSummary(settings_changes=[invalid])
    with pytest.raises(ValidationError):
        AuditSummary(settings_changes=[change] * 7)
    assert "secret" not in json.dumps(AuditSummary(settings_changes=[change]).model_dump())
    operation = AuditOperation(None, 1, "user.update", "user", 1)
    operation.summary = AuditSummary(settings_changes=[change])
    with pytest.raises(ValueError, match="仅允许"):
        operation._event("success")
