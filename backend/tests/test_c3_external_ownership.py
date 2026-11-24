"""两个独立应用读取同一次真实工具调用；只有原 worker 或已确认停止后的恢复者能收尾。"""

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.core.errors import ConflictError
from app.models import AgentChange, McpPlugin, Project, Task, TaskWorkRecord
from app.services.agent import external_activity, service, transactions
from app.services.mcp import client as mcp_client
from tests.conftest import _new_client
from tests.helpers import wait_task
from tests.mcp_fakes import FakeMcpConnector, serve
from tests.schema_helpers import isolated_worker_database
from tests.test_agent_mcp_skills import call, change_action, send_task, setup


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield {**overrides, "allow_private_ai_endpoints": True}


async def _delete_while_call_is_active(peer, client, project_id, task_id, release):
    async with _new_client(peer) as http:
        http.cookies.update(client.cookies)
        deletion = asyncio.create_task(http.delete(f"/api/v1/projects/{project_id}"))
        try:
            async with asyncio.timeout(20):
                while not deletion.done():
                    async with peer.state.sessionmaker() as db:
                        project = await db.get(Project, project_id)
                        if project.deletion_token:
                            assert project.deleted_at is None and (await db.get(Task, task_id)).status == "paused"
                            break
                    await asyncio.sleep(0.01)
                assert not deletion.done(), "B 应等待 A 原调用收尾"
            release.set()
            response = await asyncio.wait_for(deletion, 20)
            assert response.status_code == 204, response.text
        finally:
            release.set()
            await asyncio.gather(deletion, return_exceptions=True)


