import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app.models.inspiration_generation import InspirationGenerationClaim
from app.services import inspiration_service, kb_service
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register, wait_task
from tests.schema_helpers import isolated_worker_database

API = "/api/v1/inspiration/sessions"
TITLE_OPTIONS = ["星海余烬", "城市的心跳", "末日前夜", "回声少女"]
THEME_OPTIONS = ["一座城的记忆", "与过去和解", "觉醒的代价", "学会相互托付"]


@pytest.fixture(params=("sqlite", "postgresql"))
async def settings_overrides(request, monkeypatch):
    if request.param == "sqlite":
        yield {}
    else:
        async with isolated_worker_database(monkeypatch, required=True) as overrides:
            yield overrides


async def _create_session(client):
    await register(client)
    await create_ai_config(client)
    result = await client.post(API, json={"idea": "在末日前夜找回失落的城市"})
    assert result.status_code == 201, result.text
    return result.json()["id"]


def _peer_client(app, cookies):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", cookies=cookies,
    )


async def test_inspiration_disconnect_before_first_frame_releases_claim(app, client, second_app, fake_llm):
    session_id = await _create_session(client)
    path = f"{API}/{session_id}/steps/title/generate"
    headers_sent = False

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        nonlocal headers_sent
        assert message["type"] == "http.response.start" and message["status"] == 200
        headers_sent = True
        raise asyncio.CancelledError

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "scheme": "http", "method": "POST", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"cookie", "; ".join(f"{key}={value}" for key, value in client.cookies.items()).encode()),
        ],
        "client": ("127.0.0.1", 43210), "server": ("testserver", 80),
    }
    with pytest.raises(asyncio.CancelledError):
        await app(scope, receive, send)
    assert headers_sent
    assert fake_llm.calls == []

    fake_llm.push(json.dumps({"options": TITLE_OPTIONS}))
    async with _peer_client(second_app, client.cookies) as peer:
        result = await peer.post(path, json={})
    assert result.status_code == 200, result.text
    assert parse_sse(result.text)[-1][1]["round"]["options"] == TITLE_OPTIONS
    assert len(fake_llm.calls) == 1


async def test_inspiration_concurrent_notes_keep_both_changes(client, second_app, monkeypatch):
    session_id = await _create_session(client)
    original = inspiration_service.get_owned_session
    both_loaded = asyncio.Event()
    loaded_sessions = set()

    async def get_owned_session(db, user_id, candidate_id):
        result = await original(db, user_id, candidate_id)
        if candidate_id == session_id and db not in loaded_sessions and len(loaded_sessions) < 2:
            loaded_sessions.add(db)
            if len(loaded_sessions) == 2:
                both_loaded.set()
            await asyncio.wait_for(both_loaded.wait(), timeout=5)
        return result

    monkeypatch.setattr(inspiration_service, "get_owned_session", get_owned_session)
    async with _peer_client(second_app, client.cookies) as peer:
        first, second = await asyncio.gather(
            client.post(f"{API}/{session_id}/notes", json={"text": "隐藏的地图"}),
            peer.post(f"{API}/{session_id}/notes", json={"text": "钟楼的线索"}),
        )
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    current = (await client.get(f"{API}/{session_id}")).json()
    assert sorted(current["state"]["notes"]) == sorted(["隐藏的地图", "钟楼的线索"])


