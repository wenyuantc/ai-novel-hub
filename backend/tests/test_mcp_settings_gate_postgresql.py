"""跨实例 PostgreSQL 和真实 MCP TCP 验证系统开关物理发送顺序。"""

import asyncio
import json
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from app.models import AdminAuditLog, McpPlugin, SystemSettings, User
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.authorization_service import AuthenticatedActor
from app.services.mcp import client as mcp_client
from app.services.mcp import settings_gate
from tests import pg_support
from tests.helpers import make_settings, register, wait_task
from tests.mcp_fakes import FakeMcpConnector, serve
from tests.test_agent_mcp_skills import call, change_action, send_task, setup
from tests.test_collaboration_access_postgresql import _wait_for_blocker

URL = "/api/v1/admin/system-settings"


@pytest.fixture
async def postgres_mcp_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="MCP 门禁测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2, separate_settings=True, start_first_engine=True,
    ) as apps:
        assert apps[0].state.settings is not apps[1].state.settings
        yield apps


async def _admin(app, client):
    user = await register(client, "mcp-settings-admin")
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    return user


async def _proposal(app, owner, fake_llm, remote_url):
    fake = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake
    _, base, conversation, plugin = await setup(owner, fake)
    app.state.settings.allow_private_ai_endpoints = True
    async with app.state.sessionmaker() as db:
        (await db.get(McpPlugin, plugin["id"])).url = f"{remote_url}/mcp"
        await db.commit()
    app.state.task_engine.ctx.mcp_connector = mcp_client.connect
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(owner, conversation)
    completed = await wait_task(owner, task, within=20, statuses=("paused", "failed"))
    assert completed["status"] == "paused", completed
    change = (await owner.get(conversation)).json()["changes"][0]
    return base, conversation, plugin, change


def _server(case):
    remote = FastAPI()
    initialized, release_init, received, release_headers, body_started, release_body = (
        asyncio.Event() for _ in range(6)
    )
    calls = []

    @remote.post("/mcp")
    async def rpc(request: Request):
        message = await request.json()
        method = message.get("method")
        if method == "initialize":
            initialized.set()
            if case in ("disabled_first", "unknown_commit"):
                await release_init.wait()
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }}, headers={"mcp-session-id": "test-session"})
        if method == "tools/list":
            # 真实 SDK 在成功结果后会拉取工具列表校验输出结构；202 无正文会让请求永久等待。
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "tools": [{"name": "send", "inputSchema": {"type": "object", "properties": {}}}],
            }})
        if method == "tools/call":
            calls.append(message)
            received.set()
            await release_headers.wait()

            async def body():
                body_started.set()
                await release_body.wait()
                yield json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "content": [{"type": "text", "text": "远端已执行"}], "isError": False,
                }}, ensure_ascii=False).encode()
            return StreamingResponse(body(), media_type="application/json")
        return Response(status_code=202)

    @remote.get("/mcp")
    async def get_stream():
        return Response(status_code=405)

    return remote, initialized, release_init, received, release_headers, body_started, release_body, calls


async def _cancel(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


@pytest.mark.parametrize("case", ["disabled_first", "send_first", "cancel"])
async def test_settings_gate_orders_real_mcp_post_and_releases_on_headers_or_cancel(
    postgres_mcp_apps, fake_llm, monkeypatch, case,
):
    owner_app, admin_app = postgres_mcp_apps
    remote, initialized, release_init, received, release_headers, body_started, release_body, calls = _server(case)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://testserver") as admin,
        serve(remote) as remote_url,
    ):
        await _admin(admin_app, admin)
        base, _, _, change = await _proposal(owner_app, owner, fake_llm, remote_url)
        async with owner_app.state.sessionmaker() as db:
            assert await db.scalar(select(SystemSettings.id)) is None  # 首次写入也需与共享门禁串行。
        holder = {}
        original = settings_gate.acquire_send

        async def observed(db, actor):
            await original(db, actor)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))

        monkeypatch.setattr(settings_gate, "acquire_send", observed)
        pending = asyncio.create_task(owner.post(
            f"{base}/changes/{change['id']}/confirm", json=change_action(change),
        ))
        writer = None
        try:
            await asyncio.wait_for(initialized.wait(), 10)
            if case == "disabled_first":
                result = await admin.patch(URL, json={"revision": 0, "changes": {"mcp_enabled": False}})
                assert result.status_code == 200, result.text
                assert owner_app.state.settings.mcp_enabled is True
                assert admin_app.state.settings.mcp_enabled is False
                release_init.set()
                result = await asyncio.wait_for(pending, 15)
                assert result.status_code in (401, 403, 409), result.text
                assert calls == [] and not received.is_set()
            else:
                await asyncio.wait_for(received.wait(), 10)
                assert holder.get("pid")
                writer = asyncio.create_task(admin.patch(
                    URL, json={"revision": 0, "changes": {"mcp_enabled": False}},
                ))
                await asyncio.wait_for(_wait_for_blocker(admin_app.state.sessionmaker, holder["pid"]), 10)
                assert not writer.done()
                if case == "cancel":
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    release_headers.set()
                    await asyncio.wait_for(body_started.wait(), 10)
                result = await asyncio.wait_for(writer, 15)
                assert result.status_code == 200, result.text
                if case == "send_first":
                    assert not pending.done()  # 响应正文仍阻塞，设置写入已经提交。
                    release_body.set()
                    result = await asyncio.wait_for(pending, 15)
                    assert result.status_code == 200 and result.json()["status"] == "executed", result.text
                assert len(calls) == 1
            async with admin_app.state.sessionmaker() as db:
                stored = await db.get(SystemSettings, 1)
                assert stored.revision == 1 and stored.overrides["mcp_enabled"] is False
        finally:
            release_init.set()
            release_headers.set()
            release_body.set()
            await _cancel(pending, writer)


