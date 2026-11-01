"""单用户安全详情、当前资源用量与成功登录时间。"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.deps import get_db
from app.core.security import decode_access_token, hash_password
from app.models import AdminAuditLog, Chapter, ChapterVersion, Project, Task, User
from app.schemas.auth import ChangePasswordRequest, Credentials
from app.services import admin_service, auth_service
from tests.helpers import TEST_JWT_SECRET, create_project, purge_trashed_project, register

USERS = "/api/v1/admin/users"
LOGIN = "/api/v1/auth/login"
ZERO = {"project_count": 0, "chapter_count": 0, "total_words": 0}
T0 = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def utc(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


async def stored_user(app, user_id):
    async with app.state.sessionmaker() as db:
        return await db.get(User, user_id)


def clock(monkeypatch, value):
    monkeypatch.setattr(auth_service, "_now_utc", lambda: value, raising=False)


async def test_details_permissions_missing_target_and_zero_resources(app, client, system_admin):
    assert (await client.get(f"{USERS}/9999")).status_code == 401
    alice = await register(client)
    for target in (alice["id"], 9999):
        response = await client.get(f"{USERS}/{target}")
        assert response.status_code == 403
        assert response.json()["detail"] == "需要管理员权限"
    missing = await system_admin.get(f"{USERS}/9999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "用户不存在"}
    assert (await system_admin.get(f"{USERS}/invalid")).status_code == 422

    created = await system_admin.post(USERS, json={"username": "never_logged_in", "password": "secret123"})
    assert created.status_code == 201
    async with app.state.sessionmaker() as db:
        audit_count = await db.scalar(select(func.count(AdminAuditLog.id)))
        task_count = await db.scalar(select(func.count(Task.id)))
    response = await system_admin.get(f"{USERS}/{created.json()['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id", "username", "is_admin", "is_active", "created_at", "project_count", "last_login_at", "usage",
        "is_super_admin", "is_management_account", "roles", "authorization_revision",
    }
    assert body["username"] == "never_logged_in" and body["last_login_at"] is None
    assert body["project_count"] == 0
    assert body["usage"] == {"active": ZERO, "recycled": ZERO, "deleting": ZERO}
    assert (await stored_user(app, body["id"])).last_login_at is None
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(AdminAuditLog.id))) == audit_count
        assert await db.scalar(select(func.count(Task.id))) == task_count


@pytest.mark.parametrize("field,status", [("is_admin", 403), ("is_active", 401)])
async def test_details_recheck_current_admin_authority(app, system_admin, field, status):
    actor = (await system_admin.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == actor["id"]).values(**{field: False}))
        await db.commit()
    assert (await system_admin.get(f"{USERS}/{actor['id']}")).status_code == status


async def test_details_count_all_projects_once_and_only_current_owner_content(app, system_admin, fake_llm):
    async with app.state.sessionmaker() as db:
        owner = User(username="resource_owner", password_hash="private_hash")
        other = User(username="other_owner", password_hash="another_private_hash")
        db.add_all([owner, other])
        await db.flush()
        active = Project(user_id=owner.id, title="正常有章节")
        empty = Project(user_id=owner.id, title="正常空作品")
        recycled = Project(user_id=owner.id, title="回收站", deleted_at=T0)
        expired = Project(user_id=owner.id, title="到期待清理", deleted_at=T0 - timedelta(days=31))
        deleting = Project(user_id=owner.id, title="删除处理中", deletion_started_at=T0, deletion_token="intent")
        foreign = Project(user_id=other.id, title="其他用户")
        db.add_all([active, empty, recycled, expired, deleting, foreign])
        await db.flush()
        chapters = [
            Chapter(project_id=active.id, chapter_no=1, content="正文甲乙", word_count=4),
            Chapter(project_id=active.id, chapter_no=2, content="丙丁戊", word_count=3),
            Chapter(project_id=active.id, chapter_no=3, content="", word_count=0),
            Chapter(project_id=recycled.id, content="甲" * 5, word_count=5),
            Chapter(project_id=expired.id, content="乙" * 6, word_count=6),
            Chapter(project_id=deleting.id, content="丙" * 13, word_count=13),
            Chapter(project_id=foreign.id, content="私" * 99, word_count=99),
        ]
        db.add_all(chapters)
        await db.flush()
        db.add(ChapterVersion(chapter_id=chapters[0].id, seq=1, source="manual", content="旧" * 80, word_count=80))
        await db.commit()
        owner_id = owner.id

    body = (await system_admin.get(f"{USERS}/{owner_id}")).json()
    assert body["project_count"] == 5
    assert body["usage"] == {
        "active": {"project_count": 2, "chapter_count": 3, "total_words": 7},
        "recycled": {"project_count": 2, "chapter_count": 2, "total_words": 11},
        "deleting": {"project_count": 1, "chapter_count": 1, "total_words": 13},
    }
    assert sum(group["project_count"] for group in body["usage"].values()) == body["project_count"]
    assert "private_hash" not in str(body) and "私" not in str(body)
    assert fake_llm.calls == []

    statements = []

    def capture(_conn, _cursor, statement, _parameters, context, _executemany):
        if context.execution_options.get("details_projection_test"):
            statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with app.state.sessionmaker() as db:
            await db.connection(execution_options={"details_projection_test": True})
            detail = await admin_service.get_user_detail(db, owner_id)
            assert detail.project_count == 5
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert "password_hash" not in statements[0] and "token_version" not in statements[0]
    assert "chapters.content" not in statements[0] and "chapter_versions" not in statements[0]


async def test_details_follow_actual_soft_delete_restore_and_purge(app, client, system_admin):
    owner = await register(client)
    project = await create_project(client)
    path = f"{USERS}/{owner['id']}"
    assert (await system_admin.get(path)).json()["usage"]["active"]["project_count"] == 1
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    body = (await system_admin.get(path)).json()
    assert body["usage"]["active"] == ZERO
    assert body["usage"]["recycled"]["project_count"] == 1
    assert body["project_count"] == 1
    listing = (await system_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"]
    row = next(item for item in listing if item["id"] == project["id"])
    restored = await system_admin.post(
        f"/api/v1/admin/recycle-bin/projects/{project['id']}/restore",
        json={"deleted_at": row["deleted_at"], "confirm": True},
    )
    assert restored.status_code == 204, restored.text
    body = (await system_admin.get(path)).json()
    assert body["usage"]["active"]["project_count"] == 1 and body["usage"]["recycled"] == ZERO
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    await purge_trashed_project(system_admin, project["id"])
    body = (await system_admin.get(path)).json()
    assert body["project_count"] == 0 and all(group == ZERO for group in body["usage"].values())


async def test_successful_registration_and_login_are_the_only_login_time_updates(app, client, monkeypatch):
    clock(monkeypatch, T0)
    user = await register(client)
    first = await stored_user(app, user["id"])
    assert utc(first.last_login_at) == T0
    assert set(user) == {
        "id", "username", "is_admin", "created_at", "is_super_admin", "roles",
        "effective_permissions", "authorization_revision",
    }
    clock(monkeypatch, T1)
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    changed = await client.post(
        "/api/v1/auth/change-password", json={"old_password": "secret123", "new_password": "newsecret123"},
    )
    assert changed.status_code == 204 and "set-cookie" in changed.headers
    assert utc((await stored_user(app, user["id"])).last_login_at) == T0
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == user["id"]).values(updated_at=T0 - timedelta(days=10)))
        await db.commit()
    before_login = await stored_user(app, user["id"])
    logged_in = await client.post(LOGIN, json={"username": "alice", "password": "newsecret123"})
    assert logged_in.status_code == 200 and "set-cookie" in logged_in.headers
    after_login = await stored_user(app, user["id"])
    assert utc(after_login.last_login_at) == T1
    assert after_login.updated_at == before_login.updated_at


async def test_failed_and_disabled_logins_never_update_time(app, client, monkeypatch):
    clock(monkeypatch, T0)
    user = await register(client)
    clock(monkeypatch, T1)
    for username in ("alice", "unknown_user"):
        response = await client.post(LOGIN, json={"username": username, "password": "incorrect123"})
        assert response.status_code == 401 and "set-cookie" not in response.headers
        assert response.json()["detail"] == "用户名或密码错误"
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == user["id"]).values(is_active=False))
        await db.commit()
    wrong = await client.post(LOGIN, json={"username": "alice", "password": "incorrect123"})
    assert wrong.status_code == 401
    disabled = await client.post(LOGIN, json={"username": "alice", "password": "secret123"})
    assert disabled.status_code == 403 and "set-cookie" not in disabled.headers
    assert disabled.json()["detail"] == "账号已被禁用，请联系管理员"
    assert utc((await stored_user(app, user["id"])).last_login_at) == T0


async def test_admin_creation_and_bootstrap_do_not_invent_logins(app, system_admin, monkeypatch):
    clock(monkeypatch, T0)
    response = await system_admin.post(USERS, json={"username": "created_by_admin", "password": "secret123"})
    assert response.status_code == 201
    assert (await stored_user(app, response.json()["id"])).last_login_at is None
    settings = app.state.settings.model_copy(update={"admin_username": "bootstrapped", "admin_password": "secret123"})
    await auth_service.bootstrap_admin(app.state.sessionmaker, settings)
    async with app.state.sessionmaker() as db:
        bootstrapped = await db.scalar(select(User).where(User.username == "bootstrapped"))
        assert bootstrapped.last_login_at is None


async def test_late_login_cannot_move_timestamp_backwards(app, client, monkeypatch):
    clock(monkeypatch, T1)
    user = await register(client)
    clock(monkeypatch, T0)
    assert (await client.post(LOGIN, json={"username": "alice", "password": "secret123"})).status_code == 200
    assert utc((await stored_user(app, user["id"])).last_login_at) == T1


async def test_concurrent_late_login_compares_against_latest_stored_time(app, client, monkeypatch):
    clock(monkeypatch, T0)
    user = await register(client)
    reached = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(
        auth_service, "_now_utc", lambda: T0 if asyncio.current_task().get_name() == "late_login" else T1,
    )

    async def login_late():
        async with app.state.sessionmaker() as db:
            execute = db.execute

            async def delayed_update(statement, *args, **kwargs):
                if getattr(statement, "is_update", False):
                    reached.set()
                    await release.wait()
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", delayed_update)
            return await auth_service.authenticate(db, Credentials(username="alice", password="secret123"))

    pending = asyncio.create_task(login_late(), name="late_login")
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with app.state.sessionmaker() as db:
            latest = await auth_service.authenticate(db, Credentials(username="alice", password="secret123"))
            assert utc(latest.last_login_at) == T1
    finally:
        release.set()
    late = await asyncio.wait_for(pending, 5)
    assert utc(late.last_login_at) == T1
    assert utc((await stored_user(app, user["id"])).last_login_at) == T1


@pytest.mark.parametrize("action,expected_status", [("disable", 403), ("reset", 401), ("change_password", 401)])
async def test_credentials_are_rechecked_after_password_verification(
    app, client, system_admin, make_client, monkeypatch, action, expected_status,
):
    clock(monkeypatch, T0)
    user = await register(client)
    clock(monkeypatch, T1)
    reached = asyncio.Event()
    release = asyncio.Event()
    real_run_sync = auth_service.to_thread.run_sync

    async def blocked_verify(func, *args, **kwargs):
        result = await real_run_sync(func, *args, **kwargs)
        if func is auth_service.verify_password:
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(auth_service.to_thread, "run_sync", blocked_verify)
    pending_client = make_client()
    pending = asyncio.create_task(pending_client.post(LOGIN, json={"username": "alice", "password": "secret123"}))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        if action == "disable":
            changed = await system_admin.patch(f"{USERS}/{user['id']}", json={"is_active": False})
        elif action == "reset":
            changed = await system_admin.post(
                f"{USERS}/{user['id']}/reset-password", json={"new_password": "newpass123"},
            )
        else:
            # 不再次经过验旧密码阻塞点；本测试聚焦登录已验凭据随后变更的 CAS。
            async with app.state.sessionmaker() as db:
                actor = await db.get(User, user["id"])
                monkeypatch.setattr(auth_service.to_thread, "run_sync", real_run_sync)
                await auth_service.change_password(
                    db, actor, ChangePasswordRequest(old_password="secret123", new_password="newpass123"),
                )
            changed = None
        if changed is not None:
            assert changed.status_code in (200, 204), changed.text
    finally:
        release.set()
    response = await asyncio.wait_for(pending, 5)
    assert response.status_code == expected_status, response.text
    assert "set-cookie" not in response.headers
    assert utc((await stored_user(app, user["id"])).last_login_at) == T0


async def test_login_cookie_keeps_verified_version_when_reset_follows_commit(app, client, monkeypatch):
    user = await register(client)
    new_hash = hash_password("newpass123")

    async def reset_after_commit_db():
        async with app.state.sessionmaker() as db:
            real_commit = db.commit

            async def commit_and_reset():
                await real_commit()
                async with app.state.sessionmaker() as other:
                    await other.execute(
                        update(User).where(User.id == user["id"]).values(
                            password_hash=new_hash, token_version=User.token_version + 1,
                        ),
                    )
                    await other.commit()

            monkeypatch.setattr(db, "commit", commit_and_reset)
            yield db

    app.dependency_overrides[get_db] = reset_after_commit_db
    try:
        response = await client.post(LOGIN, json={"username": "alice", "password": "secret123"})
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200, response.text
    token = decode_access_token(client.cookies.get("access_token"), TEST_JWT_SECRET)
    assert token == (user["id"], 0)
    assert (await stored_user(app, user["id"])).token_version == 1
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.parametrize("route,stage", [("login", "commit"), ("register", "commit"), ("register", "flush")])
async def test_authentication_storage_failure_rolls_back_without_cookie_or_sensitive_logs(
    app, client, monkeypatch, caplog, route, stage,
):
    clock(monkeypatch, T0)
    user = await register(client)
    old = await stored_user(app, user["id"])
    clock(monkeypatch, T1)
    private_marker = f"private-SQL-parameter-{old.password_hash}"

    async def broken_db():
        async with app.state.sessionmaker() as db:
            async def fail_write():
                raise SQLAlchemyError(private_marker)

            monkeypatch.setattr(db, stage, fail_write)
            yield db

    app.dependency_overrides[get_db] = broken_db
    try:
        with caplog.at_level(logging.ERROR):
            response = await client.post(
                f"/api/v1/auth/{route}",
                json={"username": "alice" if route == "login" else "uncommitted_user", "password": "secret123"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 500, response.text
    assert response.json() == {"detail": "登录信息保存失败，请稍后重试"}
    assert "set-cookie" not in response.headers
    assert private_marker not in caplog.text and old.password_hash not in caplog.text
    assert "SQLAlchemyError" in caplog.text
    assert utc((await stored_user(app, user["id"])).last_login_at) == T0
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(User.id).where(User.username == "uncommitted_user")) is None
