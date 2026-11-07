"""实例审计的权限、范围、事务、脱敏、版本与受保护清理回归。"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAuditLog, Chapter, ChapterVersion, Project, User
from app.services import admin_service, audit_service
from app.services.audit_writer import AuditOperation, AuditOperationError
from tests.helpers import create_project, register

URL = "/api/v1/admin/audit-logs"
USERS = "/api/v1/admin/users"
RANGE = {"from_at": "2020-01-01T00:00:00Z", "to_at": "2099-01-01T00:00:00Z"}


@pytest.fixture
async def admin(app, make_client):
    c = make_client()
    data = await register(c, "manager")
    async with app.state.sessionmaker() as db:
        user = await db.get(User, data["id"])
        user.is_admin = True
        await db.commit()
    return c


async def rows(app):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))


async def detail(admin, event):
    r = await admin.get(f"{URL}/{event.id}")
    assert r.status_code == 200, r.text
    return r.json()


async def test_all_audit_routes_require_current_admin(admin, client, app):
    for c, status in ((client, 401),):
        for method, path, body in (
            ("GET", URL, None),
            ("GET", f"{URL}/1", None),
            ("POST", f"{URL}/cleanup-preview", RANGE),
            ("POST", f"{URL}/cleanup", {"preview_token": "x", "confirm": True}),
        ):
            assert (await c.request(method, path, json=body)).status_code == status
    await register(client, "alice")
    for method, path, body in (
        ("GET", URL, None),
        ("GET", f"{URL}/1", None),
        ("POST", f"{URL}/cleanup-preview", RANGE),
        ("POST", f"{URL}/cleanup", {"preview_token": "x", "confirm": True}),
    ):
        assert (await client.request(method, path, json=body)).status_code == 403
    assert await rows(app) == []
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User).where(User.username == "manager"))
        user.is_active = False
        await db.commit()
    assert (await admin.get(URL)).status_code == 401


async def test_user_success_failure_credentials_and_noop(admin, client, app):
    r = await admin.post(USERS, json={"username": "new-user", "password": "audit-secret123"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert (await admin.patch(f"{USERS}/{uid}", json={"is_active": False})).status_code == 200
    assert (
        await admin.post(f"{USERS}/{uid}/reset-password", json={"new_password": "new-audit-secret"})
    ).status_code == 204
    assert (await admin.post(USERS, json={"username": "new-user", "password": "duplicate-secret"})).status_code == 409
    me = (await admin.get("/api/v1/auth/me")).json()
    assert (await admin.patch(f"{USERS}/{me['id']}", json={"is_active": False})).status_code == 400
    assert (await admin.patch(f"{USERS}/{me['id']}", json={"is_active": True})).status_code == 200
    # schema 失败及普通注册不采集
    assert (await admin.post(USERS, json={"username": "x", "password": "123"})).status_code == 422
    await register(client, "ordinary")
    assert (await admin.delete(f"{USERS}/{uid}")).status_code == 204
    events = await rows(app)
    assert [e.action for e in events] == [
        "user.create",
        "user.update",
        "user.reset_password",
        "user.create",
        "user.update",
        "user.update",
        "user.delete",
    ]
    assert [e.result for e in events] == ["success", "success", "success", "failure", "failure", "success", "success"]
    assert events[0].actor_user_id == me["id"] and events[0].target_id == uid
    assert (await detail(admin, events[5]))["summary"]["changed_fields"] == []
    blob = json.dumps([e.__dict__ for e in events], default=lambda value: None)
    blob += (await admin.get(URL)).text
    for e in events:
        blob += json.dumps(await detail(admin, e))
    for secret in ("audit-secret123", "new-audit-secret", "duplicate-secret", "password_hash", "token_version", "$2b$"):
        assert secret not in blob
    assert events[3].error_code == "conflict"
    assert events[4].error_message == "业务条件不满足"


async def test_project_chapter_two_actors_versions_and_deleted_snapshots(admin, client, make_client, app):
    owner = await register(client, "owner")
    p = await create_project(client, title="历史书名")
    base = f"/api/v1/projects/{p['id']}"
    second = make_client()
    second_data = await register(second, "second-admin")
    async with app.state.sessionmaker() as db:
        (await db.get(User, second_data["id"])).is_admin = True
        await db.commit()
    c = (await admin.post(f"{base}/chapters", json={"title": "章名", "content": "初稿正文"})).json()
    chapter_url = f"{base}/chapters/{c['id']}"
    first_event = (await rows(app))[0]
    assert (await second.patch(chapter_url, json={"content": "修改后的正文"})).status_code == 200
    events = await rows(app)
    assert events[0].actor_user_id != events[1].actor_user_id
    assert events[0].owner_user_id == events[1].owner_user_id == owner["id"]
    assert events[0].version_ref["version_id"] == events[1].version_ref["version_id"]
    d = await detail(admin, first_event)
    assert d["version_status"] == "changed" and "初稿正文" not in json.dumps(d)
    assert events[1].summary["word_count_before"] == 4 and events[1].summary["word_count_after"] == 6
    # 清空强制保留新的空正文结构版本，审计只关联版本元数据。
    assert (await admin.patch(chapter_url, json={"content": ""})).status_code == 200
    clear_event = (await rows(app))[-1]
    versions = (await admin.get(f"{chapter_url}/versions")).json()
    assert len(versions) == 2
    empty_version = (await admin.get(f"{chapter_url}/versions/{versions[0]['id']}")).json()
    assert empty_version["id"] != events[1].version_ref["version_id"]
    assert empty_version["seq"] == events[1].version_ref["seq"] + 1
    assert empty_version["content"] == "" and empty_version["word_count"] == 0
    assert empty_version["has_scene_snapshot"] is True
    assert len(empty_version["scene_snapshot"]["scenes"]) == 1
    assert empty_version["scene_snapshot"]["scenes"][0]["content"] == ""
    assert clear_event.action == "chapter.update" and clear_event.result == "success"
    assert clear_event.actor_user_id == first_event.actor_user_id
    assert clear_event.owner_user_id == owner["id"]
    assert clear_event.summary["word_count_before"] == 6 and clear_event.summary["word_count_after"] == 0
    assert clear_event.version_ref == {
        "project_id": p["id"],
        "chapter_id": c["id"],
        "version_id": empty_version["id"],
        "seq": empty_version["seq"],
        "content_sha256": sha256(b"").hexdigest(),
    }
    clear_detail = await detail(admin, clear_event)
    assert clear_detail["version_status"] == "available"
    assert "初稿正文" not in json.dumps(clear_detail) and "修改后的正文" not in json.dumps(clear_detail)
    assert (await admin.patch(base, json={"status": "writing", "title": "改名后"})).status_code == 200
    assert (await rows(app))[-1].summary["changes"] == [{"field": "status", "before": "planning", "after": "writing"}]
    # 本人创作与普通用户被拒绝不采集
    count = len(await rows(app))
    own = await create_project(admin)
    assert (await admin.patch(f"/api/v1/projects/{own['id']}", json={"title": "本人"})).status_code == 200
    assert (await client.patch(base, json={"title": "所有者"})).status_code == 200
    assert len(await rows(app)) == count
    assert (await admin.delete(base)).status_code == 204
    assert (await detail(admin, first_event))["version_status"] == "missing"
    assert (await admin.delete(f"{USERS}/{owner['id']}")).status_code == 409
    recycled = (await admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"][0]
    assert (await admin.post(
        f"/api/v1/admin/recycle-bin/projects/{p['id']}/purge",
        json={"deleted_at": recycled["deleted_at"], "title": recycled["title"], "confirm": True},
    )).status_code == 204
    assert (await admin.delete(f"{USERS}/{owner['id']}")).status_code == 204
    assert (await admin.delete(f"{USERS}/{second_data['id']}")).status_code == 204
    d = await detail(admin, events[1])
    assert d["actor_username"] == "second-admin" and d["owner_username"] == "owner"
    assert d["project_title"] == "历史书名" and d["target_label"] == "章名"


async def test_chapter_cast_outlines_versions_success_and_business_failures(admin, client, app):
    await register(client, "owner")
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    outline = (await client.post(f"{base}/outlines", json={"title": "大纲", "chapter_no": 1})).json()
    r = await admin.post(f"{base}/chapters/from-outlines", json={"outline_ids": [outline["id"]]})
    assert r.status_code == 201, r.text
    c = r.json()[0]
    path = f"{base}/chapters/{c['id']}"
    char = (await client.post(f"{base}/characters", json={"name": "人物"})).json()
    assert (await admin.put(f"{path}/cast", json={"character_ids": [char["id"]]})).status_code == 200
    assert (await admin.put(f"{path}/cast", json={"character_ids": [999999]})).status_code == 400
    assert (await admin.patch(path, json={"content": "版本正文"})).status_code == 200
    version = (await admin.get(f"{path}/versions")).json()[0]
    vp = f"{path}/versions/{version['id']}"
    assert (await admin.patch(vp, json={"starred": True, "note": "不得进入审计的备注"})).status_code == 200
    assert (await admin.delete(vp)).status_code == 400
    assert (await admin.patch(path, json={"content": "第二版"})).status_code == 200
    current = (await admin.get(path)).json()
    assert (await admin.post(
        f"{vp}/restore", json={"expected_content_revision": current["content_revision"]},
    )).status_code == 200
    assert (await admin.delete(vp)).status_code == 204
    assert (await admin.delete(path)).status_code == 204
    events = await rows(app)
    assert [e.action for e in events] == [
        "chapter.create_from_outlines",
        "chapter.cast.update",
        "chapter.cast.update",
        "chapter.update",
        "chapter_version.update",
        "chapter_version.delete",
        "chapter.update",
        "chapter_version.restore",
        "chapter_version.delete",
        "chapter.delete",
    ]
    assert events[0].summary["affected_count"] == 1
    assert events[2].result == events[5].result == "failure"
    assert "不得进入审计的备注" not in json.dumps([e.summary for e in events])


async def test_global_skills_import_and_workshop_single_events(admin, client, app):
    await register(client, "owner")
    req = {"slug": "audit-test", "name": "技能", "scope": "global", "body": "正文机密"}
    r = await admin.post("/api/v1/skills", json=req)
    assert r.status_code == 201, r.text
    skill_id = r.json()["id"]
    path = f"/api/v1/skills/{skill_id}"
    assert (await admin.patch(path, json={"body": "新正文机密"})).status_code == 200
    assert (
        await admin.put(f"{path}/references", json={"references": [{"title": "资料", "content": "参考机密"}]})
    ).status_code == 200
    assert (await admin.post(f"{path}/restore-builtin")).status_code == 400
    assert (await admin.delete(path)).status_code == 204
    builtin = (await admin.get("/api/v1/skills?scope=global")).json()[0]
    assert (await admin.post(f"/api/v1/skills/{builtin['id']}/restore-builtin")).status_code == 200
    content = b"---\nname: imported-audit\ndescription: imported\n---\nimport-body-secret"
    r = await admin.post("/api/v1/skills/import", data={"scope": "global"}, files={"file": ("SKILL.md", content)})
    assert r.status_code == 201, r.text
    assert (
        await admin.post(
            "/api/v1/skills/import", data={"scope": "global"}, files={"file": ("bad.md", b"secret-invalid")}
        )
    ).status_code == 400
    skill_events = await rows(app)
    assert [e.action for e in skill_events] == [
        "skill.create",
        "skill.update",
        "skill.references.replace",
        "skill.restore_builtin",
        "skill.delete",
        "skill.restore_builtin",
        "skill.import",
        "skill.import",
    ]
    # 私有创建/复制不记
    await admin.post("/api/v1/skills", json={"slug": "private", "name": "私有", "body": "私有正文"})
    await admin.post(f"/api/v1/skills/{builtin['id']}/copy")
    assert len(await rows(app)) == len(skill_events)
    workshop = "/api/v1/workshop"
    body = {"kind": "style", "name": "工坊样式", "content": {"extra": "工坊内容机密"}}
    publish = await admin.post(f"{workshop}/admin/items", json=body)
    assert publish.status_code == 201, publish.text
    item = publish.json()["id"]
    assert (
        await admin.patch(f"{workshop}/admin/items/{item}", json={"category": "other", "tags": ["测试"]})
    ).status_code == 200
    for action in ("hide", "unhide"):
        assert (await admin.post(f"{workshop}/items/{item}/{action}")).status_code == 200
    pending = (await client.post(f"{workshop}/submissions", json=body)).json()["id"]
    assert (await admin.post(f"{workshop}/admin/submissions/{pending}/approve", json={})).status_code == 200
    assert (await admin.post(f"{workshop}/admin/submissions/{pending}/approve", json={})).status_code == 409
    rejected = (await client.post(f"{workshop}/submissions", json=body)).json()["id"]
    assert (
        await admin.post(f"{workshop}/admin/submissions/{rejected}/reject", json={"note": "拒绝原因机密"})
    ).status_code == 200
    await admin.post(f"{workshop}/items/{item}/like")
    await admin.post(f"{workshop}/items/{item}/import")
    assert (await admin.delete(f"{workshop}/admin/items/{item}")).status_code == 204
    events = await rows(app)
    assert [e.action for e in events[len(skill_events) :]] == [
        "workshop.publish",
        "workshop.update",
        "workshop.hide",
        "workshop.unhide",
        "workshop.approve",
        "workshop.approve",
        "workshop.reject",
        "workshop.delete",
    ]
    published = next(e for e in events if e.action == "workshop.publish")
    assert published.summary["body_chars_before"] == 0
    assert published.summary["body_chars_after"] == len("工坊内容机密")
    blob = json.dumps([e.__dict__ for e in events], default=lambda v: None)
    for text in ("正文机密", "参考机密", "import-body-secret", "secret-invalid", "工坊内容机密", "拒绝原因机密"):
        assert text not in blob


async def test_filters_stable_order_literal_search_pagination_and_safe_failure_target(admin, app):
    async with app.state.sessionmaker() as db:
        for label in ("literal%_\\", "literal%_\\", "else"):
            db.add(
                AdminAuditLog(
                    operation_id=str(uuid4()),
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                    actor_user_id=123,
                    actor_username=label,
                    action="user.delete",
                    result="success",
                    target_type="user",
                    target_id=45,
                    target_label="历史用户",
                    summary={},
                )
            )
        await db.commit()
    r = await admin.get(
        URL,
        params={
            "actor_q": "%_\\",
            "actor_user_id": 123,
            "target_type": "user",
            "target_id": 45,
            "action": "user.delete",
            "result": "success",
            "page_size": 1,
            **RANGE,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 2 and r.json()["items"][0]["id"] == 2
    r = await admin.get(URL, params={"actor_q": "%_\\", "page_size": 1, "page": 2})
    assert r.json()["items"][0]["id"] == 1
    assert (await admin.get(URL, params={"actor_q": "no-such"})).json()["total"] == 0
    for params in (
        {"from_at": RANGE["from_at"]},
        {"page": 0},
        {"page_size": 101},
        {"from_at": "2026-01-01T00:00:00", "to_at": RANGE["to_at"]},
        {"from_at": RANGE["to_at"], "to_at": RANGE["from_at"]},
    ):
        assert (await admin.get(URL, params=params)).status_code == 422
    assert (await admin.get(f"{URL}/9999")).status_code == 404
    assert (await admin.delete("/api/v1/projects/99999")).status_code == 404
    event = (await rows(app))[-1]
    assert event.result == "failure" and event.target_id == 99999
    assert event.owner_user_id is None and event.target_label is None


async def test_audit_write_failure_rolls_back_business_and_safe_system_failure(admin, client, app, monkeypatch):
    await register(client, "owner")
    p = await create_project(client)
    original = AsyncSession.flush
    failed = False

    async def fail_success_once(db, objects=None):
        nonlocal failed
        if not failed and any(isinstance(o, AdminAuditLog) and o.result == "success" for o in db.new):
            failed = True
            raise RuntimeError("secret SQL/password request must never be audited")
        return await original(db, objects)

    monkeypatch.setattr(AsyncSession, "flush", fail_success_once)
    # 用 service 捕获真实异常，HTTP 全局 handler 的通用 500 行为由已有测试覆盖。
    from app.schemas.project import ProjectUpdate
    from app.services.project_service import update_project

    async with app.state.sessionmaker() as db:
        actor = await db.scalar(select(User).where(User.username == "manager"))
        with pytest.raises(AuditOperationError, match="服务器内部错误"):
            await update_project(db, actor.id, p["id"], ProjectUpdate(title="不应提交"))
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, p["id"])).title == p["title"]
    event = (await rows(app))[0]
    assert event.result == "failure" and event.error_code == "system_error"
    assert "secret" not in json.dumps(event.__dict__, default=lambda v: None)


async def test_user_create_audit_failure_and_postcommit_projection_failure(admin, app, monkeypatch):
    from app.schemas.admin import AdminUserCreate

    original = AuditOperation._event

    def fail(self, result, **kwargs):
        if result == "success":
            raise RuntimeError("audit unavailable")
        return original(self, result, **kwargs)

    monkeypatch.setattr(AuditOperation, "_event", fail)
    async with app.state.sessionmaker() as db:
        actor = await db.scalar(select(User).where(User.username == "manager"))
        with pytest.raises(AuditOperationError, match="服务器内部错误"):
            await admin_service.create_user(db, actor, AdminUserCreate(username="rolled-back", password="secret123"))
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(User.id).where(User.username == "rolled-back")) is None
    monkeypatch.setattr(AuditOperation, "_event", original)

    def output_error(*args):
        raise RuntimeError("post commit response")

    monkeypatch.setattr(admin_service, "_to_out", output_error)
    async with app.state.sessionmaker() as db:
        actor = await db.scalar(select(User).where(User.username == "manager"))
        with pytest.raises(AuditOperationError, match="服务器内部错误"):
            await admin_service.create_user(db, actor, AdminUserCreate(username="committed", password="secret123"))
    events = await rows(app)
    assert [e.result for e in events] == ["failure", "success"]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(User.id).where(User.username == "committed")) is not None


async def test_commit_confirmation_lost_never_adds_failure(admin, app, monkeypatch):
    from app.schemas.admin import AdminUserCreate

    original = AsyncSession.commit
    once = False

    async def commit_then_fail(db):
        nonlocal once
        await original(db)
        if not once:
            once = True
            raise RuntimeError("connection acknowledgement lost")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_fail)
    async with app.state.sessionmaker() as db:
        actor = await db.scalar(select(User).where(User.username == "manager"))
        with pytest.raises(AuditOperationError, match="服务器内部错误"):
            await admin_service.create_user(db, actor, AdminUserCreate(username="persisted", password="secret123"))
    assert [e.result for e in await rows(app)] == ["success"]


async def preview(admin, dates=RANGE):
    response = await admin.post(f"{URL}/cleanup-preview", json=dates)
    assert response.status_code == 200, response.text
    return response.json()


async def execute(admin, token):
    return await admin.post(f"{URL}/cleanup", json={"preview_token": token, "confirm": True})


async def test_cleanup_half_open_utc_high_water_protection_and_actual_count(admin, app):
    async with app.state.sessionmaker() as db:
        for hour, action in ((0, "user.delete"), (1, "user.delete"), (2, "user.delete"), (1, "audit.cleanup")):
            db.add(
                AdminAuditLog(
                    operation_id=str(uuid4()),
                    occurred_at=datetime(2026, 1, 1, hour, tzinfo=UTC),
                    actor_user_id=99,
                    actor_username="historic",
                    action=action,
                    result="failure",
                    target_type="audit" if action == "audit.cleanup" else "user",
                    summary={},
                )
            )
        await db.commit()
    dates = {"from_at": "2026-01-01T09:00:00+08:00", "to_at": "2026-01-01T10:00:00+08:00"}
    p = await preview(admin, dates)
    assert p["count"] == 1 and p["protected_count"] == 1
    # 预览不删除；同范围后插入 ID 超过高水位也不删除。
    assert len(await rows(app)) == 4
    async with app.state.sessionmaker() as db:
        db.add(
            AdminAuditLog(
                operation_id=str(uuid4()),
                occurred_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                actor_user_id=99,
                actor_username="new",
                action="user.delete",
                result="success",
                target_type="user",
                summary={},
            )
        )
        await db.commit()
    result = await execute(admin, p["preview_token"])
    assert result.status_code == 200, result.text
    assert result.json()["deleted_count"] == 1
    events = await rows(app)
    assert {e.id for e in events} == {1, 3, 4, 5, 6}
    assert events[-1].summary["deleted_count"] == 1
    assert events[-1].summary["expected_count"] == 1
    assert (await execute(admin, p["preview_token"])).status_code == 409
    for _ in range(2):
        p = await preview(admin)
        result = await execute(admin, p["preview_token"])
        assert result.status_code == 200
    assert all(e.action == "audit.cleanup" for e in await rows(app))
    assert len(await rows(app)) == 4
    # 同时预览后别的管理员清理导致 actual=0，不能误删新行。
    p = await preview(admin)
    assert (await execute(admin, p["preview_token"])).json()["deleted_count"] == 0


@pytest.mark.parametrize("mode", ["expired", "tampered", "purpose", "actor", "shape", "login"])
async def test_cleanup_bad_tokens_safe_protected_failure(admin, app, mode):
    p = await preview(admin)
    secret = app.state.settings.jwt_secret
    payload = jwt.decode(p["preview_token"], secret, algorithms=["HS256"])
    status, code = 400, "invalid_preview"
    if mode == "expired":
        payload["iat"] -= 1000
        payload["exp"] -= 1000
    elif mode == "purpose":
        payload["purpose"] = "login"
    elif mode == "actor":
        payload["actor_user_id"] += 1
        status, code = 403, "preview_actor_mismatch"
    elif mode == "shape":
        payload["high_water_id"] = -1
    elif mode == "login":
        payload = {
            "sub": "1",
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iat": int(datetime.now(UTC).timestamp()),
        }
    token = jwt.encode(payload, secret, algorithm="HS256")
    if mode == "tampered":
        a, b, c = token.split(".")
        token = f"{a}.{b}.{'A' if c[0] != 'A' else 'B'}{c[1:]}"
    response = await execute(admin, token)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    events = await rows(app)
    assert len(events) == 1 and events[0].action == "audit.cleanup" and events[0].result == "failure"
    assert token not in json.dumps(events[0].__dict__, default=lambda v: None)
    p = await preview(admin)
    assert p["protected_count"] == 1 and p["count"] == 0


async def test_cleanup_requires_confirm_and_valid_aware_dates(admin, app):
    for data in (
        {},
        {"from_at": RANGE["from_at"]},
        {**RANGE, "to_at": RANGE["from_at"]},
        {**RANGE, "from_at": "2026-01-01T00:00:00"},
    ):
        assert (await admin.post(f"{URL}/cleanup-preview", json=data)).status_code == 422
    for data in ({}, {"preview_token": "x"}, {"preview_token": "x", "confirm": False}):
        assert (await admin.post(f"{URL}/cleanup", json=data)).status_code == 422
    assert await rows(app) == []


@pytest.mark.parametrize("failure", ["delete", "event", "commit"])
async def test_cleanup_atomic_failure_keeps_rows_and_protected_failure(admin, app, monkeypatch, failure):
    await admin.post(USERS, json={"username": "keep", "password": "secret123"})
    p = await preview(admin)
    original_execute = AsyncSession.execute
    original_event = AuditOperation._event
    original_commit = AsyncSession.commit
    once = False

    async def execute_error(db, statement, *args, **kwargs):
        nonlocal once
        if not once and getattr(statement, "is_delete", False):
            once = True
            raise RuntimeError("delete secret")
        return await original_execute(db, statement, *args, **kwargs)

    def event_error(audit, result, **kwargs):
        if result == "success":
            raise RuntimeError("event secret")
        return original_event(audit, result, **kwargs)

    async def commit_error(db):
        nonlocal once
        if not once:
            once = True
            raise RuntimeError("commit secret")
        return await original_commit(db)

    if failure == "delete":
        monkeypatch.setattr(AsyncSession, "execute", execute_error)
    elif failure == "event":
        monkeypatch.setattr(AuditOperation, "_event", event_error)
    else:
        monkeypatch.setattr(AsyncSession, "commit", commit_error)
    from app.schemas.audit import AuditCleanupRequest

    async with app.state.sessionmaker() as db:
        actor = await db.scalar(select(User).where(User.username == "manager"))
        with pytest.raises(AuditOperationError, match="服务器内部错误"):
            await audit_service.cleanup(
                db,
                actor,
                AuditCleanupRequest(preview_token=p["preview_token"], confirm=True),
                app.state.settings.jwt_secret,
            )
    events = await rows(app)
    assert len(events) == 2 and events[0].action == "user.create"
    assert events[1].action == "audit.cleanup" and events[1].result == "failure"
    assert events[1].summary["deleted_count"] is None
    assert (await execute(admin, p["preview_token"])).status_code == 409


async def test_oversized_global_skill_import_still_rejected_without_content(admin, app):
    secret = b"oversized-upload-secret"
    response = await admin.post(
        "/api/v1/skills/import",
        data={"scope": "global"},
        files={"file": ("private-filename-secret.md", secret * 100000)},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Skill 文件过大，上限为 2MB"
    events = await rows(app)
    assert len(events) == 1 and events[0].action == "skill.import" and events[0].result == "failure"
    text = json.dumps(events[0].__dict__, default=lambda v: None)
    assert "oversized-upload-secret" not in text and "private-filename-secret" not in text


async def test_global_skill_slug_conflict_is_409_and_not_audit_integrity_failure(admin, app):
    for slug in ("occupied", "editable"):
        response = await admin.post("/api/v1/skills", json={"scope": "global", "slug": slug, "name": slug})
        assert response.status_code == 201
    skill_id = response.json()["id"]
    response = await admin.patch(f"/api/v1/skills/{skill_id}", json={"slug": "occupied"})
    assert response.status_code == 409, response.text
    event = (await rows(app))[-1]
    assert event.action == "skill.update" and event.result == "failure" and event.error_code == "conflict"
    assert (await admin.get(f"/api/v1/skills/{skill_id}")).json()["slug"] == "editable"


async def test_cleanup_same_nonce_concurrent_only_one_result(admin, app):
    import asyncio

    await admin.post(USERS, json={"username": "concurrent", "password": "secret123"})
    p = await preview(admin)
    responses = await asyncio.gather(execute(admin, p["preview_token"]), execute(admin, p["preview_token"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    events = await rows(app)
    assert len(events) == 1 and events[0].action == "audit.cleanup" and events[0].result == "success"


async def test_cleanup_parallel_preview_actual_count_can_differ(admin, app):
    await admin.post(USERS, json={"username": "parallel", "password": "secret123"})
    first = await preview(admin)
    second = await preview(admin)
    assert first["count"] == second["count"] == 1
    assert (await execute(admin, first["preview_token"])).json()["deleted_count"] == 1
    result = await execute(admin, second["preview_token"])
    assert result.status_code == 200 and result.json()["deleted_count"] == 0
    events = await rows(app)
    assert len(events) == 2 and events[-1].summary["expected_count"] == 1
    assert events[-1].summary["deleted_count"] == 0


async def test_summary_whitelist_rejects_free_text_and_unbounded_values(admin, app):
    from pydantic import ValidationError

    from app.schemas.audit import AuditSummary, AuditValueChange

    for data in ({"password": "secret"}, {"changed_fields": ["password"]}, {"affected_ids": list(range(101))}):
        with pytest.raises(ValidationError):
            AuditSummary.model_validate(data)
    with pytest.raises(ValidationError):
        AuditValueChange(field="status", before="secret-value", after="draft")


async def test_registered_action_failures_keep_safe_identity_without_false_success(admin, client, app):
    await register(client, "owner")
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "存在章节"})).json()
    chapter_path = f"{base}/chapters/{chapter['id']}"
    checks = [
        ("user.update", "PATCH", f"{USERS}/99999", {"is_active": False}, 404),
        ("user.reset_password", "POST", f"{USERS}/99999/reset-password", {"new_password": "private-secret"}, 404),
        ("user.delete", "DELETE", f"{USERS}/99999", None, 404),
        ("project.update", "PATCH", "/api/v1/projects/99999", {"title": "未保存"}, 404),
        ("project.delete", "DELETE", "/api/v1/projects/99999", None, 404),
        ("chapter.create", "POST", f"{base}/chapters", {"outline_id": 99999, "content": "private-secret"}, 400),
        ("chapter.create_from_outlines", "POST", f"{base}/chapters/from-outlines", {"outline_ids": [99999]}, 400),
        ("chapter.update", "PATCH", f"{base}/chapters/99999", {"content": "private-secret"}, 404),
        ("chapter.delete", "DELETE", f"{base}/chapters/99999", None, 404),
        ("chapter.cast.update", "PUT", f"{base}/chapters/99999/cast", {"character_ids": []}, 404),
        ("chapter_version.update", "PATCH", f"{chapter_path}/versions/99999", {"note": "private-secret"}, 404),
        ("chapter_version.delete", "DELETE", f"{chapter_path}/versions/99999", None, 404),
        ("chapter_version.restore", "POST", f"{chapter_path}/versions/99999/restore", None, 404),
        ("workshop.approve", "POST", "/api/v1/workshop/admin/submissions/99999/approve", {}, 404),
        ("workshop.reject", "POST", "/api/v1/workshop/admin/submissions/99999/reject", {"note": "private-secret"}, 404),
        ("workshop.update", "PATCH", "/api/v1/workshop/admin/items/99999", {"tags": []}, 404),
        ("workshop.hide", "POST", "/api/v1/workshop/items/99999/hide", None, 404),
        ("workshop.unhide", "POST", "/api/v1/workshop/items/99999/unhide", None, 404),
        ("workshop.delete", "DELETE", "/api/v1/workshop/admin/items/99999", None, 404),
        ("skill.update", "PATCH", "/api/v1/skills/99999", {"body": "private-secret"}, 404),
        ("skill.references.replace", "PUT", "/api/v1/skills/99999/references", {"references": []}, 404),
        ("skill.delete", "DELETE", "/api/v1/skills/99999", None, 404),
        ("skill.restore_builtin", "POST", "/api/v1/skills/99999/restore-builtin", None, 404),
    ]
    for action, method, path, body, status in checks:
        response = await admin.request(method, path, json=body)
        assert response.status_code == status, (action, response.text)
    events = await rows(app)
    assert [event.action for event in events] == [check[0] for check in checks]
    assert all(event.result == "failure" for event in events)
    assert "private-secret" not in json.dumps([event.__dict__ for event in events], default=lambda v: None)


async def test_builtin_restore_noop_and_batch_ids_are_bounded(admin, app):
    builtin = (await admin.get("/api/v1/skills?scope=global")).json()[0]
    response = await admin.post(f"/api/v1/skills/{builtin['id']}/restore-builtin")
    assert response.status_code == 200
    assert (await rows(app))[0].summary["changed_fields"] == []
    async with app.state.sessionmaker() as db:
        actor = await db.scalar(select(User).where(User.username == "manager"))
        async with AuditOperation(db, actor, "chapter.create_from_outlines", "project", 99) as audit:
            audit.affected(list(range(1, 151)))
            await audit.commit()
    event = (await rows(app))[-1]
    assert event.summary["affected_count"] == 150 and event.summary["ids_truncated"] is True
    assert event.summary["affected_ids"] == list(range(1, 101))


@pytest.mark.parametrize("phase", ["business_flush", "version_primitive"])
async def test_version_unique_competition_remains_safe_409(admin, client, app, monkeypatch, phase):
    import sqlite3

    from sqlalchemy.exc import IntegrityError

    await register(client, "owner")
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}/chapters"
    c = (await client.post(base, json={"title": "原名", "content": "原文"})).json()
    original = AsyncSession.flush
    once = False

    async def conflict(db, objects=None):
        nonlocal once
        model = Chapter if phase == "business_flush" else ChapterVersion
        business_changed = any(isinstance(obj, model) for obj in [*db.dirty, *db.new])
        if not once and business_changed:
            once = True
            raise IntegrityError(
                "INSERT private-sql",
                {"content": "private-parameter"},
                sqlite3.IntegrityError("UNIQUE constraint failed: chapter_versions.chapter_id, chapter_versions.seq"),
            )
        return await original(db, objects)

    monkeypatch.setattr(AsyncSession, "flush", conflict)
    data = {"title": "不得提交"} if phase == "business_flush" else {"content": "不得提交正文"}
    response = await admin.patch(f"{base}/{c['id']}", json=data)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "正文同时被其他操作修改，请刷新后重试"
    event = (await rows(app))[-1]
    assert event.result == "failure" and event.error_code == "conflict"
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, c["id"])
        assert chapter.title == "原名" and chapter.content == "原文"


async def test_audit_system_database_error_does_not_reach_raw_exception_logger(admin, client, app, monkeypatch, caplog):
    import sqlite3

    from sqlalchemy.exc import IntegrityError

    await register(client, "owner")
    p = await create_project(client)
    original = AsyncSession.flush
    once = False

    async def database_error(db, objects=None):
        nonlocal once
        if not once and any(isinstance(obj, AdminAuditLog) and obj.result == "success" for obj in db.new):
            once = True
            raise IntegrityError(
                "INSERT raw-sql-secret",
                {"password": "raw-password-secret", "content": "raw-body-secret"},
                sqlite3.IntegrityError("unrecognized-private-database-error"),
            )
        return await original(db, objects)

    monkeypatch.setattr(AsyncSession, "flush", database_error)
    response = await admin.patch(f"/api/v1/projects/{p['id']}", json={"title": "不得提交"})
    assert response.status_code == 500 and response.json() == {"detail": "服务器内部错误"}
    assert (await rows(app))[-1].error_code == "system_error"
    blob = caplog.text + response.text + json.dumps([e.__dict__ for e in await rows(app)], default=lambda v: None)
    for secret in ("raw-sql-secret", "raw-password-secret", "raw-body-secret", "unrecognized-private-database-error"):
        assert secret not in blob


async def test_skill_create_and_workshop_publish_business_rejections_are_audited(admin, app):
    request = {"slug": "duplicate-create", "name": "全局技能", "scope": "global"}
    assert (await admin.post("/api/v1/skills", json=request)).status_code == 201
    response = await admin.post("/api/v1/skills", json=request)
    assert response.status_code == 409
    response = await admin.post(
        "/api/v1/workshop/admin/items",
        json={
            "kind": "prompt",
            "prompt_key": "nonexistent-template",
            "name": "未发布模板",
            "content": {"user": "publish-body-must-not-be-audited"},
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "提示词模板不存在，无法分享"
    events = await rows(app)
    assert [(event.action, event.result) for event in events] == [
        ("skill.create", "success"),
        ("skill.create", "failure"),
        ("workshop.publish", "failure"),
    ]
    assert events[1].error_code == "conflict" and events[2].error_code == "bad_request"
    assert "publish-body-must-not-be-audited" not in json.dumps(
        [event.__dict__ for event in events], default=lambda value: None
    )


async def test_private_skill_denial_audits_only_safe_id_and_private_writes_are_excluded(admin, client, app):
    await register(client, "private-owner")
    private = await client.post(
        "/api/v1/skills", json={"slug": "private-source", "name": "private-name-secret", "body": "private-body-secret"}
    )
    assert private.status_code == 201
    skill_id = private.json()["id"]
    response = await admin.patch(f"/api/v1/skills/{skill_id}", json={"body": "rejected-body-secret"})
    assert response.status_code == 404 and response.json()["detail"] == "Skill 不存在"
    events = await rows(app)
    assert len(events) == 1 and events[0].result == "failure" and events[0].target_id == skill_id
    assert events[0].target_label is None and events[0].owner_user_id is None
    assert events[0].owner_username is None
    output = json.dumps(await detail(admin, events[0]))
    for secret in ("private-name-secret", "private-body-secret", "rejected-body-secret", "private-owner"):
        assert secret not in output
    own = await admin.post("/api/v1/skills", json={"slug": "private-mine", "name": "我的私有技能"})
    assert own.status_code == 201
    assert (await admin.patch(f"/api/v1/skills/{own.json()['id']}", json={"body": "私有更新"})).status_code == 200
    assert len(await rows(app)) == 1
