"""RBAC集成测试复用真实HTTP授权/版本流，不绕过产品服务分配角色。"""


async def create_role(admin, name, permissions, **extra):
    response = await admin.post("/api/v1/admin/roles", json={"name": name, "permissions": permissions, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def authorization_revision(admin):
    response = await admin.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["authorization_revision"]


async def assign_roles(admin, user_id, role_ids):
    response = await admin.put(f"/api/v1/admin/users/{user_id}/roles", json={
        "authorization_revision": await authorization_revision(admin), "role_ids": role_ids,
    })
    assert response.status_code == 200, response.text
    return response.json()


async def set_quota(admin, user_id, changes):
    current = await admin.get(f"/api/v1/admin/users/{user_id}/quotas")
    assert current.status_code == 200, current.text
    current = current.json()
    response = await admin.patch(
        f"/api/v1/admin/users/{user_id}/quotas",
        json={
            "authorization_revision": current["authorization_revision"],
            "revision": current["revision"],
            "changes": changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()
