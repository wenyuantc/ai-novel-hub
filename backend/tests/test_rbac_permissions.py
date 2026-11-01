"""33项目录逐项通过真实HTTP验证：最小授权放行，只有依赖读权不放行写。"""

from app.core.permissions import PERMISSION_CODES, PERMISSIONS
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_skills import skill_body
from tests.test_workshop import style


def closure(code):
    result = set()
    by_code = {permission.code: permission for permission in PERMISSIONS}

    def visit(value):
        if value not in result:
            result.add(value)
            for dependency in by_code[value].requires:
                visit(dependency)

    visit(code)
    return [value for value in PERMISSION_CODES if value in result]


async def test_every_delegable_permission_direct_request(client, system_admin, make_client):
    actor = await register(client, "逐项操作者")
    owner = make_client()
    ordinary = await register(owner, "普通目标")
    path = f"/api/v1/admin/users/{ordinary['id']}"
    cases = {
        "dashboard.read": ("GET", "/api/v1/admin/dashboard/overview", None, 200),
        "users.read": ("GET", "/api/v1/admin/users", None, 200),
        "users.create": ("POST", "/api/v1/admin/users", {"username": "授权创建账号", "password": "secret123"}, 201),
        "users.status": ("PATCH", path, {"is_active": True}, 200),
        "users.reset_password": ("POST", f"{path}/reset-password", {"new_password": "secret456"}, 204),
        "users.delete": ("DELETE", "/api/v1/admin/users/999999", None, 404),
        "quotas.read": ("GET", f"{path}/quotas", None, 200),
        "quotas.update": None,
        "projects.read": ("GET", "/api/v1/admin/projects", None, 200),
        "projects.edit": None,
        "projects.recycle": None,
        "recycle_bin.read": ("GET", "/api/v1/admin/recycle-bin/projects", None, 200),
        "recycle_bin.restore": None,
        "recycle_bin.purge": None,
        "tasks.read": ("GET", "/api/v1/admin/tasks", None, 200),
        "tasks.cancel": ("POST", "/api/v1/admin/tasks/00000000-0000-0000-0000-000000000001/cancel", {
            "confirm": True, "reason": "逐项授权验证",
        }, 404),
        "audit.read": ("GET", "/api/v1/admin/audit-logs", None, 200),
        "audit.cleanup": ("POST", "/api/v1/admin/audit-logs/cleanup-preview", {
            "from_at": "2020-01-01T00:00:00Z", "to_at": "2021-01-01T00:00:00Z",
        }, 200),
        "settings.read": ("GET", "/api/v1/admin/system-settings", None, 200),
        "settings.update": None,
        "workshop.read": ("GET", "/api/v1/workshop/admin/submissions", None, 200),
        "workshop.approve": ("POST", "/api/v1/workshop/admin/submissions/999999/approve", {}, 404),
        "workshop.reject": ("POST", "/api/v1/workshop/admin/submissions/999999/reject", {"note": "未通过"}, 404),
        "workshop.publish": ("POST", "/api/v1/workshop/admin/items", style(), 201),
        "workshop.update": ("PATCH", "/api/v1/workshop/admin/items/999999", {"tags": []}, 404),
        "workshop.hide": None,
        "workshop.unhide": None,
        "workshop.delete": ("DELETE", "/api/v1/workshop/admin/items/999999", None, 404),
        "global_skills.create": ("POST", "/api/v1/skills", skill_body(scope="global", slug="delegated"), 201),
        "global_skills.update": None,
        "global_skills.references": None,
        "global_skills.delete": None,
        "global_skills.restore_builtin": None,
    }
    assert set(cases) == set(PERMISSION_CODES) and len(cases) == 33
    serial = 0

    async def prepare(code):
        nonlocal serial
        serial += 1
        if cases[code] is not None:
            return cases[code]
        if code == "quotas.update":
            current = (await system_admin.get(f"{path}/quotas")).json()
            return "PATCH", f"{path}/quotas", {
                "authorization_revision": current["authorization_revision"], "revision": current["revision"],
                "changes": {"project_count": {"mode": "inherit"}},
            }, 200
        if code == "settings.update":
            current = (await system_admin.get("/api/v1/admin/system-settings")).json()
            return "PATCH", "/api/v1/admin/system-settings", {
                "revision": current["revision"], "changes": {"auto_analyze_enabled": False},
            }, 200
        if code.startswith("projects.") or code.startswith("recycle_bin."):
            # 重置密码测试会撤销owner会话，先用新密码重新登录。
            await owner.post("/api/v1/auth/login", json={"username": "普通目标", "password": "secret456"})
            project = await create_project(owner)
            url = f"/api/v1/projects/{project['id']}"
            if code == "projects.edit":
                return "PATCH", url, {"title": "最小写权限"}, 200
            if code == "projects.recycle":
                return "DELETE", url, None, 204
            assert (await owner.delete(url)).status_code == 204
            recycled = (await system_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"]
            row = next(item for item in recycled if item["id"] == project["id"])
            action = code.split(".")[1]
            body = {"deleted_at": row["deleted_at"], "confirm": True}
            if action == "purge":
                body.update(title=row["title"], confirm=True)
            return "POST", f"/api/v1/admin/recycle-bin/projects/{project['id']}/{action}", body, 204
        if code in ("workshop.hide", "workshop.unhide"):
            item = (await system_admin.post("/api/v1/workshop/admin/items", json=style())).json()
            if code == "workshop.unhide":
                assert (await system_admin.post(f"/api/v1/workshop/items/{item['id']}/hide")).status_code == 200
            return "POST", f"/api/v1/workshop/items/{item['id']}/{code.split('.')[1]}", None, 200
        skills = (await system_admin.get("/api/v1/skills", params={"scope": "global"})).json()
        if code == "global_skills.delete":
            created = await system_admin.post(
                "/api/v1/skills", json=skill_body(scope="global", slug=f"delete-{serial}")
            )
            assert created.status_code == 201, created.text
            return "DELETE", f"/api/v1/skills/{created.json()['id']}", None, 204
        builtin = next(item for item in skills if item["builtin_key"])
        url = f"/api/v1/skills/{builtin['id']}"
        if code == "global_skills.update":
            return "PATCH", url, {"name": builtin["name"]}, 200
        if code == "global_skills.references":
            return "PUT", f"{url}/references", {"references": []}, 200
        assert code == "global_skills.restore_builtin"
        return "POST", f"{url}/restore-builtin", None, 200

    for code in PERMISSION_CODES:
        allowed = closure(code)
        role = await create_role(system_admin, f"最小授权-{code}", allowed)
        await assign_roles(system_admin, actor["id"], [role["id"]])
        method, url, body, status = await prepare(code)
        response = await client.request(method, url, json=body)
        assert response.status_code == status, (code, response.text)
        # 只保留依赖项：读权不可代替本动作的写权。
        dependencies = [value for value in allowed if value != code]
        role = await create_role(system_admin, f"仅依赖-{code}", dependencies)
        await assign_roles(system_admin, actor["id"], [role["id"]])
        method, url, body, _ = await prepare(code)
        response = await client.request(method, url, json=body)
        # 工坊非作者没有该管理动作时保留原404隐私边界，不暴露可操作条目。
        denied = 404 if code in ("workshop.hide", "workshop.unhide") else 403
        assert response.status_code == denied, (code, response.status_code, response.text)