async def test_inspiration_peers_only_generate_one_call_per_step(app, client, second_app, fake_llm):
    session_id = await _create_session(client)
    path = f"{API}/{session_id}"
    title_gate, theme_gate = Gate(), Gate()
    fake_llm.push([json.dumps({"options": TITLE_OPTIONS}), title_gate])
    fake_llm.push([json.dumps({"options": THEME_OPTIONS}), theme_gate])
    async with _peer_client(second_app, client.cookies) as peer, asyncio.TaskGroup() as tasks:
        title = tasks.create_task(client.post(f"{path}/steps/title/generate", json={}))
        try:
            await title_gate.wait_reached()
            async with second_app.state.sessionmaker() as db:
                claim = await db.get(InspirationGenerationClaim, (session_id, "title"))
                assert claim.worker_key == app.state.task_engine.request_worker.worker_key
            duplicate = await peer.post(f"{path}/steps/title/generate", json={})
            assert duplicate.status_code == 409
            assert duplicate.json() == {"detail": "该步骤正在生成选项，请稍候再试"}
            assert len(fake_llm.calls) == 1
            theme = tasks.create_task(peer.post(f"{path}/steps/theme/generate", json={}))
            await theme_gate.wait_reached()
            note = await peer.post(f"{path}/notes", json={"text": "钟楼的线索"})
            assert note.status_code == 200
        finally:
            title_gate.release()
            theme_gate.release()
    assert title.result().status_code == theme.result().status_code == 200
    assert parse_sse(title.result().text)[-1][0] == parse_sse(theme.result().text)[-1][0] == "done"
    state = (await client.get(path)).json()["state"]
    assert state["notes"] == ["钟楼的线索"]
    assert state["steps"]["title"]["rounds"][0]["options"] == TITLE_OPTIONS
    assert state["steps"]["theme"]["rounds"][0]["options"] == THEME_OPTIONS
    assert len(fake_llm.calls) == 2


async def test_inspiration_new_owner_replaces_fenced_claim_and_old_cleanup_cannot_delete_it(
    app, client, second_app, fake_llm,
):
    session_id = await _create_session(client)
    path = f"{API}/{session_id}/steps/title/generate"
    old_gate, new_gate = Gate(), Gate()
    fake_llm.push(['{"options":', old_gate, '["旧一","旧二","旧三","旧四"]}'])
    fake_llm.push([json.dumps({"options": TITLE_OPTIONS}), new_gate])
    async with _peer_client(second_app, client.cookies) as peer, asyncio.TaskGroup() as tasks:
        old = tasks.create_task(client.post(path, json={}))
        try:
            await old_gate.wait_reached()
            # 模拟租约已确认终止，旧协程此时尚未从外部调用恢复。
            await app.state.task_engine.request_worker.lease.stop()
            new = tasks.create_task(peer.post(path, json={}))
            await new_gate.wait_reached()
            old_gate.release()
            old_response = await old
            old_events = parse_sse(old_response.text)
            assert old_events[-1][0] == "error"
            assert "旧一" not in old_response.text
            async with second_app.state.sessionmaker() as db:
                claim = await db.get(InspirationGenerationClaim, (session_id, "title"))
                assert claim.worker_key == second_app.state.task_engine.request_worker.worker_key
        finally:
            old_gate.release()
            new_gate.release()
    assert parse_sse(new.result().text)[-1][1]["round"]["options"] == TITLE_OPTIONS
    session = (await client.get(f"{API}/{session_id}")).json()
    assert [rnd["options"] for rnd in session["state"]["steps"]["title"]["rounds"]] == [TITLE_OPTIONS]


async def test_inspiration_lease_expiry_cancels_silent_model_without_waiting_for_next_delta(app, client, fake_llm):
    session_id = await _create_session(client)
    path = f"{API}/{session_id}/steps/title/generate"
    gate = Gate()
    fake_llm.push(['{"options":', gate, '["旧一","旧二","旧三","旧四"]}'])
    async with asyncio.TaskGroup() as tasks:
        generation = tasks.create_task(client.post(path, json={}))
        try:
            await gate.wait_reached()
            app.state.task_engine.request_worker._expire()
            async with asyncio.timeout(2):
                result = await asyncio.shield(generation)
            assert not gate.released.is_set()
        finally:
            gate.release()
    assert parse_sse(result.text)[-1] == ("error", {"detail": "灵感生成状态已变化，请重试"})
    assert fake_llm.aborted == 1
    session = (await client.get(f"{API}/{session_id}")).json()
    assert session["state"]["steps"]["title"]["rounds"] == []


