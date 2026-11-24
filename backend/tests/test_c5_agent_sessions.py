import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import literal_column, select, update

from app.models import AgentChange, AgentConversation, AgentMessage
from app.models.agent_operation import AgentConversationOperation
from app.services import personal_projection, task_service
from app.services.agent import conversation_operations, service
from tests.helpers import Gate, wait_task
from tests.mcp_fakes import FakeMcpConnector
from tests.schema_helpers import isolated_worker_database
from tests.test_agent_mcp_skills import URL, call, change_action, send_task, setup
from tests.test_agent_runtime import setup_agent


@pytest.fixture(params=("sqlite", "postgresql"))
async def settings_overrides(monkeypatch, request):
    if request.param == "sqlite":
        yield {}
    else:
        async with isolated_worker_database(monkeypatch, required=True) as overrides:
            yield overrides


@pytest.fixture
async def peer_client(client, second_app):
    # 测试内完成注册后再同步原 Cookie，两个应用仍分别验证令牌。
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
    ) as peer:
        yield peer


async def test_peer_waits_through_external_confirmation_and_never_sends_twice(
    app, client, second_app, peer_client, fake_llm,
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def remote(_name, _arguments):
        entered.set()
        await release.wait()
        return "远端已执行"

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    second_app.state.task_engine.ctx.mcp_connector = connector
    _, base, url, _ = await setup(client, connector, handler=remote)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    fake_llm.push_tools([("text", {"text": "已发送。"})])
    task = await send_task(client, url)
    state = await wait_task(client, task, within=15, statuses=("paused", "failed"))
    assert state["status"] == "paused", state
    change = (await client.get(url)).json()["changes"][0]
    change_id = change["id"]
    confirm_url = f"{base}/changes/{change_id}/confirm"
    payload = change_action(change)

    peer_client.cookies.update(client.cookies)
    async with asyncio.TaskGroup() as jobs:
        first = jobs.create_task(client.post(confirm_url, json=payload))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            # 运行器恢复与重复确认都可能在 executing 的跨提交窗口发生。
            await second_app.state.task_engine._recover_external()
            async with second_app.state.sessionmaker() as db:
                status = await db.scalar(select(AgentChange.status).where(AgentChange.id == change_id))
            assert status == "executing"
            duplicate = jobs.create_task(peer_client.post(confirm_url, json=payload))
            rename = jobs.create_task(peer_client.patch(url, json={"title": "调用完成后重命名"}))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(rename), 0.25)
            assert not duplicate.done()
        finally:
            release.set()
    assert first.result().status_code == duplicate.result().status_code == 200
    assert first.result().json()["result"] == duplicate.result().json()["result"] == {"text": "远端已执行"}
    assert rename.result().status_code == 200, rename.result().text
    assert connector.calls == [(URL, "send", {"to": "编辑部"})]
    assert (await wait_task(client, task, within=15))["status"] == "succeeded"


async def test_peer_undo_waits_through_send_commit_and_rechecks_active_turn(
    client, peer_client, fake_llm, monkeypatch,
):
    project, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "林远"})])
    fake_llm.push_tools([("text", {"text": "已创建。"})])
    original = await send_task(client, url)
    assert (await wait_task(client, original, within=15, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(url)).json()["changes"][0]
    path = f"{base}/changes/{change['id']}"
    response = await client.post(path + "/confirm", json=change_action(change))
    assert response.status_code == 200, response.text
    completed = response.json()
    assert (await wait_task(client, original, within=15))["status"] == "succeeded"

    committed, release = asyncio.Event(), asyncio.Event()
    create_task = task_service.create_task

    async def hold_after_commit(*args, **kwargs):
        result = await create_task(*args, **kwargs)
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(task_service, "create_task", hold_after_commit)
    model_gate = Gate()
    fake_llm.push_tools([model_gate, ("text", {"text": "收到新一轮。"})])
    peer_client.cookies.update(client.cookies)
    try:
        async with asyncio.TaskGroup() as jobs:
            send = jobs.create_task(client.post(url + "/messages", json={"content": "请继续"}))
            try:
                await asyncio.wait_for(committed.wait(), 10)
                undo = jobs.create_task(peer_client.post(path + "/undo", json=change_action(completed)))
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(undo), 0.25)
            finally:
                release.set()
        assert send.result().status_code == 202, send.result().text
        assert undo.result().status_code == 409, undo.result().text
        assert undo.result().json()["detail"] == "请等待智能体暂停或结束后撤销"
        url_characters = f"/api/v1/projects/{project['id']}/characters"
        assert [row["name"] for row in (await client.get(url_characters)).json()] == ["林远"]
    finally:
        model_gate.release()
    assert (await wait_task(client, send.result().json()["task"], within=15))["status"] == "succeeded"


async def test_abandoned_conversation_operation_can_be_taken_after_worker_stops(app, client, second_app):
    _, _, url = await setup_agent(client)
    cid = int(url.rsplit("/", 1)[1])
    other = second_app.state.task_engine.request_worker
    async with second_app.state.sessionmaker.begin() as db:
        assert await db.get(AgentConversation, cid)
        db.add(AgentConversationOperation(conversation_id=cid, operation_key=str(uuid4()), worker_key=other.worker_key))
    await other.stop()
    saved = await client.patch(url, json={"title": "已接手"})
    assert saved.status_code == 200 and saved.json()["title"] == "已接手", saved.text
    async with app.state.sessionmaker() as db:
        assert await db.get(AgentConversationOperation, cid) is None