async def test_lost_settings_commit_ack_is_recovered_without_sending_from_stale_instance(
    postgres_mcp_apps, fake_llm, monkeypatch,
):
    owner_app, admin_app = postgres_mcp_apps
    remote, initialized, release_init, _, release_headers, _, release_body, calls = _server("unknown_commit")
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://testserver") as admin,
        serve(remote) as remote_url,
    ):
        admin_user = await _admin(admin_app, admin)
        base, _, _, change = await _proposal(owner_app, owner, fake_llm, remote_url)
        manager = admin_app.state.system_settings
        commit_original, reload_original = AsyncSession.commit, manager._reload
        committed, allow_read = False, False

        async def commit(db):
            nonlocal committed
            target = any(isinstance(row, AdminAuditLog) and row.action == "system_settings.update"
                         for row in db.identity_map.values())
            await commit_original(db)
            if target and not committed:
                committed = True
                raise RuntimeError("模拟提交确认丢失")

        async def reload():
            if committed and not allow_read:
                raise RuntimeError("模拟暂时不能读取")
            return await reload_original()

        monkeypatch.setattr(AsyncSession, "commit", commit)
        monkeypatch.setattr(manager, "_reload", reload)
        pending = asyncio.create_task(owner.post(
            f"{base}/changes/{change['id']}/confirm", json=change_action(change),
        ))
        try:
            await asyncio.wait_for(initialized.wait(), 10)
            result = await admin.patch(URL, json={"revision": 0, "changes": {"mcp_enabled": False}})
            assert result.status_code == 500 and committed and not manager.runtime.settings_confirmed
            async with admin_app.state.sessionmaker() as db:
                user = await db.get(User, admin_user["id"])
                actor = AuthenticatedActor(user.id, user.token_version)
                with pytest.raises(VisibilityPolicyError):
                    await settings_gate.acquire_send(db, actor)
            assert owner_app.state.settings.mcp_enabled is True
            release_init.set()
            result = await asyncio.wait_for(pending, 15)
            assert result.status_code in (401, 403, 409), result.text
            assert calls == []
            allow_read = True
            await manager.load()
            assert manager.runtime.settings_confirmed and manager.settings.mcp_enabled is False
            result = await admin.patch(URL, json={"revision": 1, "changes": {"mcp_enabled": True}})
            assert result.status_code == 200, result.text
            # 即便另一实例内存仍是 false，原数据库门禁也能观察恢复的持久值。
            owner_app.state.settings.mcp_enabled = False
            async with owner_app.state.sessionmaker() as db:
                user = await db.scalar(select(User).where(User.username == "alice"))
                assert user is not None
                await settings_gate.acquire_send(db, AuthenticatedActor(user.id, user.token_version))
        finally:
            release_init.set()
            release_headers.set()
            release_body.set()
            await _cancel(pending)


