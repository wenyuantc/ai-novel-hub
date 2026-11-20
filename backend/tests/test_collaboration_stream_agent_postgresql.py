"""隔离 PostgreSQL 专项：协作者 agent.turn 受控 SSE 帧租约并发与撤权串行化。

覆盖：
1. PostgreSQL FOR SHARE 帧租约与撤权（FOR UPDATE）严格串行：帧发送中撤权阻塞至帧结束，下一帧立即被拒；
2. 慢模型 _send_lease 与 _task_frame_lease（含 AgentConversation / AgentChange FOR SHARE）并发无阻塞；
3. 跨实例无通知直接改库关闭 allow_owner_ai 后流立即断开。
"""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import Base, ProjectCollaborator, Task
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine import stream_evidence
from app.services.task_engine.stream_lease import _capture_stream_binding, task_sse_response
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register
from tests.schema_helpers import create_isolated_schema
from tests.stream_helpers import mount_stream_response
from tests.test_agent_entries_e4id import _invite, _new_conversation, _send
from tests.test_collaboration_stream_agent import DENIED, _ProdStream
from tests.test_collaboration_stream_running_e5c1 import _serve, _settled
from tests.test_consistency_stream_slow_model import slow_headers
from tests.test_task_sse_scope import _tcp_server


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
    schema = f"collab_agent_sse_{uuid4().hex}"
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


async def test_postgresql_agent_stream_frame_lease_serializes_with_revocation_and_runs_concurrent_with_send_lease(
    app, client, make_client, fake_llm, monkeypatch,
):
    await register(client, "pg-agsse-owner")
    await create_ai_config(client, api_key="sk-pg-agsse-owner-01")
    project = await create_project(client, title="PG智能体流作品")
    editor_client = make_client()
    editor = await register(editor_client, "pg-agsse-editor")
    member = await _invite(client, editor_client, project["id"], "pg-agsse-editor", allow_owner_ai=True)
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)

    # 1. 慢模型 _send_lease 与 _task_frame_lease 并发无阻塞
    header_entered = asyncio.Event()
    slow_headers(monkeypatch, 1.0, reached=header_entered)
    fake_llm.push_tools([("text", {"text": "PG慢模型完成"})])
    turn = await _send(editor_client, base, cid)
    await asyncio.wait_for(header_entered.wait(), 10)
    async with _serve(app) as tcp_base:
        stream = _ProdStream(tcp_base, editor_client, turn)
        try:
            await stream.wait_for(lambda evs: any(n == "snapshot" for n, _ in evs), within=5)
            events = await stream.finish(within=15)
        finally:
            await stream.aclose()
    assert events[-1][0] == "done" and "PG慢模型完成" in "".join(d["text"] for n, d in events if n == "delta")

    # 2. 跨实例无通知直接改库关闭 allow_owner_ai 立即断开
    monkeypatch.setattr(app.state.task_engine, "heartbeat_seconds", 0.1)
    gate = Gate()
    fake_llm.push_tools([("text", {"text": "PG第一段"}), gate, ("text", {"text": "PG不可泄露"})])
    turn2 = await _send(editor_client, base, cid)
    async with _serve(app) as tcp_base:
        stream2 = _ProdStream(tcp_base, editor_client, turn2)
        try:
            await asyncio.wait_for(gate.reached.wait(), 10)
            await stream2.wait_for(lambda evs: any("PG第一段" in d.get("text", "") for _, d in evs))
            async with app.state.sessionmaker() as db:
                await db.execute(update(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
                ).values(allow_owner_ai=False))
                await db.commit()
            events2 = await stream2.finish(within=10)
        finally:
            gate.release()
            await stream2.aclose()
    assert events2[-1] == ("error", DENIED)
    assert "PG不可泄露" not in stream2.text
    await _settled(app, turn2["id"])

    # 3. FOR SHARE 帧租约发送中阻塞撤权至帧结束，下一帧立即被拒
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
        ).values(allow_owner_ai=True))
        await db.commit()
        row = await db.get(Task, turn["id"])
        binding = await _capture_stream_binding(db, row, AuthenticatedActor(editor["id"], 0))

    entered, release, next_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fake_verify(db, _task, _project, _event, _data, evidence):
        return evidence == "ok"

    async def fake_events():
        yield "snapshot", {"text": "PG先发"}, "ok"
        await next_frame.wait()
        yield "delta", {"text": "PG撤权后"}, "ok"

    async def endpoint():
        return task_sse_response(fake_events(), sessionmaker=app.state.sessionmaker, binding=binding)

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", fake_verify)
    mount_stream_response(app, "/_test/task-frame", endpoint)
    async with _tcp_server(app, entered, release) as tcp:
        pending = asyncio.create_task(tcp.get("/_test/task-frame"))
        revoke = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            revoke = asyncio.create_task(client.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
                params={"expected_revision": member["revision"]},
            ))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(revoke), 0.15)
            release.set()
            assert (await asyncio.wait_for(revoke, 5)).status_code == 204
            next_frame.set()
            response = await asyncio.wait_for(pending, 5)
            parsed = parse_sse(response.text)
            assert any(d.get("text") == "PG先发" for _, d in parsed)
            assert "PG撤权后" not in response.text and parsed[-1] == ("error", DENIED)
        finally:
            release.set()
            next_frame.set()
            for item in (pending, revoke):
                if item is not None and not item.done():
                    item.cancel()
            await asyncio.gather(*(item for item in (pending, revoke) if item is not None), return_exceptions=True)