@pytest.mark.parametrize("lose_lease", [False, True], ids=["healthy", "lease-lost"])
async def test_inspiration_claim_precedes_vector_call_and_closes_read_transaction(
    app, client, second_app, fake_llm, monkeypatch, lose_lease,
):
    await register(client)
    await create_ai_config(client)
    await create_ai_config(client, model_type="embedding", model="kb-vector", name="参考资料向量")
    project = await create_project(client, premise="钟楼和隐藏的地图")
    uploaded = await client.post(
        f"/api/v1/projects/{project['id']}/kb/documents",
        files={"file": ("map.md", "# 钟楼\n\n钟楼中有一张隐藏的地图。".encode())},
    )
    assert uploaded.status_code == 202, uploaded.text
    indexed = await wait_task(client, uploaded.json()["task"], within=10)
    assert indexed["status"] == "succeeded", indexed
    created = await client.post(f"{API}/from-project/{project['id']}")
    assert created.status_code == 200
    path = f"{API}/{created.json()['id']}/steps/title/generate"
    original = kb_service.knowledge_for_project
    read_closed = False

    async def observe(db, *args, query_embed_scope, **kwargs):
        @asynccontextmanager
        async def check():
            nonlocal read_closed
            assert db.in_transaction()
            async with query_embed_scope():
                assert not db.in_transaction()
                read_closed = True
                yield

        return await original(db, *args, query_embed_scope=check, **kwargs)

    monkeypatch.setattr(kb_service, "knowledge_for_project", observe)
    vector_gate = Gate()
    fake_llm.embed_after_gates = [vector_gate]
    fake_llm.push(json.dumps({"options": TITLE_OPTIONS}))
    before = len(fake_llm.embed_calls)
    async with _peer_client(second_app, client.cookies) as peer, asyncio.TaskGroup() as tasks:
        generation = tasks.create_task(client.post(path, json={}))
        try:
            await vector_gate.wait_reached()
            assert read_closed
            duplicate = await peer.post(path, json={})
            assert duplicate.status_code == 409
            assert len(fake_llm.embed_calls) == before + 1
            assert fake_llm.calls == []
            if lose_lease:
                app.state.task_engine.request_worker._expire()
                async with asyncio.timeout(2):
                    result = await asyncio.shield(generation)
                assert result.status_code == 409
                assert result.json()["detail"] == "灵感生成状态已变化，请重试"
                assert not vector_gate.released.is_set()
        finally:
            vector_gate.release()
    if not lose_lease:
        assert parse_sse(generation.result().text)[-1][0] == "done"
    else:
        assert fake_llm.calls == []


async def test_inspiration_worker_releases_claim_after_database_recovers(
    app, client, second_app, fake_llm, monkeypatch,
):
    session_id = await _create_session(client)
    path = f"{API}/{session_id}/steps/title/generate"
    worker = app.state.task_engine.request_worker
    worker.engine.heartbeat_interval = 0.05
    blocked_worker = worker.worker_key
    db_available = asyncio.Event()
    original = inspiration_service._delete_claim

    async def flaky_delete(sessionmaker, key):
        if key.worker_key == blocked_worker and not db_available.is_set():
            raise OSError("临时连接故障")
        await original(sessionmaker, key)

    monkeypatch.setattr(inspiration_service, "_delete_claim", flaky_delete)
    fake_llm.push(json.dumps({"options": TITLE_OPTIONS}))
    first = await client.post(path, json={})
    assert parse_sse(first.text)[-1][0] == "done"
    async with _peer_client(second_app, client.cookies) as peer:
        duplicate = await peer.post(path, json={})
        assert duplicate.status_code == 409
        db_available.set()
        async with asyncio.timeout(10):
            while True:
                async with second_app.state.sessionmaker() as db:
                    claim = await db.get(InspirationGenerationClaim, (session_id, "title"))
                if claim is None:
                    break
                await asyncio.sleep(0.05)
        assert worker.worker_key == blocked_worker
        fake_llm.push(json.dumps({"options": TITLE_OPTIONS}))
        second = await peer.post(path, json={})
        assert parse_sse(second.text)[-1][0] == "done"
    session = (await client.get(f"{API}/{session_id}")).json()
    assert len(session["state"]["steps"]["title"]["rounds"]) == 2