@pytest.mark.parametrize("settings_overrides", ["postgresql"], indirect=True)
async def test_lost_claim_does_not_commit_a_new_conversation_version(app, client, peer_client, monkeypatch):
    _, _, url = await setup_agent(client)
    cid = int(url.rsplit("/", 1)[1])
    peer_client.cookies.update(client.cookies)
    version = select(literal_column("xmin::text")).select_from(AgentConversation).where(AgentConversation.id == cid)
    async with app.state.sessionmaker() as db:
        original_version = await db.scalar(version)

    original_take = conversation_operations._take
    retrying, release = asyncio.Event(), asyncio.Event()
    inject_loss = True

    async def lose_after_row_update(db, claim, user_id, project_id):
        nonlocal inject_loss
        if inject_loss:
            inject_loss = False
            # 模拟两个领取者同时见到空槽，后到方改到会话行但 ON CONFLICT 拒绝了它。
            await db.execute(update(AgentConversation).where(AgentConversation.id == cid).values(
                updated_at=AgentConversation.updated_at,
            ))
            return False
        retrying.set()
        await release.wait()
        return await original_take(db, claim, user_id, project_id)

    monkeypatch.setattr(conversation_operations, "_take", lose_after_row_update)
    async with asyncio.TaskGroup() as jobs:
        request = jobs.create_task(peer_client.patch(url, json={"title": "只在实际领取后写入"}))
        try:
            await asyncio.wait_for(retrying.wait(), 10)
            async with app.state.sessionmaker() as db:
                assert await db.scalar(version) == original_version
        finally:
            release.set()
    assert request.result().status_code == 200, request.result().text


async def _replace_empty_conversation(peer, app, base, url):
    cid = int(url.rsplit("/", 1)[1])
    async with app.state.sessionmaker() as db:
        before = await db.get(AgentConversation, cid)
        created_at, updated_at, title = before.created_at, before.updated_at, before.title
    deleted = await peer.delete(url)
    assert deleted.status_code == 204, deleted.text
    replacement = await peer.post(f"{base}/conversations", json={"title": title})
    assert replacement.status_code == 201 and replacement.json()["id"] == cid, replacement.text
    # 即使两个实体的公开字段和秒级时间戳完全相同，也不能由旧请求认作同一个实体。
    async with app.state.sessionmaker.begin() as db:
        after = await db.get(AgentConversation, cid)
        after.created_at, after.updated_at = created_at, updated_at
    return cid


@pytest.mark.parametrize("settings_overrides", ["sqlite"], indirect=True)
@pytest.mark.parametrize("operation", ["patch", "delete", "send"])
async def test_reused_conversation_id_cannot_receive_late_mutation(
    app, client, peer_client, fake_llm, monkeypatch, operation,
):
    _, base, url = await setup_agent(client)
    peer_client.cookies.update(client.cookies)
    checked, release = asyncio.Event(), asyncio.Event()
    precheck = service._precheck_conversation

    async def pause_after_precheck(*args):
        result = await precheck(*args)
        if not checked.is_set():
            checked.set()
            await release.wait()
        return result

    monkeypatch.setattr(service, "_precheck_conversation", pause_after_precheck)
    if operation == "patch":
        request = client.patch(url, json={"title": "旧请求误改了新会话"})
    elif operation == "delete":
        request = client.delete(url)
    else:
        fake_llm.push_tools([("text", {"text": "旧请求误写了新会话"})])
        request = client.post(f"{url}/messages", json={"content": "只写原会话"})
    async with asyncio.TaskGroup() as jobs:
        late = jobs.create_task(request)
        try:
            await asyncio.wait_for(checked.wait(), 10)
            cid = await _replace_empty_conversation(peer_client, app, base, url)
        finally:
            release.set()

    assert late.result().status_code == 404, late.result().text
    async with app.state.sessionmaker() as db:
        replacement = await db.get(AgentConversation, cid)
        assert replacement is not None and replacement.title == ""
        assert await db.scalar(select(AgentMessage.id).where(AgentMessage.conversation_id == cid)) is None
        assert await db.get(AgentConversationOperation, cid) is None


@pytest.mark.parametrize("settings_overrides", ["sqlite"], indirect=True)
async def test_conversation_projection_cannot_substitute_identical_replacement(app, client, peer_client, monkeypatch):
    _, base, url = await setup_agent(client)
    peer_client.cookies.update(client.cookies)
    read, release = asyncio.Event(), asyncio.Event()
    project = personal_projection.project_agent_payload

    async def pause_before_projection(db, user_id, project_id, payload, proofs):
        if "conversation" in payload:
            read.set()
            await release.wait()
        return await project(db, user_id, project_id, payload, proofs)

    monkeypatch.setattr(personal_projection, "project_agent_payload", pause_before_projection)
    async with asyncio.TaskGroup() as jobs:
        late = jobs.create_task(client.get(url))
        try:
            await asyncio.wait_for(read.wait(), 10)
            await _replace_empty_conversation(peer_client, app, base, url)
        finally:
            release.set()
    assert late.result().status_code == 404, late.result().text
