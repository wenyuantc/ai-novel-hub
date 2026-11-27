import asyncio

import pytest
from sqlalchemy import select

from app.models import AdminAuditLog, FeaturePluginState
from app.services.audit_writer import AuditOperation
from tests.helpers import register
from tests.plugins.sample_plugin import SAMPLE, records
from tests.rbac_helpers import assign_roles, create_role

URL = "/api/v1/admin/plugins"
RECORDS = "/api/v1/plugins/example/records"


@pytest.fixture
def settings_overrides(monkeypatch):
    monkeypatch.setattr("app.plugins.builtins.REGISTERED", (SAMPLE,))
    return {}


async def switch(admin, revision, enabled):
    return await admin.patch(URL + "/example", json={"revision": revision, "enabled": enabled})


async def test_lifecycle_and_public_config(app, client, make_client, system_admin):
    denied = await client.get(RECORDS)
    assert denied.status_code == 403 and denied.json()["code"] == "plugin_disabled"
    await register(client)
    anonymous = make_client()
    listed = await system_admin.get(URL)
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json() == {
        "items": [
            {
                "id": "example",
                "name": "示例",
                "description": "测试插件",
                "version": "1",
                "enabled": False,
                "revision": 0,
                "updated_at": None,
            }
        ]
    }
    async with app.state.engine.begin() as conn:
        await conn.run_sync(records.metadata.create_all)
    async with app.state.sessionmaker() as db:
        from datetime import UTC, datetime

        db.add(FeaturePluginState(plugin_id="removed", revision=1, updated_at=datetime.now(UTC), enabled=True))
        await db.commit()
    assert (await anonymous.get("/api/v1/auth/config")).json()["enabled_plugins"] == []
    enabled = await switch(system_admin, 0, True)
    assert enabled.status_code == 200 and enabled.json()["revision"] == 1
    assert (await switch(system_admin, 1, True)).json() == enabled.json()
    assert (await anonymous.get("/api/v1/auth/config")).json()["enabled_plugins"] == ["example"]
    assert (await anonymous.get(RECORDS)).status_code == 401
    assert (await client.post(RECORDS, params={"value": "原始作品"})).status_code == 200
    assert (await switch(system_admin, 1, False)).json()["revision"] == 2
    assert (await client.get(RECORDS)).status_code == 403
    assert (await anonymous.get("/api/v1/auth/config")).json()["enabled_plugins"] == []
    assert (await switch(system_admin, 2, True)).json()["revision"] == 3
    assert (await client.get(RECORDS)).json() == ["原始作品"]
    async with app.state.sessionmaker() as db:
        logs = list(
            await db.scalars(
                select(AdminAuditLog).where(AdminAuditLog.target_type == "feature_plugin").order_by(AdminAuditLog.id)
            )
        )
        assert [row.action for row in logs] == [
            "feature_plugin.enable",
            "feature_plugin.disable",
            "feature_plugin.enable",
        ]
        assert all(row.target_label == "示例" and row.result == "success" for row in logs)


async def test_permissions_and_invalid_updates(client, system_admin):
    user = await register(client)
    denied = await client.get(URL)
    assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
    role = await create_role(system_admin, "只读插件", ["plugins.read"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    assert (await client.get(URL)).status_code == 200
    assert (await switch(client, 0, True)).status_code == 403
    assert (await system_admin.patch(URL + "/missing", json={"revision": 0, "enabled": True})).status_code == 404
    for body in (
        {"enabled": "yes", "revision": 0},
        {"enabled": True, "revision": -1},
        {"enabled": True, "revision": 0, "extra": 1},
    ):
        invalid = await system_admin.patch(URL + "/example", json=body)
        assert invalid.status_code == 422 and invalid.headers["cache-control"] == "no-store"
    assert (await client.get(URL)).json()["items"][0]["revision"] == 0


async def test_concurrent_first_write_and_audit_rollback(app, client, system_admin, monkeypatch):
    user = await register(client, "插件管理员")
    role = await create_role(system_admin, "管理插件", ["plugins.read", "plugins.update"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    first, second = await asyncio.gather(switch(system_admin, 0, True), switch(client, 0, True))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    stale = first if first.status_code == 409 else second
    assert stale.json()["code"] == "stale_revision"
    generate = AuditOperation._event

    def fail_success(self, result, **kwargs):
        if result == "success" and self.values["action"] == "feature_plugin.disable":
            raise RuntimeError("模拟审计失败")
        return generate(self, result, **kwargs)

    monkeypatch.setattr(AuditOperation, "_event", fail_success)
    assert (await switch(system_admin, 1, False)).status_code == 500
    async with app.state.sessionmaker() as db:
        row = await db.get(FeaturePluginState, "example")
        assert row.enabled and row.revision == 1
        assert (
            await db.scalar(
                select(AdminAuditLog.id).where(
                    AdminAuditLog.action == "feature_plugin.disable",
                    AdminAuditLog.result == "success",
                )
            )
            is None
        )