@asynccontextmanager
async def _disconnecting_server(received, release):
    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method = lines[0].split(" ")[0]
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            size = int(next((v.strip() for k, v in headers.items() if k.lower() == "content-length"), "0"))
            data = await reader.readexactly(size) if size else b""
            message = json.loads(data) if data else {}
            rpc_method = message.get("method")
            if rpc_method == "tools/call":
                received.set()
                await release.wait()
                return  # 真实 TCP 在响应头之前断开。
            if rpc_method == "initialize":
                content = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test", "version": "1"},
                }}).encode()
                status = b"200 OK"
            else:
                content = b""
                status = b"405 Method Not Allowed" if method == "GET" else b"202 Accepted"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                         + b"Mcp-Session-Id: test-session\r\nConnection: close\r\nContent-Length: "
                         + str(len(content)).encode() + b"\r\n\r\n" + content)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        release.set()
        server.close()
        await server.wait_closed()


async def test_tcp_disconnect_before_headers_releases_pg_settings_gate(postgres_mcp_apps, fake_llm, monkeypatch):
    owner_app, admin_app = postgres_mcp_apps
    received, release = asyncio.Event(), asyncio.Event()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://testserver") as admin,
        _disconnecting_server(received, release) as remote_url,
    ):
        await _admin(admin_app, admin)
        base, _, _, change = await _proposal(owner_app, owner, fake_llm, remote_url)
        holder = {}
        original = settings_gate.acquire_send

        async def observed(db, actor):
            await original(db, actor)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))

        monkeypatch.setattr(settings_gate, "acquire_send", observed)
        pending = asyncio.create_task(owner.post(
            f"{base}/changes/{change['id']}/confirm", json=change_action(change),
        ))
        writer = None
        try:
            await asyncio.wait_for(received.wait(), 10)
            writer = asyncio.create_task(admin.patch(URL, json={"revision": 0, "changes": {"mcp_enabled": False}}))
            await asyncio.wait_for(_wait_for_blocker(admin_app.state.sessionmaker, holder["pid"]), 10)
            release.set()
            result = await asyncio.wait_for(writer, 15)
            assert result.status_code == 200, result.text
            result = await asyncio.wait_for(pending, 15)
            assert result.status_code == 409 and result.json()["code"] == "external_outcome_unknown", result.text
        finally:
            release.set()
            await _cancel(pending, writer)


async def test_repeatable_read_cannot_decide_from_prelock_snapshot(postgres_mcp_apps):
    owner_app, _ = postgres_mcp_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner:
        user = await register(owner)
    async with owner_app.state.sessionmaker() as db:
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        row = await db.get(User, user["id"])
        with pytest.raises(VisibilityPolicyError):
            await settings_gate.acquire_send(db, AuthenticatedActor(row.id, row.token_version))


async def test_first_missing_row_uses_immutable_false_deployment_default(
    postgres_mcp_apps, tmp_path, fake_llm,
):
    owner_app, _ = postgres_mcp_apps
    settings = make_settings(tmp_path, database_url=owner_app.state.settings.database_url, mcp_enabled=False)
    disabled_app = main.create_app(settings, provider_factory=fake_llm.factory, start_task_engine=False)
    try:
        await disabled_app.state.system_settings.load()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
        ) as owner:
            user = await register(owner)
        disabled_app.state.settings.mcp_enabled = True  # 不得将可变运行设置当作部署事实。
        async with disabled_app.state.sessionmaker() as db:
            assert await db.scalar(select(SystemSettings.id)) is None
            row = await db.get(User, user["id"])
            with pytest.raises(VisibilityPolicyError):
                await settings_gate.acquire_send(db, AuthenticatedActor(row.id, row.token_version))
    finally:
        await disabled_app.state.system_settings.close()


async def test_concurrent_first_setting_writes_do_not_replace_one_another(postgres_mcp_apps):
    first_app, second_app = postgres_mcp_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=first_app), base_url="http://testserver") as first,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver") as second,
    ):
        await _admin(first_app, first)
        second.cookies.update(first.cookies)
        results = await asyncio.gather(
            first.patch(URL, json={"revision": 0, "changes": {"mcp_enabled": False}}),
            second.patch(URL, json={"revision": 0, "changes": {"mcp_enabled": True}}),
        )
        assert sorted(item.status_code for item in results) == [200, 409]
        winner = next(item.json() for item in results if item.status_code == 200)
        values = {item["key"]: item["value"] for item in winner["items"]}
        async with first_app.state.sessionmaker() as db:
            stored = await db.get(SystemSettings, 1)
            assert stored.revision == 1
            assert stored.overrides["mcp_enabled"] is values["mcp_enabled"]
