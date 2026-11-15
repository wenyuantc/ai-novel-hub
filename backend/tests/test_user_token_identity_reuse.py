"""删除账号后旧登录令牌不能借用重建账号的整数 ID。"""

import pytest

from tests.helpers import register


@pytest.mark.xfail(
    strict=True,
    reason=(
        "待实现：SQLite users 无 AUTOINCREMENT，JWT 仅 sub/ver；"
        "rbac-quotas.md 明确该实体 ID 复用风险不在当前迁移范围"
    ),
)
async def test_deleted_user_cookie_never_authenticates_new_user_after_id_reuse(make_client, system_admin):
    old, new = make_client(), make_client()
    previous = await register(old, "deleted-token-user")
    deleted = await system_admin.delete(f"/api/v1/admin/users/{previous['id']}")
    assert deleted.status_code == 204, deleted.text
    replacement = await register(new, "replacement-token-user")
    assert (await new.get("/api/v1/auth/me")).json()["id"] == replacement["id"]
    assert (await old.get("/api/v1/auth/me")).status_code == 401
