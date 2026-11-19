"""协作审计查询：作者侧防越权/防枚举隔离、管理员全站视图、清理隔离与时间边界。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.models import CollaborationAuditEvent, Project, Series, User
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def test_owner_project_audit_filters_pagination_and_summary(client, make_client):
    owner = await register(client, "audit-q-owner")
    guest_client = make_client()
    guest = await register(guest_client, "audit-q-guest")
    other_client = make_client()
    await register(other_client, "audit-q-other")
    project = await create_project(client, title="星海纪元")
    series = (await client.post("/api/v1/series", json={"name": "独立系列"})).json()
    base = f"/api/v1/projects/{project['id']}/collaborators"
    audit_url = f"{base}/audit-logs"

    # 系列事件不应混入作品审计。
    series_invite = await client.post(
        f"/api/v1/series/{series['id']}/collaborators", json={"username": "audit-q-guest"},
    )
    assert series_invite.status_code == 201

    invited = (await client.post(base, json={"username": "audit-q-guest", "role": "viewer"})).json()
    accept_url = f"/api/v1/collaboration/invitations/project/{invited['invitation_id']}/accept"
    assert (await guest_client.post(accept_url, json={"expected_revision": 1})).status_code == 200
    stale_mod = await client.patch(f"{base}/{guest['id']}", json={"expected_revision": 1, "role": "editor"})
    assert stale_mod.status_code == 409
    assert (await client.patch(
        f"{base}/{guest['id']}", json={"expected_revision": 2, "role": "editor", "allow_owner_ai": True},
    )).status_code == 200
    assert (await client.delete(f"{base}/{guest['id']}", params={"expected_revision": 3})).status_code == 204
    invited_other = (await client.post(base, json={"username": "audit-q-other", "role": "viewer"})).json()
    decline_url = f"/api/v1/collaboration/invitations/project/{invited_other['invitation_id']}/decline"
    assert (await other_client.post(decline_url, json={"expected_revision": 1})).status_code == 200

    res = await client.get(audit_url)
    assert res.status_code == 200, res.text
    assert res.headers.get("cache-control") == "no-store"
    body = res.json()
    assert body["total"] == 7
    assert [item["id"] for item in body["items"]] == sorted((item["id"] for item in body["items"]), reverse=True)
    assert all(item["scope"] == "project" and item["resource_id"] == project["id"] for item in body["items"])

    modify_ok = next(i for i in body["items"] if i["action"] == "modify" and i["result"] == "success")
    assert modify_ok["summary"] == {
        "before_role": "viewer",
        "after_role": "editor",
        "before_status": "accepted",
        "after_status": "accepted",
        "before_allow_owner_ai": False,
        "after_allow_owner_ai": True,
    }
    modify_fail = next(i for i in body["items"] if i["action"] == "modify" and i["result"] == "failure")
    assert modify_fail["error_code"] == "stale_invitation"
    assert modify_fail["summary"]["after_role"] is None

    # 空白 member_q 等价于未筛选。
    assert (await client.get(audit_url, params={"member_q": "   "})).json()["total"] == 7

    # 组合筛选：按成员关键字、成员 ID、动作、结果与分页。
    by_member_q = (await client.get(audit_url, params={"member_q": "Q-OTHER"})).json()
    assert by_member_q["total"] == 2
    assert {i["action"] for i in by_member_q["items"]} == {"invite", "decline"}

    by_member_id = (await client.get(audit_url, params={"member_user_id": guest["id"], "result": "success"})).json()
    assert by_member_id["total"] == 4
    assert all(i["target_user_id"] == guest["id"] for i in by_member_id["items"])

    by_owner = (await client.get(audit_url, params={"member_user_id": owner["id"], "action": "invite"})).json()
    assert by_owner["total"] == 2

    page1 = (await client.get(audit_url, params={"page": 1, "page_size": 3})).json()
    page2 = (await client.get(audit_url, params={"page": 2, "page_size": 3})).json()
    page3 = (await client.get(audit_url, params={"page": 3, "page_size": 3})).json()
    assert page1["total"] == page2["total"] == page3["total"] == 7
    all_ids = [i["id"] for i in page1["items"] + page2["items"] + page3["items"]]
    assert len(all_ids) == 7 and len(set(all_ids)) == 7


async def test_owner_series_audit_query_and_isolation(client, make_client, system_admin):
    await register(client, "series-audit-owner")
    guest_client = make_client()
    guest = await register(guest_client, "series-audit-guest")
    stranger_client = make_client()
    await register(stranger_client, "series-audit-stranger")

    series = (await client.post("/api/v1/series", json={"name": "九州"})).json()
    base = f"/api/v1/series/{series['id']}/collaborators"
    audit_url = f"{base}/audit-logs"

    invited = (await client.post(base, json={"username": "series-audit-guest"})).json()
    accept_url = f"/api/v1/collaboration/invitations/series/{invited['invitation_id']}/accept"
    assert (await guest_client.post(accept_url, json={"expected_revision": 1})).status_code == 200
    assert (await client.delete(f"{base}/{guest['id']}", params={"expected_revision": 2})).status_code == 204

    res = await client.get(audit_url, params={"member_q": "audit-guest"})
    assert res.status_code == 200, res.text
    assert res.headers.get("cache-control") == "no-store"
    data = res.json()
    assert data["total"] == 3
    assert [i["action"] for i in data["items"]] == ["revoke", "accept", "invite"]
    assert all(i["scope"] == "series" and i["summary"]["after_allow_owner_ai"] is None for i in data["items"])

    # 协作者、陌生人及非作者超管均返回 404。
    for other in (guest_client, stranger_client, system_admin):
        assert (await other.get(audit_url)).status_code == 404


async def test_owner_audit_access_and_project_lifecycle_matrix(app, client, make_client, system_admin):
    owner = await register(client, "life-audit-owner")
    viewer_client = make_client()
    await register(viewer_client, "life-audit-viewer")
    editor_client = make_client()
    await register(editor_client, "life-audit-editor")
    stranger_client = make_client()
    await register(stranger_client, "life-audit-stranger")
    audit_admin_client = make_client()
    audit_admin = await register(audit_admin_client, "life-audit-reader")
    content_admin_client = make_client()
    content_admin = await register(content_admin_client, "life-content-admin")

    audit_role = await create_role(system_admin, "仅查审计", ["audit.read"])
    content_role = await create_role(system_admin, "内容维护", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, audit_admin["id"], [audit_role["id"]])
    await assign_roles(system_admin, content_admin["id"], [content_role["id"]])

    project = await create_project(client, title="生命周期作品")
    base = f"/api/v1/projects/{project['id']}/collaborators"
    audit_url = f"{base}/audit-logs"

    members = (
        ("viewer", viewer_client, "life-audit-viewer"),
        ("editor", editor_client, "life-audit-editor"),
    )
    for role, c, uname in members:
        inv = (await client.post(base, json={"username": uname, "role": role})).json()
        assert (await c.post(
            f"/api/v1/collaboration/invitations/project/{inv['invitation_id']}/accept", json={"expected_revision": 1},
        )).status_code == 200

    # 未登录 401。
    assert (await make_client().get(audit_url)).status_code == 401
    # 协作者、陌生人、拥有 audit.read/projects.edit 的非作者管理员、超管均返回 404。
    blocked = (viewer_client, editor_client, stranger_client, audit_admin_client, content_admin_client, system_admin)
    for other in blocked:
        assert (await other.get(audit_url)).status_code == 404

    # 软删进入回收站 -> 404；恢复 -> 200。
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    assert (await client.get(audit_url)).status_code == 404
    trash = (await system_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"]
    recycled = next(item for item in trash if item["id"] == project["id"])
    assert (await system_admin.post(
        f"/api/v1/admin/recycle-bin/projects/{project['id']}/restore",
        json={"deleted_at": recycled["deleted_at"], "confirm": True},
    )).status_code == 204
    assert (await client.get(audit_url)).json()["total"] == 4

    # 删除中 -> 409 project_deleting。
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        row.deletion_started_at = datetime.now(UTC)
        row.deletion_token = str(uuid4())
        await db.commit()
    deleting = await client.get(audit_url)
    assert deleting.status_code == 409 and deleting.json()["code"] == "project_deleting"

    # 停用作者账号 -> 401。
    async with app.state.sessionmaker() as db:
        (await db.get(User, owner["id"])).is_active = False
        await db.commit()
    assert (await client.get(audit_url)).status_code == 401


async def test_owner_audit_excludes_stranger_probes_and_redacts_unconfirmed_invite_targets(
    app, client, make_client, system_admin,
):
    owner = await register(client, "sec-owner")
    guest_client = make_client()
    guest = await register(guest_client, "sec-guest")
    disabled_client = make_client()
    disabled = await register(disabled_client, "sec-disabled-user")
    stranger_client = make_client()
    stranger = await register(stranger_client, "sec-stranger")
    bystander_client = make_client()
    bystander = await register(bystander_client, "sec-bystander")

    async with app.state.sessionmaker() as db:
        (await db.get(User, disabled["id"])).is_active = False
        await db.commit()

    project = await create_project(client, title="安全边界作品")
    base = f"/api/v1/projects/{project['id']}/collaborators"
    audit_url = f"{base}/audit-logs"

    # 1. 作者正常邀请 + 重复邀请失败 + 自邀失败。
    assert (await client.post(base, json={"username": "sec-guest", "role": "viewer"})).status_code == 201
    assert (await client.post(base, json={"username": "sec-guest", "role": "editor"})).status_code == 409
    assert (await client.post(base, json={"username": "sec-owner", "role": "viewer"})).status_code == 400

    # 2. 作者分别邀请已禁用账号与不存在账号，均返回 invitee_unavailable。
    assert (await client.post(base, json={"username": "sec-disabled-user", "role": "viewer"})).status_code == 400
    assert (await client.post(base, json={"username": "sec-missing-user", "role": "viewer"})).status_code == 400

    # 3. 陌生人与已受邀协作者越权调用邀请/修改/撤销接口，产生挂载在该 resource_id 下的失败记录。
    assert (await stranger_client.post(base, json={"username": "sec-bystander", "role": "editor"})).status_code == 404
    stranger_patch = await stranger_client.patch(
        f"{base}/{guest['id']}", json={"expected_revision": 1, "role": "editor"},
    )
    assert stranger_patch.status_code == 404
    assert (await stranger_client.delete(f"{base}/{guest['id']}", params={"expected_revision": 1})).status_code == 404
    assert (await guest_client.post(base, json={"username": "sec-bystander", "role": "viewer"})).status_code == 404

    # 4. 作品进回收站期间作者邀请 bystander 失败（not_found），再恢复作品：历史行也不得暴露 bystander。
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    assert (await client.post(base, json={"username": "sec-bystander", "role": "viewer"})).status_code == 404
    trash = (await system_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"]
    recycled = next(item for item in trash if item["id"] == project["id"])
    assert (await system_admin.post(
        f"/api/v1/admin/recycle-bin/projects/{project['id']}/restore",
        json={"deleted_at": recycled["deleted_at"], "confirm": True},
    )).status_code == 204

    # 核实数据库确实记录了 disabled 的 target 与陌生人的越权失败。
    async with app.state.sessionmaker() as db:
        db_rows = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.scope == "project", CollaborationAuditEvent.resource_id == project["id"],
        )))
        assert len(db_rows) == 10
        assert any(r.error_code == "invitee_unavailable" and r.target_user_id == disabled["id"] for r in db_rows)
        assert any(r.actor_user_id == stranger["id"] and r.target_user_id == bystander["id"] for r in db_rows)

    # 作者侧：仅见 6 条（排除 4 条非作者越权探测）。
    owner_list = (await client.get(audit_url)).json()
    assert owner_list["total"] == 6
    assert all(item["actor_user_id"] == owner["id"] for item in owner_list["items"])

    # self_invite 和 already_collaborator 保留已知目标。
    self_inv = next(i for i in owner_list["items"] if i["error_code"] == "self_invite")
    dup_inv = next(i for i in owner_list["items"] if i["error_code"] == "already_collaborator")
    assert self_inv["target_user_id"] == owner["id"] and self_inv["target_username"] == "sec-owner"
    assert dup_inv["target_user_id"] == guest["id"] and dup_inv["target_username"] == "sec-guest"

    # invitee_unavailable（已禁用 vs 不存在）与回收期 not_found 一律抹平目标。
    unavail = [i for i in owner_list["items"] if i["error_code"] == "invitee_unavailable"]
    assert len(unavail) == 2
    assert all(i["target_user_id"] is None and i["target_username"] is None for i in unavail)
    recycled_fail = next(i for i in owner_list["items"] if i["error_code"] == "not_found")
    assert recycled_fail["target_user_id"] is None and recycled_fail["target_username"] is None

    # 成员筛选也不得借此探测已禁用用户、陌生人或旁观者。
    for q in ("sec-disabled-user", "sec-missing-user", "sec-stranger", "sec-bystander"):
        assert (await client.get(audit_url, params={"member_q": q})).json()["total"] == 0
    for uid in (disabled["id"], stranger["id"], bystander["id"]):
        assert (await client.get(audit_url, params={"member_user_id": uid})).json()["total"] == 0


async def test_admin_collaboration_audit_full_visibility_permissions_and_cleanup_isolation(
    app, client, make_client, system_admin,
):
    await register(client, "adm-collab-owner")
    guest_client = make_client()
    guest = await register(guest_client, "adm-collab-guest")
    stranger_client = make_client()
    stranger = await register(stranger_client, "adm-collab-stranger")
    audit_only_client = make_client()
    audit_only = await register(audit_only_client, "adm-audit-only")
    no_audit_client = make_client()
    no_audit = await register(no_audit_client, "adm-no-audit")

    role_audit = await create_role(system_admin, "日志只读", ["audit.read"])
    role_projects = await create_role(system_admin, "作品只读", ["projects.read"])
    await assign_roles(system_admin, audit_only["id"], [role_audit["id"]])
    await assign_roles(system_admin, no_audit["id"], [role_projects["id"]])

    # 权限边界：未登录 401，普通用户 / 无 audit.read 管理员 403。
    admin_url = "/api/v1/admin/audit-logs/collaboration"
    assert (await make_client().get(admin_url)).status_code == 401
    assert (await client.get(admin_url)).status_code == 403
    denied = await no_audit_client.get(admin_url)
    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"
    assert denied.json()["required_permission"] == "audit.read"

    # 构造覆盖作品、系列、越权探测、resource_id=0、彻底删除作品与删除系列的记录。
    project = await create_project(client, title="将被彻删的作品")
    series = (await client.post("/api/v1/series", json={"name": "将被删除的系列"})).json()
    p_inv = (await client.post(
        f"/api/v1/projects/{project['id']}/collaborators", json={"username": "adm-collab-guest", "role": "editor"},
    )).json()
    assert (await guest_client.post(
        f"/api/v1/collaboration/invitations/project/{p_inv['invitation_id']}/accept", json={"expected_revision": 1},
    )).status_code == 200
    assert (await stranger_client.post(
        f"/api/v1/projects/{project['id']}/collaborators", json={"username": "adm-collab-guest", "role": "viewer"},
    )).status_code == 404
    assert (await stranger_client.post(
        f"/api/v1/collaboration/invitations/project/{uuid4()}/accept", json={"expected_revision": 1},
    )).status_code == 404
    assert (await client.post(
        f"/api/v1/series/{series['id']}/collaborators", json={"username": "adm-collab-guest"},
    )).status_code == 201

    # 彻底删除作品并删除系列与被邀用户。
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    trash = (await system_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"]
    recycled = next(item for item in trash if item["id"] == project["id"])
    assert (await system_admin.post(
        f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge",
        json={"deleted_at": recycled["deleted_at"], "confirm": True, "title": project["title"]},
    )).status_code == 204
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(Series, series["id"]))
        await db.delete(await db.get(User, guest["id"]))
        await db.commit()

    # 仅具 audit.read 的管理员可查询全站快照。
    res = await audit_only_client.get(admin_url)
    assert res.status_code == 200, res.text
    assert res.headers.get("cache-control") == "no-store"
    all_events = res.json()
    assert all_events["total"] == 5

    # 筛选已彻底删除的作品快照。
    by_project = (
        await audit_only_client.get(admin_url, params={"scope": "project", "resource_id": project["id"]})
    ).json()
    assert by_project["total"] == 3
    assert any(i["actor_user_id"] == stranger["id"] and i["result"] == "failure" for i in by_project["items"])
    assert any(
        i["resource_label"] == "将被彻删的作品" and i["target_username"] == "adm-collab-guest"
        for i in by_project["items"]
    )

    # 筛选已删除的系列与 resource_q。
    by_series = (await audit_only_client.get(admin_url, params={"scope": "series", "resource_q": "将被删除"})).json()
    assert by_series["total"] == 1 and by_series["items"][0]["resource_id"] == series["id"]

    # 筛选 resource_id = 0 的无效邀请探测。
    by_zero = (await audit_only_client.get(admin_url, params={"resource_id": 0})).json()
    assert by_zero["total"] == 1
    assert by_zero["items"][0]["actor_user_id"] == stranger["id"]
    assert by_zero["items"][0]["error_code"] == "not_found"

    # 筛选 target_user_id（即使用户已删）与 actor_q。
    by_target = (await audit_only_client.get(admin_url, params={"target_user_id": guest["id"]})).json()
    assert by_target["total"] == 4
    by_actor = (await audit_only_client.get(admin_url, params={"actor_q": "stranger"})).json()
    assert by_actor["total"] == 2

    # 确认现有 /admin/audit-logs/{log_id} 路由不受影响。
    admin_logs = (await system_admin.get("/api/v1/admin/audit-logs")).json()
    assert admin_logs["total"] > 0
    assert (await system_admin.get(f"/api/v1/admin/audit-logs/{admin_logs['items'][0]['id']}")).status_code == 200

    # 清理隔离：执行管理操作日志按日期清理，协作审计零损失。
    preview = (await system_admin.post(
        "/api/v1/admin/audit-logs/cleanup-preview",
        json={"from_at": "2020-01-01T00:00:00Z", "to_at": "2099-01-01T00:00:00Z"},
    )).json()
    assert preview["count"] > 0
    cleaned = await system_admin.post(
        "/api/v1/admin/audit-logs/cleanup",
        json={"preview_token": preview["preview_token"], "confirm": True},
    )
    assert cleaned.status_code == 200 and cleaned.json()["deleted_count"] > 0
    after_cleanup = (await audit_only_client.get(admin_url)).json()
    assert after_cleanup["total"] == 5
    assert [i["id"] for i in after_cleanup["items"]] == [i["id"] for i in all_events["items"]]


async def test_literal_like_escaping_and_time_range_half_open_boundaries(app, client, make_client, system_admin):
    owner = await register(client, "like_100%\\owner")
    guest = await register(make_client(), "like_100%\\guest")
    await register(make_client(), "likeX100YYguest")
    project = await create_project(client, title="进度_50%\\卷")
    await create_project(client, title="进度X50YY卷")
    base = f"/api/v1/projects/{project['id']}/collaborators"

    assert (await client.post(base, json={"username": "like_100%\\guest", "role": "viewer"})).status_code == 201
    assert (await client.post(base, json={"username": "likeX100YYguest", "role": "viewer"})).status_code == 201

    # 字面转义 %、_、\，不能当作通配符命中 likeX100YYguest 或 进度X50YY卷。
    owner_match = (await client.get(f"{base}/audit-logs", params={"member_q": "_100%\\g"})).json()
    assert owner_match["total"] == 1 and owner_match["items"][0]["target_user_id"] == guest["id"]

    admin_match = (await system_admin.get(
        "/api/v1/admin/audit-logs/collaboration", params={"resource_q": "_50%\\", "target_q": "_100%\\"},
    )).json()
    assert admin_match["total"] == 1 and admin_match["items"][0]["target_user_id"] == guest["id"]

    # 精确测试半开 UTC 区间 [from_at, to_at) 与 SQLite server_default 19 字符整秒格式。
    t_before = datetime(2026, 6, 1, 0, 59, 59, tzinfo=UTC)
    t_start = datetime(2026, 6, 1, 1, 0, 0, tzinfo=UTC)  # 等价 2026-06-01T09:00:00+08:00
    t_mid = datetime(2026, 6, 1, 1, 0, 0, 500000, tzinfo=UTC)
    t_end = datetime(2026, 6, 1, 2, 0, 0, tzinfo=UTC)  # 等价 2026-06-01T10:00:00+08:00
    async with app.state.sessionmaker() as db:
        e_before = CollaborationAuditEvent(
            operation_id=str(uuid4()), occurred_at=t_before, scope="project", resource_id=project["id"],
            resource_label="边界", actor_user_id=owner["id"], actor_username=owner["username"],
            action="invite", result="success", summary={},
        )
        e_start = CollaborationAuditEvent(
            operation_id=str(uuid4()), occurred_at=t_start, scope="project", resource_id=project["id"],
            resource_label="边界", actor_user_id=owner["id"], actor_username=owner["username"],
            action="modify", result="success", summary={},
        )
        e_mid = CollaborationAuditEvent(
            operation_id=str(uuid4()), occurred_at=t_mid, scope="project", resource_id=project["id"],
            resource_label="边界", actor_user_id=owner["id"], actor_username=owner["username"],
            action="revoke", result="success", summary={},
        )
        e_end = CollaborationAuditEvent(
            operation_id=str(uuid4()), occurred_at=t_end, scope="project", resource_id=project["id"],
            resource_label="边界", actor_user_id=owner["id"], actor_username=owner["username"],
            action="accept", result="success", summary={},
        )
        db.add_all([e_before, e_start, e_mid, e_end])
        await db.commit()
        ids = {"before": e_before.id, "start": e_start.id, "mid": e_mid.id, "end": e_end.id}
        if db.get_bind().dialect.name == "sqlite":
            # 模拟 SQLite server_default CURRENT_TIMESTAMP 写入的无微秒 19 字符格式。
            await db.execute(
                text("UPDATE collaboration_audit_events SET occurred_at = '2026-06-01 01:00:00' WHERE id = :id"),
                {"id": ids["start"]},
            )
            await db.execute(
                text("UPDATE collaboration_audit_events SET occurred_at = '2026-06-01 02:00:00' WHERE id = :id"),
                {"id": ids["end"]},
            )
            await db.commit()

    # 带 +08:00 偏移查询：应包含 start（整秒 19 字符）和 mid，严格排除 before 与 end（整秒 19 字符）。
    params = {"from_at": "2026-06-01T09:00:00+08:00", "to_at": "2026-06-01T10:00:00+08:00"}
    owner_range = (await client.get(f"{base}/audit-logs", params=params)).json()
    assert owner_range["total"] == 2
    assert [i["id"] for i in owner_range["items"]] == [ids["mid"], ids["start"]]

    admin_range = (await system_admin.get("/api/v1/admin/audit-logs/collaboration", params=params)).json()
    assert admin_range["total"] == 2
    assert [i["id"] for i in admin_range["items"]] == [ids["mid"], ids["start"]]


@pytest.mark.parametrize(
    "params",
    [
        {"from_at": "2026-06-01T00:00:00Z"},
        {"to_at": "2026-06-02T00:00:00Z"},
        {"from_at": "2026-06-01T00:00:00", "to_at": "2026-06-02T00:00:00Z"},
        {"from_at": "2026-06-02T00:00:00Z", "to_at": "2026-06-01T00:00:00Z"},
        {"from_at": "2026-06-01T00:00:00Z", "to_at": "2026-06-01T00:00:00Z"},
        {"action": "delete"},
        {"result": "partial"},
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"unknown_param": "x"},
    ],
    ids=[
        "only-from", "only-to", "naive-from", "reversed-range", "empty-range",
        "bad-action", "bad-result", "page-zero", "page-size-zero", "page-size-too-large", "extra-param",
    ],
)
async def test_invalid_common_filter_parameters_return_422(client, system_admin, params):
    await register(client, "val-owner")
    project = await create_project(client)
    owner_res = await client.get(f"/api/v1/projects/{project['id']}/collaborators/audit-logs", params=params)
    assert owner_res.status_code == 422
    assert (await system_admin.get("/api/v1/admin/audit-logs/collaboration", params=params)).status_code == 422


async def test_invalid_id_bounds_return_422(client, system_admin):
    await register(client, "bounds-audit-owner")
    project = await create_project(client)
    oversized = 2_147_483_648
    p_url = f"/api/v1/projects/{project['id']}/collaborators/audit-logs"
    a_url = "/api/v1/admin/audit-logs/collaboration"

    assert (await client.get(f"/api/v1/projects/{oversized}/collaborators/audit-logs")).status_code == 422
    assert (await client.get(f"/api/v1/series/{oversized}/collaborators/audit-logs")).status_code == 422
    assert (await client.get(p_url, params={"member_user_id": 0})).status_code == 422
    assert (await client.get(p_url, params={"member_user_id": oversized})).status_code == 422
    assert (await client.get(p_url, params={"member_q": "a" * 65})).status_code == 422

    assert (await system_admin.get(a_url, params={"scope": "chapter"})).status_code == 422
    assert (await system_admin.get(a_url, params={"resource_id": -1})).status_code == 422
    assert (await system_admin.get(a_url, params={"resource_id": oversized})).status_code == 422
    assert (await system_admin.get(a_url, params={"actor_user_id": 0})).status_code == 422
    assert (await system_admin.get(a_url, params={"target_user_id": oversized})).status_code == 422
    assert (await system_admin.get(a_url, params={"resource_q": "x" * 201})).status_code == 422
