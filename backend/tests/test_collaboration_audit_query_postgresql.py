"""隔离 PostgreSQL 专项：验证协作审计查询的 PG 时间区间、字面转义、防枚举与清理隔离。"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import Base, CollaborationAuditEvent, User
from tests.helpers import create_project, register
from tests.schema_helpers import create_isolated_schema


@pytest.fixture
async def settings_overrides(monkeypatch):
    raw = os.getenv("AINOVEL_POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("缺少隔离 PostgreSQL 测试库 AINOVEL_POSTGRES_TEST_URL")
    url = make_url(raw)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.database != "ainovel_test"
        or url.query
    ):
        raise ValueError("AINOVEL_POSTGRES_TEST_URL 必须指向本机专用 ainovel_test 测试库")
    schema = f"collab_audit_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
        try:
            def make_scoped_engine(database_url):
                assert make_url(database_url) == url
                return create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})

            monkeypatch.setattr("app.main.create_engine", make_scoped_engine)
            monkeypatch.setattr(Base.metadata, "create_all", create_isolated_schema)
            yield {"database_url": raw}
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(DropSchema(schema, cascade=True))
    finally:
        await admin_engine.dispose()


async def test_postgresql_collaboration_audit_query_boundaries_redaction_and_cleanup(
    app, client, make_client, system_admin,
):
    owner = await register(client, "pg_owner_100%\\a")
    guest_client = make_client()
    guest = await register(guest_client, "pg_guest_100%\\a")
    await register(make_client(), "pg_guestX100YYa")
    disabled_client = make_client()
    disabled = await register(disabled_client, "pg_disabled")
    stranger_client = make_client()
    await register(stranger_client, "pg_stranger")

    async with app.state.sessionmaker() as db:
        (await db.get(User, disabled["id"])).is_active = False
        await db.commit()

    project = await create_project(client, title="PG_50%\\作品")
    base = f"/api/v1/projects/{project['id']}/collaborators"
    audit_url = f"{base}/audit-logs"

    inv = (await client.post(base, json={"username": "pg_guest_100%\\a", "role": "viewer"})).json()
    assert (await guest_client.post(
        f"/api/v1/collaboration/invitations/project/{inv['invitation_id']}/accept", json={"expected_revision": 1},
    )).status_code == 200
    assert (await client.post(base, json={"username": "pg_guestX100YYa", "role": "viewer"})).status_code == 201
    assert (await client.post(base, json={"username": "pg_disabled", "role": "viewer"})).status_code == 400
    assert (await client.post(base, json={"username": "pg_missing", "role": "viewer"})).status_code == 400
    stranger_res = await stranger_client.post(base, json={"username": "pg_guest_100%\\a", "role": "editor"})
    assert stranger_res.status_code == 404
    assert (await stranger_client.post(
        f"/api/v1/collaboration/invitations/project/{uuid4()}/accept", json={"expected_revision": 1},
    )).status_code == 404

    # 作者侧排除陌生人越权，并脱敏 disabled / missing 邀请。
    owner_all = (await client.get(audit_url)).json()
    assert owner_all["total"] == 5
    assert (await client.get(audit_url, params={"member_q": "pg_disabled"})).json()["total"] == 0
    assert (await client.get(audit_url, params={"member_q": "pg_stranger"})).json()["total"] == 0

    # PostgreSQL ILIKE 字面转义 %、_、\。
    escaped = (await client.get(audit_url, params={"member_q": "_100%\\a"})).json()
    assert escaped["total"] == 5  # owner 用户名也含 _100%\a，但 guest_100%\a 专有前缀只命中 2 条
    guest_escaped = (await client.get(audit_url, params={"member_q": "guest_100%\\a"})).json()
    assert guest_escaped["total"] == 2
    assert all(i["target_user_id"] == guest["id"] for i in guest_escaped["items"])

    # PostgreSQL 原生 TIMESTAMPTZ 半开区间 [from_at, to_at)。
    async with app.state.sessionmaker() as db:
        e_start = CollaborationAuditEvent(
            operation_id=str(uuid4()), occurred_at=datetime(2026, 6, 1, 1, 0, 0, tzinfo=UTC),
            scope="project", resource_id=project["id"], resource_label="PG时间",
            actor_user_id=owner["id"], actor_username=owner["username"],
            action="modify", result="success", summary={},
        )
        e_end = CollaborationAuditEvent(
            operation_id=str(uuid4()), occurred_at=datetime(2026, 6, 1, 2, 0, 0, tzinfo=UTC),
            scope="project", resource_id=project["id"], resource_label="PG时间",
            actor_user_id=owner["id"], actor_username=owner["username"],
            action="revoke", result="success", summary={},
        )
        db.add_all([e_start, e_end])
        await db.commit()
        start_id = e_start.id

    ranged = (await client.get(
        audit_url, params={"from_at": "2026-06-01T09:00:00+08:00", "to_at": "2026-06-01T10:00:00+08:00"},
    )).json()
    assert ranged["total"] == 1 and ranged["items"][0]["id"] == start_id

    # 管理员全站查询含越权与 resource_id=0，且管理日志清理不影响协作审计。
    admin_url = "/api/v1/admin/audit-logs/collaboration"
    admin_all = (await system_admin.get(admin_url)).json()
    assert admin_all["total"] == 9
    assert (await system_admin.get(admin_url, params={"resource_id": 0})).json()["total"] == 1
    assert (await system_admin.get(admin_url, params={"resource_q": "PG_50%\\"})).json()["total"] == 5

    # 制造一条可清理的管理操作日志并清理。
    reenable = await system_admin.patch(f"/api/v1/admin/users/{disabled['id']}", json={"is_active": True})
    assert reenable.status_code == 200
    preview = (await system_admin.post(
        "/api/v1/admin/audit-logs/cleanup-preview",
        json={"from_at": "2020-01-01T00:00:00Z", "to_at": "2099-01-01T00:00:00Z"},
    )).json()
    assert preview["count"] >= 1
    cleaned = await system_admin.post(
        "/api/v1/admin/audit-logs/cleanup",
        json={"preview_token": preview["preview_token"], "confirm": True},
    )
    assert cleaned.status_code == 200
    assert (await system_admin.get(admin_url)).json()["total"] == 9
