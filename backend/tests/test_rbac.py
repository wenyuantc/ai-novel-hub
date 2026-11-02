"""真实HTTP操作权限、多角色组合、目标保护与无隐式扩权。"""

import pytest
from sqlalchemy import select

from app.core.permissions import BUILTIN_ROLES, PERMISSION_CODES
from app.models import AdminAuditLog, User
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, authorization_revision, create_role, set_quota

ROLES = "/api/v1/admin/roles"
USERS = "/api/v1/admin/users"


async def test_catalog_and_fixed_roles_are_explicit(system_admin, client):
    user = await register(client)
    assert user["effective_permissions"] == [] and not user["is_super_admin"]
    assert user["roles"][0]["system_key"] == "user"
    denied = await client.get(ROLES)
    assert denied.status_code == 403 and denied.json()["code"] == "permission_denied"
    response = await system_admin.get("/api/v1/admin/permissions")
    assert response.headers["cache-control"] == "no-store"
    items = response.json()["items"]
    assert {item["code"] for item in items} == set(PERMISSION_CODES)
    assert len(items) == 37
    roles = (await system_admin.get(ROLES)).json()["items"]
    assert {r["system_key"] for r in roles} == set(BUILTIN_ROLES)
    for role in roles:
        assert set(role["permissions"]) == set(BUILTIN_ROLES[role["system_key"]][2])
        payload = {
            "authorization_revision": role["authorization_revision"],
            "revision": role["revision"],
            "name": "篡改",
        }
        bad = await system_admin.patch(f"{ROLES}/{role['id']}", json=payload)
        assert bad.status_code == 409 and bad.json()["code"] == "builtin_role_readonly"


@pytest.mark.parametrize("permissions", [["*"], ["roles.update"], ["tasks.cancel"], ["users.read", "users.read"]])
async def test_unknown_duplicate_or_missing_dependency_permissions_rejected(system_admin, permissions):
    response = await system_admin.post(ROLES, json={"name": "非法角色", "permissions": permissions})
    assert response.status_code == 422


async def test_multirole_union_and_role_edit_revokes_without_logout(system_admin, client, make_client):
    user = await register(client)
    owner = make_client()
    await register(owner, "writer")
    project = await create_project(owner)
    reading = await create_role(system_admin, "作品只读", ["projects.read"])
    tasks = await create_role(system_admin, "任务只读", ["tasks.read"])
    await assign_roles(system_admin, user["id"], [reading["id"], tasks["id"]])
    me = (await client.get("/api/v1/auth/me")).json()
    assert set(me["effective_permissions"]) == {"projects.read", "tasks.read"}
    assert not me["is_admin"] and not me["is_super_admin"]
    assert (await client.get(f"/api/v1/projects/{project['id']}")).status_code == 200
    assert (await client.get("/api/v1/admin/tasks")).status_code == 200
    assert (await client.get(USERS)).status_code == 403
    assert (await client.patch(f"/api/v1/projects/{project['id']}", json={"title": "越权"})).status_code == 403
    updated = await system_admin.patch(f"{ROLES}/{reading['id']}", json={
        "authorization_revision": await authorization_revision(system_admin),
        "revision": reading["revision"], "permissions": [],
    })
    assert updated.status_code == 200, updated.text
    assert (await client.get("/api/v1/admin/projects")).status_code == 403
    assert (await client.get("/api/v1/auth/me")).json()["id"] == user["id"]
    assert (await client.get("/api/v1/admin/tasks")).status_code == 200
    await assign_roles(system_admin, user["id"], [])
    assert (await client.get("/api/v1/admin/tasks")).status_code == 403
    assert (await client.post("/api/v1/projects", json={"title": "自己的作品"})).status_code == 201


async def test_copy_super_is_not_super_and_in_use_role_cannot_be_deleted(system_admin, client):
    user = await register(client)
    super_role = next(r for r in (await system_admin.get(ROLES)).json()["items"] if r["system_key"] == "super_admin")
    response = await system_admin.post(ROLES, json={
        "name": "全部可委派权限", "source_role_id": super_role["id"], "source_revision": super_role["revision"],
    })
    assert response.status_code == 201, response.text
    role = response.json()
    assert role["system_key"] is None and not role["is_builtin"]
    assert set(role["permissions"]) == set(PERMISSION_CODES)
    await assign_roles(system_admin, user["id"], [role["id"]])
    assert not (await client.get("/api/v1/auth/me")).json()["is_super_admin"]
    assert (await client.get(ROLES)).status_code == 403
    response = await system_admin.delete(f"{ROLES}/{role['id']}", params={
        "authorization_revision": await authorization_revision(system_admin), "revision": role["revision"],
    })
    assert response.status_code == 409 and response.json()["code"] == "role_in_use"
    await assign_roles(system_admin, user["id"], [])
    response = await system_admin.delete(f"{ROLES}/{role['id']}", params={
        "authorization_revision": await authorization_revision(system_admin), "revision": role["revision"],
    })
    assert response.status_code == 204