@pytest.mark.parametrize("case", [
    "live", "peer_delete", "stopped_before_send", "stopped_after_send", "lost_owner", "swapped_owner",
])
async def test_peer_requires_original_external_worker_and_call(
    app, second_app, client, fake_llm, monkeypatch, case,
):
    paused, release = asyncio.Event(), asyncio.Event()
    calls = []
    remote = FastAPI()

    @remote.post("/mcp")
    async def rpc(request: Request):
        msg = await request.json()
        method = msg.get("method")
        if method == "initialize":
            if case == "stopped_before_send":
                paused.set()
                await release.wait()
            value = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                     "serverInfo": {"name": "test", "version": "1"}}
        elif method == "tools/list":
            value = {"tools": [{"name": "send", "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            calls.append(msg["params"])

            async def body():
                paused.set()
                await release.wait()
                yield json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "content": [{"type": "text", "text": "完成"}], "isError": False,
                }})
            return StreamingResponse(body(), media_type="application/json")
        else:
            return Response(status_code=202)
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": value})

    fake = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake
    project, base, url, plugin = await setup(client, fake)
    async with serve(remote) as remote_url:
        async with app.state.sessionmaker() as db:
            (await db.get(McpPlugin, plugin["id"])).url = f"{remote_url}/mcp"
            await db.commit()
        app.state.task_engine.ctx.mcp_connector = mcp_client.connect
        fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
        task = await send_task(client, url)
        assert (await wait_task(client, task, within=20, statuses=("paused", "failed")))["status"] == "paused"
        change = (await client.get(url)).json()["changes"][0]
        pending = asyncio.create_task(client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change)))
        try:
            await asyncio.wait_for(paused.wait(), 10)
            exit_failures = []
            if case == "lost_owner":
                def fail_first_exit(_conn, _cursor, statement, parameters, _context, _many):
                    if (not exit_failures and "UPDATE task_work_records SET state=" in statement
                            and "exited" in parameters):
                        exit_failures.append(True)
                        raise OperationalError(statement, parameters, OSError("退出提交前短暂断开"))

                event.listen(app.state.engine.sync_engine, "after_cursor_execute", fail_first_exit)
            async with second_app.state.sessionmaker() as peer:
                saved = await peer.get(AgentChange, change["id"])
                worker_key = saved.external_worker_key
                assert worker_key == app.state.task_engine.request_worker.worker_key
                assert worker_key != app.state.task_engine.lease.worker_key
                assert await external_activity.has_active_call(peer, project["id"])
                if case in ("lost_owner", "swapped_owner"):
                    peer_worker = second_app.state.task_engine.request_worker.worker_key
                    saved.external_worker_key = None if case == "lost_owner" else peer_worker
                    await peer.commit()
            stopped = case in ("stopped_before_send", "stopped_after_send")
            if stopped:
                # 原工作仍卡在网络边界，主库与侧库已实际确认旧登记停止；即使新代登记也不能补用。
                await app.state.task_engine.request_worker.lease.stop()
            else:
                monkeypatch.setattr(service, "EXTERNAL_STALE_AFTER", timedelta(0))
            async with second_app.state.sessionmaker() as peer:
                result = await service.recover_external(peer, engine=second_app.state.task_engine)
                await peer.commit()
                assert result == int(stopped)
                saved = await peer.get(AgentChange, change["id"])
                if stopped:
                    assert saved.status == "failed" and saved.result["_external_outcome_unknown"] is True
                    assert not await external_activity.has_active_call(peer, project["id"])
                else:
                    assert saved.status == "executing"
            if case == "peer_delete":
                await _delete_while_call_is_active(second_app, client, project["id"], task["id"], release)
            if case == "live":
                async with _new_client(second_app) as peer:
                    peer.cookies.update(client.cookies)
                    detail = await peer.get(url)
                    assert detail.status_code == 200, detail.text
                    viewed = detail.json()["changes"][0]
                    assert viewed["status"] == "executing" and viewed["result"] is None
                    # B 等待 A 的持久会话占用，释放后读取同一次已提交的执行结果。
                    duplicate = asyncio.create_task(peer.post(
                        f"{base}/changes/{change['id']}/confirm", json=change_action(viewed),
                    ))
                    try:
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(asyncio.shield(duplicate), 0.25)
                    finally:
                        release.set()
                        completed = await asyncio.wait_for(duplicate, 10)
                    assert completed.status_code == 200, completed.text
                    assert completed.json()["status"] == "executed"
                    assert completed.json()["result"] == {"text": "完成"}
        finally:
            release.set()
            response = await asyncio.wait_for(pending, 10)
        if case == "live":
            assert response.status_code == 200 and response.json()["status"] == "executed", response.text
            async with second_app.state.sessionmaker() as peer:
                remaining = await peer.scalar(select(TaskWorkRecord.work_key).where(TaskWorkRecord.kind == "external"))
                assert remaining is None
        elif case == "peer_delete":
            assert response.status_code == 409, response.text
            async with second_app.state.sessionmaker() as peer:
                saved = await peer.get(AgentChange, change["id"])
                assert saved.status == "failed" and saved.error == service.EXTERNAL_RETURNED_NOT_APPLIED
                assert (await peer.get(Project, project["id"])).deleted_at is not None
                assert (await peer.get(Task, task["id"])).status == "cancelled"
                assert not await external_activity.has_active_call(peer, project["id"])
        else:
            assert response.status_code == 409, response.text
            async with second_app.state.sessionmaker() as peer:
                saved = await peer.get(AgentChange, change["id"])
                if stopped:
                    assert saved.status == "failed" and saved.result["_external_outcome_unknown"] is True
                else:
                    assert saved.status == "executing"
                    if case == "lost_owner":
                        assert exit_failures == [True]
                    receipt = await peer.get(TaskWorkRecord, saved.result["_external_claim_id"])
                    assert receipt.state == "exited"
                    saved.external_worker_key = worker_key
                    await peer.commit()
                    # 恢复原归属后，已退出的工作到期可由 B 核对，不依赖 A 的内存。
                    assert await service.recover_external(peer, engine=second_app.state.task_engine) == 1
                    await peer.commit()
            if stopped:
                async with _new_client(second_app) as peer:
                    peer.cookies.update(client.cookies)
                    refused = await peer.delete(f"/api/v1/projects/{project['id']}")
                    assert refused.status_code == 409, refused.text
                async with second_app.state.sessionmaker() as peer:
                    assert (await peer.get(Project, project["id"])).deleted_at is None
                    assert (await peer.get(Task, task["id"])).status == "paused"
        assert len(calls) == (0 if case == "stopped_before_send" else 1)


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("stage", ["claim", "result"])
async def test_uncertain_commit_does_not_repeat_the_external_call(
    app, client, fake_llm, monkeypatch, stage, committed,
):
    fake = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake
    _, base, url, _ = await setup(client, fake)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, within=20, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(url)).json()["changes"][0]
    real_commit = transactions.AgentAuditCoordinator.commit
    commits = 0
    target = 1 if stage == "claim" else 2

    async def uncertain(self, db, factory):
        nonlocal commits
        if "external_call_in_progress" in db.info:
            commits += 1
            if commits == target:
                if committed:
                    await real_commit(self, db, factory)
                raise ConflictError("模拟数据库提交未获确认")
        return await real_commit(self, db, factory)

    monkeypatch.setattr(transactions.AgentAuditCoordinator, "commit", uncertain)
    response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert response.status_code == 409 and commits >= target, response.text
    monkeypatch.setattr(service, "EXTERNAL_STALE_AFTER", timedelta(0))
    async with app.state.sessionmaker() as db:
        saved = await db.get(AgentChange, change["id"])
        if stage == "result" and not committed:
            assert saved.status == "executing" and saved.external_worker_key is not None
            receipt = await db.get(TaskWorkRecord, saved.result["_external_claim_id"])
            assert receipt.state == "exited"
            assert await service.recover_external(db, engine=app.state.task_engine) == 1
            await db.commit()
            await db.refresh(saved)
            assert saved.status == "failed" and saved.result["_external_outcome_unknown"] is True
        elif stage == "result":
            assert saved.status == "executed"
        elif committed:
            assert saved.status == "failed"
            assert saved.error == service.EXTERNAL_NOT_SENT and saved.result == {"error": service.EXTERNAL_NOT_SENT}
        else:
            assert saved.status == "proposed"
        assert saved.external_worker_key is None
        assert await db.scalar(select(TaskWorkRecord.work_key).where(TaskWorkRecord.kind == "external")) is None
        assert await service.recover_external(db, engine=app.state.task_engine) == 0
    assert len(fake.calls) == int(stage == "result")