async def test_legacy_boolean_role_field_is_not_an_authorization_backdoor(system_admin, client):
    user = await register(client)
    for admin in (client, system_admin):
        bad = await admin.post(USERS, json={"username": "injected", "password": "secret123", "is_admin": True})
        assert bad.status_code in (403, 422)
        bad = await admin.patch(f"{USERS}/{user['id']}", json={"is_admin": False, "is_active": False})
        assert bad.status_code in (403, 422)
    assert (await system_admin.get(f"{USERS}/{user['id']}")).json()["is_active"]


async def test_management_targets_are_protected_even_if_disabled(app, system_admin, client, make_client):
    actor = await register(client, "account_operator")
    target_client = make_client()
    target = await register(target_client, "managed_target")
    ordinary_client = make_client()
    ordinary = await register(ordinary_client, "ordinary_target")
    permission = ["users.read", "users.status", "users.reset_password", "users.delete", "quotas.read", "quotas.update"]
    role = await create_role(system_admin, "普通账号维护", permission)
    reader = await create_role(system_admin, "目标只读管理", ["tasks.read"])
    await assign_roles(system_admin, actor["id"], [role["id"]])
    await assign_roles(system_admin, target["id"], [reader["id"]])
    assert (await client.patch(f"{USERS}/{ordinary['id']}", json={"is_active": False})).status_code == 200
    for active in (True, False):
        if not active:
            assert (await system_admin.patch(f"{USERS}/{target['id']}", json={"is_active": False})).status_code == 200
        for method, suffix, body in [
            ("PATCH", "", {"is_active": False}), ("POST", "/reset-password", {"new_password": "newpass123"}),
            ("DELETE", "", None),
        ]:
            response = await client.request(method, f"{USERS}/{target['id']}{suffix}", json=body)
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "management_account_protected"
        quota = (await client.get(f"{USERS}/{target['id']}/quotas")).json()
        response = await client.patch(f"{USERS}/{target['id']}/quotas", json={
            "authorization_revision": quota["authorization_revision"], "revision": quota["revision"],
            "changes": {"project_count": {"mode": "limited", "value": 0}},
        })
        assert response.status_code == 403
    async with app.state.sessionmaker() as db:
        stored = await db.get(User, target["id"])
        assert stored is not None and not stored.is_active


async def test_role_and_quota_changes_have_bounded_safe_audits(app, system_admin, client):
    user = await register(client)
    role = await create_role(system_admin, "审计角色", ["projects.read"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "limited", "value": 2}})
    async with app.state.sessionmaker() as db:
        logs = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action.in_(
            ["role.create", "user.roles.update", "user.quotas.update"],
        ))))
    assert {r.action for r in logs} == {"role.create", "user.roles.update", "user.quotas.update"}
    assert all(r.result == "success" and r.actor_user_id is not None and r.summary["rbac_change"] for r in logs)
    assert "secret123" not in str([r.summary for r in logs])


async def test_self_super_identity_cannot_be_removed(system_admin):
    current = (await system_admin.get("/api/v1/auth/me")).json()
    response = await system_admin.put(f"{USERS}/{current['id']}/roles", json={
        "authorization_revision": current["authorization_revision"], "role_ids": [],
    })
    assert response.status_code == 400
    assert (await system_admin.get("/api/v1/auth/me")).json()["is_super_admin"]


@pytest.mark.parametrize("action", ["role.create", "user.roles.update", "user.quotas.update"])
async def test_new_security_writes_roll_back_with_audit_failure(
    app, client, system_admin, monkeypatch, action,
):
    from sqlalchemy import func, select

    from app.models import AdminAuditLog, AuthorizationState, Role, UserQuota, UserRole
    from app.services.audit_writer import AuditOperation
    from tests.rbac_helpers import authorization_revision

    user = await register(client, "rollback-target")
    role = await create_role(system_admin, "事务角色", ["users.read"])
    before_revision = await authorization_revision(system_admin)
    async with app.state.sessionmaker() as db:
        before_roles = await db.scalar(select(func.count(Role.id)))
    original = AuditOperation._event

    def event(audit, result, **kwargs):
        if audit.values["action"] == action and result == "success":
            raise RuntimeError("秘密审计错误不得泄露")
        return original(audit, result, **kwargs)

    monkeypatch.setattr(AuditOperation, "_event", event)
    if action == "role.create":
        response = await system_admin.post(ROLES, json={"name": "不能留下", "permissions": ["users.read"]})
    elif action == "user.roles.update":
        response = await system_admin.put(f"{USERS}/{user['id']}/roles", json={
            "authorization_revision": before_revision, "role_ids": [role["id"]],
        })
    else:
        response = await system_admin.patch(f"{USERS}/{user['id']}/quotas", json={
            "authorization_revision": before_revision, "revision": 0,
            "changes": {"project_count": {"mode": "limited", "value": 0}},
        })
    assert response.status_code == 500 and "秘密" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AuthorizationState.revision)) == before_revision
        assert await db.scalar(select(func.count(Role.id))) == before_roles
        assert await db.scalar(select(func.count(UserRole.user_id)).where(UserRole.user_id == user["id"])) == 0
        assert await db.get(UserQuota, user["id"]) is None
        logs = (await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == action))).all()
        assert logs[-1].result == "failure" and logs[-1].error_code == "system_error"
