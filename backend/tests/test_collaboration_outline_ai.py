"""大纲 AI 任务作者路径的模型/检索边界与协作者关闭保护。"""

import asyncio
import json
from contextlib import suppress

import pytest
from sqlalchemy import update

from app.ai.providers import OpenAICompatProvider, create_provider
from app.models import Project
from app.services import outline_ai_service, retrieval_service
from app.services.ai_dispatch import VisibilityPolicyError
from tests.helpers import create_ai_config, create_project, register, wait_task, wait_until
from tests.test_agent_providers import openai_chunk
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_story_access import _invite


async def test_outline_stream_coalesces_fast_fragments_without_losing_text():
    class Provider:
        async def generate_stream(self, _messages, _model, **_kwargs):
            for i in range(100):
                yield str(i % 10)

    job = outline_ai_service.OutlineJob(kind="outline.continue", project_id=1, provider=Provider(), model="model")
    pieces = []
    events = [event async for event in outline_ai_service._stream(job, [], pieces)]
    expected = "".join(str(i % 10) for i in range(100))
    assert "".join(pieces) == "".join(data["text"] for _, data in events) == expected
    assert len(events) <= 4


async def test_outline_stream_flushes_slow_fragments_and_cancels_provider():
    blocked, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        async def generate_stream(self, _messages, _model, **_kwargs):
            try:
                yield "甲"
                yield "乙"
                await blocked.wait()
            finally:
                closed.set()

    job = outline_ai_service.OutlineJob(kind="outline.continue", project_id=1, provider=Provider(), model="model")
    events = outline_ai_service._stream(job, [], [])
    first = await asyncio.wait_for(anext(events), 1)
    second = await asyncio.wait_for(anext(events), 1)
    assert first == ("delta", {"text": "甲"}) and second == ("delta", {"text": "乙"})
    await events.aclose()
    assert closed.is_set()


@pytest.mark.parametrize("kind", ["outline.continue", "outline.expand"])
async def test_outline_retrieval_stale_visibility_never_sends_embedding(
    app, client, fake_llm, monkeypatch, kind,
):
    user = await register(client, "outline-retrieval-owner")
    project = await create_project(client)
    await create_ai_config(client)
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post(f"{base}/outlines", json={"title": "原章纲", "summary": "原梗概"})
    assert created.status_code == 201, created.text
    params = {"count": 2, "direction": "续写"} if kind == "outline.continue" else {
        "count": 2, "outline_id": created.json()["id"], "instruction": "展开",
    }
    payload = json.dumps({"object": "list", "model": "embedding-model",
                          "data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    async with _server(payload=payload, mime="application/json") as (url, _, headers, body, requests):
        headers.set()
        body.set()
        provider = OpenAICompatProvider("local-key", f"{url}/v1/", 3)

        async def embedding_after_visibility_change(*_args, **_kwargs):
            async with app.state.sessionmaker() as writer:
                await writer.execute(update(Project).where(Project.id == project["id"]).values(
                    ai_visibility_revision=Project.ai_visibility_revision + 1,
                ))
                await writer.commit()
            return retrieval_service.Embedder(provider, "openai_compat", "embedding-model")

        async def query_knowledge(_db, _project, query, *, embedder, purpose):
            await embedder.embed([query])
            return ""

        monkeypatch.setattr(retrieval_service, "get_embedder", embedding_after_visibility_change)
        monkeypatch.setattr(outline_ai_service.kb_service, "knowledge_text", query_knowledge)
        async with app.state.sessionmaker() as db:
            with pytest.raises(VisibilityPolicyError):
                await outline_ai_service.build_outline_job(
                    db, user_id=user["id"], kind=kind, project_id=project["id"], params=params,
                    cipher=app.state.cipher, factory=fake_llm.factory, ai_timeout=3, allow_private=True,
                )
        assert requests == []
        assert fake_llm.calls == []


async def test_editor_outline_ai_admission_boundaries_never_touch_owner_vectors(
    app, client, make_client, fake_llm,
):
    """E4-g3a 起大纲 AI 对 accepted editor 开放：无本人模型且未获作者授权 400、viewer 403、外书章纲 404，
    均零任务、零模型/向量调用，作者旧向量索引不被改写（成功路径见 test_collaboration_outline_entry_e4g3）。"""
    import numpy as np
    from sqlalchemy import func, select

    from app.models import RetrievalChunk, StoryMemory, Task

    await register(client, "outline-owner")
    editor, viewer = make_client(), make_client()
    await register(editor, "outline-editor")
    await register(viewer, "outline-viewer")
    project = await create_project(client)
    foreign = await create_project(client, title="外部作品")
    await create_ai_config(client)
    await create_ai_config(client, model_type="embedding", model="legacy-model")
    base, foreign_base = (f"/api/v1/projects/{row['id']}" for row in (project, foreign))
    own = await client.post(f"{base}/outlines", json={"title": "本书章纲"})
    other = await client.post(f"{foreign_base}/outlines", json={"title": "外书秘密"})
    assert own.status_code == other.status_code == 201
    async with app.state.sessionmaker() as db:
        memory = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", content="作者旧向量私有记忆")
        db.add(memory)
        await db.flush()
        await retrieval_service.upsert_many(db, project["id"], "memory", [
            retrieval_service.ChunkInput(ref_id=memory.id, text=memory.content, chapter_no=1),
        ])
        await db.execute(update(RetrievalChunk).where(
            RetrievalChunk.project_id == project["id"], RetrievalChunk.source == "memory",
            RetrievalChunk.ref_id == memory.id,
        ).values(embedding=retrieval_service.encode_vector(np.array([1.0, 0.0], dtype=np.float32)),
                 embedding_key="openai_compat:legacy-model:2"))
        await db.commit()
    await _invite(client, editor, project["id"], "outline-editor", "editor")
    await _invite(client, viewer, project["id"], "outline-viewer", "viewer")
    for actor, expected in ((editor, 400), (viewer, 403)):
        assert (await actor.post(f"{base}/outlines/continue", json={"count": 1})).status_code == expected
        assert (await actor.post(f"{base}/outlines/{own.json()['id']}/expand",
                                 json={"count": 2})).status_code == expected
    for actor, expected in ((client, 404), (editor, 400)):
        result = await actor.post(f"{base}/outlines/{other.json()['id']}/expand", json={"count": 2})
        assert result.status_code == expected and "外书秘密" not in result.text
    assert fake_llm.calls == [] and fake_llm.embed_calls == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        key = await db.scalar(select(RetrievalChunk.embedding_key).where(
            RetrievalChunk.project_id == project["id"], RetrievalChunk.source == "memory",
        ))
        assert key == "openai_compat:legacy-model:2"


@pytest.mark.parametrize("kind", ["continue", "expand"])
async def test_outline_acceptance_freezes_the_authors_model_binding(app, client, kind):
    from app.models import Task
    from app.services import project_ai_config

    user = await register(client, "outline-binding-owner")
    project = await create_project(client)
    config = await create_ai_config(client, model="original-model")
    base = f"/api/v1/projects/{project['id']}"
    outline = await client.post(f"{base}/outlines", json={"title": "章纲"})
    assert outline.status_code == 201, outline.text
    await app.state.task_engine.stop()
    if kind == "continue":
        path, body = f"{base}/outlines/continue", {"count": 1}
    else:
        path, body = f"{base}/outlines/{outline.json()['id']}/expand", {"count": 2}
    response = await client.post(path, json=body)
    assert response.status_code == 202, response.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, response.json()["id"])
        binding = project_ai_config.deserialize_project_model(
            task.input.get("_project_model"), actor_id=user["id"], project_id=project["id"],
            project_owner_id=user["id"],
        )
        assert binding is not None and binding.config_id == config["id"]
        assert binding.actor_id == user["id"] and binding.grant_identity is None
        assert "original-model" not in repr(binding)


@pytest.mark.parametrize("embedding", [False, True])
async def test_outline_acceptance_freezes_shared_retrieval_choice(app, client, embedding):
    from app.models import Task
    from app.services import project_ai_config

    user = await register(client, "outline-retrieval-binding-owner")
    project = await create_project(client)
    await create_ai_config(client)
    embedding_config = (
        await create_ai_config(client, model_type="embedding", model="embedding-original") if embedding else None
    )
    await app.state.task_engine.stop()
    created = await client.post(f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1})
    assert created.status_code == 202, created.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, created.json()["id"])
        selected = project_ai_config.deserialize_shared_embedding(
            task.input.get("_shared_embedding"), actor_id=user["id"], project_id=project["id"],
            project_owner_id=user["id"], task_grant=project_ai_config.TASK_GRANT_ABSENT,
        )
        assert selected is not None and selected.mode == ("vector" if embedding else "keyword")
        if embedding:
            assert selected.model is not None and selected.model.config_id == embedding_config["id"]
        else:
            assert selected.reason == "embedding_config_missing" and selected.model is None


@pytest.mark.parametrize("kind", ["continue", "expand"])
@pytest.mark.parametrize("change", ["default", "original_model"])
async def test_outline_retry_preserves_the_accepted_model(app, client, fake_llm, kind, change):
    from app.models import Task

    await register(client, "outline-retry-owner")
    project = await create_project(client)
    original = await create_ai_config(client, model="original-model")
    replacement = await create_ai_config(client, model="replacement-model")
    base = f"/api/v1/projects/{project['id']}"
    outline = await client.post(f"{base}/outlines", json={"title": "章纲"})
    assert outline.status_code == 201, outline.text
    await app.state.task_engine.stop()
    if kind == "continue":
        path, body = f"{base}/outlines/continue", {"count": 1}
    else:
        path, body = f"{base}/outlines/{outline.json()['id']}/expand", {"count": 2}
    created = await client.post(path, json=body)
    assert created.status_code == 202, created.text
    initial = created.json()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, initial["id"])
        binding = task.input["_project_model"]
        task.status = "failed"
        await db.commit()

    if change == "default":
        changed = await client.post(f"/api/v1/ai-configs/{replacement['id']}/default")
    else:
        changed = await client.patch(f"/api/v1/ai-configs/{original['id']}", json={"model": "mutated-model"})
    assert changed.status_code == 200, changed.text
    retried = await client.post(
        f"/api/v1/tasks/{initial['id']}/retry", params={"task_key": initial["task_key"]},
    )
    if change == "original_model":
        assert retried.status_code in (400, 409), retried.text
        assert "mutated-model" not in retried.text
    else:
        assert retried.status_code == 202, retried.text
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, retried.json()["id"])
            assert task.input["_project_model"] == binding
            assert task.input["ai_config_id"] == original["id"]
    assert fake_llm.calls == [] and fake_llm.embed_calls == []


@pytest.mark.parametrize("tampering", ["missing", "wrong_project", "request_config"])
async def test_outline_retry_rejects_lost_or_inconsistent_binding(app, client, fake_llm, tampering):
    from app.models import Task

    await register(client, "outline-retry-invalid-owner")
    project = await create_project(client)
    await create_ai_config(client)
    await app.state.task_engine.stop()
    created = await client.post(f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1})
    assert created.status_code == 202, created.text
    initial = created.json()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, initial["id"])
        data = dict(task.input)
        if tampering == "missing":
            data.pop("_project_model")
        elif tampering == "wrong_project":
            data["_project_model"] = {**data["_project_model"], "project_id": project["id"] + 1}
        else:
            data["request"] = {**data["request"], "ai_config_id": data["ai_config_id"] + 1}
        task.input = data
        task.status = "failed"
        await db.commit()
    response = await client.post(
        f"/api/v1/tasks/{initial['id']}/retry", params={"task_key": initial["task_key"]},
    )
    assert response.status_code in (400, 409), response.text
    assert fake_llm.calls == [] and fake_llm.embed_calls == []


@pytest.mark.parametrize("change", ["keyword_then_vector", "vector_changed", "vector_replaced"])
async def test_outline_retry_keeps_the_original_shared_retrieval_choice(app, client, fake_llm, change):
    from app.models import Task

    await register(client, "outline-retry-retrieval-owner")
    project = await create_project(client)
    await create_ai_config(client)
    original = None if change == "keyword_then_vector" else await create_ai_config(
        client, model_type="embedding", model="embedding-original",
    )
    await app.state.task_engine.stop()
    created = await client.post(f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1})
    assert created.status_code == 202, created.text
    initial = created.json()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, initial["id"])
        selection = task.input["_shared_embedding"]
        task.status = "failed"
        await db.commit()
    if change == "vector_changed":
        changed = await client.patch(f"/api/v1/ai-configs/{original['id']}", json={"model": "embedding-mutated"})
        assert changed.status_code == 200, changed.text
    else:
        new = await create_ai_config(client, model_type="embedding", model="embedding-new")
        if change == "vector_replaced":
            changed = await client.post(f"/api/v1/ai-configs/{new['id']}/default")
            assert changed.status_code == 200, changed.text
    response = await client.post(
        f"/api/v1/tasks/{initial['id']}/retry", params={"task_key": initial["task_key"]},
    )
    if change == "keyword_then_vector":
        assert response.status_code == 202, response.text
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, response.json()["id"])
            assert task.input["_shared_embedding"] == selection
    else:
        assert response.status_code in (400, 409), response.text
    assert fake_llm.calls == [] and fake_llm.embed_calls == []


@pytest.mark.parametrize("kind", ["continue", "expand"])
@pytest.mark.parametrize("change", ["default", "original_model"])
async def test_outline_execution_uses_accepted_model_over_real_http(app, client, monkeypatch, kind, change):
    await register(client, "outline-real-http-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    outline = await client.post(f"{base}/outlines", json={"title": "章纲"})
    assert outline.status_code == 201, outline.text
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    content = json.dumps({"outlines": [{"title": "续章", "summary": "梗概"}],
                          "chapters": [{"title": "细纲", "summary": "梗概"}]}, ensure_ascii=False)
    payload = (f"data: {json.dumps(openai_chunk({'content': content}, 'stop'))}\n\n"
               "data: [DONE]\n\n").encode()
    await app.state.task_engine.stop()
    async with _server(payload=payload) as (original_url, _, original_headers, original_body, original_requests):
        async with _server(payload=payload) as (replacement_url, _, replacement_headers, replacement_body, other):
            original_headers.set()
            original_body.set()
            replacement_headers.set()
            replacement_body.set()
            original = await create_ai_config(client, base_url=f"{original_url}/v1/", model="original-model")
            replacement = await create_ai_config(client, base_url=f"{replacement_url}/v1/", model="replacement-model")
            if kind == "continue":
                path, body = f"{base}/outlines/continue", {"count": 1}
            else:
                path, body = f"{base}/outlines/{outline.json()['id']}/expand", {"count": 2}
            created = await client.post(path, json=body)
            assert created.status_code == 202, created.text
            if change == "default":
                changed = await client.post(f"/api/v1/ai-configs/{replacement['id']}/default")
            else:
                changed = await client.patch(f"/api/v1/ai-configs/{original['id']}", json={"model": "mutated-model"})
            assert changed.status_code == 200, changed.text
            await app.state.task_engine.start()
            result = await wait_task(client, created.json(), within=10)
            if change == "default":
                assert result["status"] == "succeeded", result
                assert len(original_requests) == 1 and json.loads(original_requests[0])["model"] == "original-model"
            else:
                assert result["status"] == "failed", result
                assert original_requests == []
            assert other == []


async def test_outline_expand_rechecks_the_model_between_real_http_batches(app, client, monkeypatch):
    await register(client, "outline-http-batches-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    outline = await client.post(f"{base}/outlines", json={"title": "原章纲"})
    assert outline.status_code == 201, outline.text
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    content = json.dumps({"chapters": [{"title": f"细纲{i}", "summary": "梗概"} for i in range(5)]},
                         ensure_ascii=False)
    payload = (f"data: {json.dumps(openai_chunk({'content': content}, 'stop'))}\n\n"
               "data: [DONE]\n\n").encode()
    async with _server(payload=payload) as (url, received, headers, body, requests):
        config = await create_ai_config(client, base_url=f"{url}/v1/", model="original-model")
        created = await client.post(f"{base}/outlines/{outline.json()['id']}/expand", json={"count": 6})
        assert created.status_code == 202, created.text
        await asyncio.wait_for(received.wait(), 5)
        headers.set()
        changed = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "mutated-model"})
        assert changed.status_code == 200, changed.text
        body.set()
        task = await wait_task(client, created.json(), within=10)
        assert task["status"] == "failed", task
        assert len(requests) == 1
        assert json.loads(requests[0])["model"] == "original-model"


async def test_outline_discards_real_http_output_after_original_actor_revocation(app, client, monkeypatch):
    from app.models import Task, User

    user = await register(client, "outline-http-revoked-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    content = json.dumps({"outlines": [{"title": "撤权后秘密", "summary": "不可交付"}]}, ensure_ascii=False)
    payload = (f"data: {json.dumps(openai_chunk({'content': content}, 'stop'))}\n\n"
               "data: [DONE]\n\n").encode()
    async with _server(payload=payload) as (url, received, headers, body, requests):
        await create_ai_config(client, base_url=f"{url}/v1/", model="original-model")
        created = await client.post(f"{base}/outlines/continue", json={"count": 1})
        assert created.status_code == 202, created.text
        identity = created.json()
        task_id = identity["id"]
        await asyncio.wait_for(received.wait(), 5)
        stream = asyncio.create_task(client.get(
            f"/api/v1/tasks/{task_id}/stream", params={"task_key": identity["task_key"]},
        ))
        try:
            await wait_until(lambda: app.state.task_engine.hubs[task_id].subscriber_count > 0, within=5)
            headers.set()
            # 等待认证行锁释放；模拟令牌在收到响应头后失效、正文尚未读取。
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
                await db.commit()
            body.set()
            await wait_until(lambda: task_id not in app.state.task_engine._running, within=10)
            response = await asyncio.wait_for(stream, 10)
            assert response.status_code == 200 and "撤权后秘密" not in response.text
            assert len(requests) == 1
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, task_id)
                assert task.result is None and "撤权后秘密" not in task.output_text
            hub = app.state.task_engine.hubs.get(task_id)
            assert hub is None or "撤权后秘密" not in hub.text
        finally:
            if not stream.done():
                stream.cancel()
                with suppress(asyncio.CancelledError):
                    await stream


async def test_outline_stops_real_http_stream_when_actor_is_revoked_between_frames(app, client, monkeypatch):
    from app.models import Task, User

    user = await register(client, "outline-http-stream-owner")
    project = await create_project(client)
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    release, first_written = asyncio.Event(), asyncio.Event()
    requests = []
    first = f"data: {json.dumps(openai_chunk({'content': '{\"outlines\":[{\"title\":\"获准\",'}, None))}\n\n".encode()
    last = (f"data: {json.dumps(openai_chunk({'content': '\"summary\":\"撤权后秘密\"}]}'}, 'stop'))}\n\n"
            "data: [DONE]\n\n").encode()
    server_tasks = set()

    async def serve(reader, writer):
        server_tasks.add(asyncio.current_task())
        try:
            raw = (await reader.readuntil(b"\r\n\r\n")).decode()
            request_headers = dict(line.lower().split(": ", 1) for line in raw.split("\r\n")[1:] if line)
            requests.append(await reader.readexactly(int(request_headers.get("content-length", 0))))
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Length: {len(first) + len(last)}\r\n"
                          "Content-Type: text/event-stream\r\nConnection: close\r\n\r\n").encode() + first)
            await writer.drain()
            first_written.set()
            await release.wait()
            writer.write(last)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            server_tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        await create_ai_config(client, base_url=f"http://127.0.0.1:{port}/v1/", model="text-original")
        created = await client.post(f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1})
        assert created.status_code == 202, created.text
        task_id = created.json()["id"]
        await asyncio.wait_for(first_written.wait(), 5)
        await wait_until(lambda: "获准" in app.state.task_engine.hubs[task_id].text, within=5)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
            await db.commit()
        release.set()
        await wait_until(lambda: task_id not in app.state.task_engine._running, within=10)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, task_id)
            assert task.result is None and "撤权后秘密" not in task.output_text
        hub = app.state.task_engine.hubs.get(task_id)
        assert hub is None or "撤权后秘密" not in hub.text
        assert len(requests) == 1
    finally:
        release.set()
        server.close()
        if server_tasks:
            await asyncio.gather(*server_tasks, return_exceptions=True)
        await server.wait_closed()


async def test_outline_vector_request_rechecks_actor_before_text_send(app, client, monkeypatch):
    from app.models import User

    user = await register(client, "outline-http-vector-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def force_vector_query(_db, _project, query, *, embedder, purpose):
        assert embedder is not None
        await embedder.embed([query])
        return "受保护资料"

    monkeypatch.setattr(outline_ai_service.kb_service, "knowledge_text", force_vector_query)
    embedding_payload = json.dumps({"object": "list", "model": "embedding-original",
                                    "data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
    async with _server(payload=embedding_payload, mime="application/json") as (
        embed_url, received, headers, body, seen,
    ):
        async with _server() as (text_url, _, text_headers, text_body, text_requests):
            text_headers.set()
            text_body.set()
            await create_ai_config(client, base_url=f"{text_url}/v1/", model="text-original")
            await create_ai_config(
                client, base_url=f"{embed_url}/v1/", model_type="embedding", model="embedding-original",
            )
            created = await client.post(f"{base}/outlines/continue", json={"count": 1, "direction": "需要资料"})
            assert created.status_code == 202, created.text
            task_id = created.json()["id"]
            await asyncio.wait_for(received.wait(), 8)
            assert len(seen) == 1 and json.loads(seen[0])["model"] == "embedding-original"
            headers.set()
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
                await db.commit()
            body.set()
            await wait_until(lambda: task_id not in app.state.task_engine._running, within=10)
            assert len(seen) == 1 and text_requests == []


@pytest.mark.parametrize("change", ["keyword_then_vector", "vector_changed"])
async def test_outline_execution_respects_original_shared_retrieval_choice(app, client, monkeypatch, change):
    await register(client, "outline-http-retrieval-owner")
    project = await create_project(client)
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    observed = []

    async def capture_embedder(_db, _project, _query, *, embedder, purpose):
        observed.append(embedder)
        return ""

    monkeypatch.setattr(outline_ai_service.kb_service, "knowledge_text", capture_embedder)
    content = json.dumps({"outlines": [{"title": "续章", "summary": "梗概"}]}, ensure_ascii=False)
    payload = (f"data: {json.dumps(openai_chunk({'content': content}, 'stop'))}\n\n"
               "data: [DONE]\n\n").encode()
    await app.state.task_engine.stop()
    async with _server(payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        await create_ai_config(client, base_url=f"{url}/v1/", model="text-original")
        original = None if change == "keyword_then_vector" else await create_ai_config(
            client, base_url=f"{url}/v1/", model_type="embedding", model="embedding-original",
        )
        created = await client.post(
            f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1, "direction": "检索"},
        )
        assert created.status_code == 202, created.text
        if change == "keyword_then_vector":
            await create_ai_config(client, base_url=f"{url}/v1/", model_type="embedding", model="embedding-new")
        else:
            changed = await client.patch(f"/api/v1/ai-configs/{original['id']}", json={"model": "embedding-mutated"})
            assert changed.status_code == 200, changed.text
        await app.state.task_engine.start()
        task = await wait_task(client, created.json(), within=10)
        if change == "keyword_then_vector":
            assert task["status"] == "succeeded", task
            assert observed == [None]
            assert len(requests) == 1 and json.loads(requests[0])["model"] == "text-original"
        else:
            assert task["status"] == "failed", task
            assert observed == [] and requests == []


async def test_outline_task_input_change_after_claim_never_reaches_real_http(app, client, monkeypatch):
    from app.models import Task
    from app.services.task_engine.handlers.outline import OutlineHandler

    await register(client, "outline-http-input-owner")
    project = await create_project(client)
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    started = asyncio.Event()
    original = OutlineHandler.build_job

    async def replace_input(self, ctx, task, *, execution):
        changed = {**task.input, "request": {**task.input["request"], "direction": "篡改后的输入"}}
        async with ctx.sessionmaker() as db:
            await db.execute(update(Task).where(Task.id == task.id).values(input=changed))
            await db.commit()
        started.set()
        return await original(self, ctx, task, execution=execution)

    monkeypatch.setattr(OutlineHandler, "build_job", replace_input)
    async with _server() as (url, _, headers, body, requests):
        headers.set()
        body.set()
        await create_ai_config(client, base_url=f"{url}/v1/", model="text-original")
        created = await client.post(
            f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1, "direction": "原始输入"},
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["id"]
        await asyncio.wait_for(started.wait(), 8)
        await wait_until(lambda: task_id not in app.state.task_engine._running, within=10)
        assert requests == []
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, task_id)
            assert task.result is None and "篡改后的输入" not in task.output_text


@pytest.mark.parametrize("space", ["legacy", "shared", "mixed", "mixed_source"])
async def test_owner_outline_recalls_only_author_legacy_vectors_with_bound_model(app, client, monkeypatch, space):
    """E6-d（裁决 1）调整：作品向量空间统一为作者旧键 provider:model:dim，大纲检索只读该空间。
    原「旧键或 shared:v2 均可召回」改为：shared:v2 键（无生产写入方）不属作者现用空间，按来源退回关键词、
    不发送查询向量；同一来源混用两种键同样退回关键词。"""
    import numpy as np

    from app.models import KbChunk, KbDocument, RetrievalChunk, StoryMemory
    await register(client, "outline-owner-vector-search")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    outline = await client.post(f"{base}/outlines", json={"title": "起点"})
    assert outline.status_code == 201, outline.text
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    content = json.dumps({"outlines": [{"title": "续章", "summary": "梗概"}]}, ensure_ascii=False)
    text_payload = (f"data: {json.dumps(openai_chunk({'content': content}, 'stop'))}\n\n"
                    "data: [DONE]\n\n").encode()
    embedding_payload = json.dumps({"object": "list", "model": "embedding-original",
                                    "data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
    async with (
        _server(payload=text_payload) as (text_url, _, text_headers, text_body, text_requests),
        _server(payload=embedding_payload, mime="application/json") as (
            embed_url, _, embed_headers, embed_body, embed_requests,
        ),
    ):
        text_headers.set()
        text_body.set()
        embed_headers.set()
        embed_body.set()
        await create_ai_config(client, base_url=f"{text_url}/v1/", model="text-original")
        await create_ai_config(client, base_url=f"{embed_url}/v1/", model_type="embedding", model="embedding-original")
        async with app.state.sessionmaker() as db:
            memory = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", content="旧情节秘密蓝鸟")
            document = KbDocument(project_id=project["id"], filename="设定.txt", sha256="a" * 64,
                                  status="ready", text="神秘遗迹留声机")
            db.add_all([memory, document])
            await db.flush()
            chunk = KbChunk(document_id=document.id, seq=1, content="神秘遗迹留声机")
            db.add(chunk)
            await db.flush()
            await retrieval_service.upsert_many(db, project["id"], "memory", [
                retrieval_service.ChunkInput(ref_id=memory.id, text=memory.content, chapter_no=1),
            ])
            await retrieval_service.upsert_many(db, project["id"], "kb", [
                retrieval_service.ChunkInput(ref_id=chunk.id, text=chunk.content),
            ])
            if space == "mixed_source":
                other = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", content="另一个旧空间回忆")
                db.add(other)
                await db.flush()
                await retrieval_service.upsert_many(db, project["id"], "memory", [
                    retrieval_service.ChunkInput(ref_id=other.id, text=other.content, chapter_no=1),
                ])
            if space == "legacy":
                key = "openai_compat:embedding-original"
            else:
                # 历史 shared:v2 空间键字面形状（生成原语已于 E6-f 删除，生产无写入方）。
                key = "shared:v2:" + "0" * 64
            kb_key = "openai_compat:embedding-original" if space == "mixed" else key
            encoded = retrieval_service.encode_vector(np.array([1.0, 0.0], dtype=np.float32))
            await db.execute(update(RetrievalChunk).where(
                RetrievalChunk.project_id == project["id"], RetrievalChunk.ref_id == memory.id,
                RetrievalChunk.source == "memory",
            ).values(embedding=encoded, embedding_key=retrieval_service.make_embedding_key(key, 2)))
            await db.execute(update(RetrievalChunk).where(
                RetrievalChunk.project_id == project["id"], RetrievalChunk.ref_id == chunk.id,
                RetrievalChunk.source == "kb",
            ).values(embedding=encoded, embedding_key=retrieval_service.make_embedding_key(kb_key, 2)))
            if space == "mixed_source":
                await db.execute(update(RetrievalChunk).where(
                    RetrievalChunk.project_id == project["id"], RetrievalChunk.ref_id == other.id,
                    RetrievalChunk.source == "memory",
                ).values(embedding=encoded, embedding_key="openai_compat:embedding-original:2"))
            await db.commit()
        created = await client.post(f"{base}/outlines/continue", json={"count": 1, "direction": "陌生查询词"})
        assert created.status_code == 202, created.text
        task = await wait_task(client, created.json(), within=10)
        assert task["status"] == "succeeded", task
        # 查询文本对 kb 与记忆相同，同一 embedder 缓存命中：至多一次查询向量请求。
        vector_kb = space in ("legacy", "mixed")
        assert len(embed_requests) == (1 if vector_kb else 0) and len(text_requests) == 1
        request_text = json.dumps(json.loads(text_requests[0]), ensure_ascii=False)
        # 查询词与资料无关键词重叠：只有作者旧键空间的向量命中才会注入。
        assert ("神秘遗迹留声机" in request_text) is vector_kb
        assert ("旧情节秘密蓝鸟" in request_text) is (space == "legacy")
        assert "另一个旧空间回忆" not in request_text


@pytest.mark.parametrize("case", ["text_recreated", "embedding_recreated", "old_text", "old_embedding"])
async def test_outline_rejects_recreated_or_old_model_identity_before_http(app, client, monkeypatch, case):
    from app.models import AIConfig, Task

    await register(client, "outline-model-identity-owner")
    project = await create_project(client)
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    await app.state.task_engine.stop()
    async with _server() as (url, _, headers, body, requests):
        headers.set()
        body.set()
        text_model = await create_ai_config(client, base_url=f"{url}/v1/", model="text-original")
        embedding = await create_ai_config(
            client, base_url=f"{url}/v1/", model_type="embedding", model="embedding-original",
        )
        created = await client.post(f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1})
        assert created.status_code == 202, created.text
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, created.json()["id"])
            if case.endswith("recreated"):
                config_id = text_model["id"] if case == "text_recreated" else embedding["id"]
                original = await db.get(AIConfig, config_id)
                previous_key = original.instance_key
                fields = ("id", "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
                          "is_default", "thinking_level", "context_window_k", "max_output_k")
                same_fields = {field: getattr(original, field) for field in fields}
                await db.delete(original)
                await db.flush()
                replacement = AIConfig(**same_fields)
                db.add(replacement)
                await db.flush()
                assert replacement.id == config_id and replacement.instance_key != previous_key
            else:
                data = dict(task.input)
                key = "_project_model" if case == "old_text" else "_shared_embedding"
                binding = dict(data[key])
                binding["version"] = 1
                if case == "old_text":
                    binding.pop("config_instance_key")
                else:
                    nested = dict(binding["model"])
                    nested["version"] = 1
                    nested.pop("config_instance_key")
                    binding["model"] = nested
                data[key] = binding
                task.input = data
            await db.commit()
        await app.state.task_engine.start()
        task_result = await wait_task(client, created.json(), within=10)
        assert task_result["status"] == "failed", task_result
        assert requests == []


async def test_outline_admission_does_not_hold_write_locks_during_dns(app, client, monkeypatch):
    from app.ai import endpoint_guard

    await register(client, "outline-preflight-owner")
    project = await create_project(client)
    await create_ai_config(client, base_url="https://gated.example.test/v1/")
    await app.state.task_engine.stop()
    reached, release, writer_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_dns(host, port):
        assert host == "gated.example.test"
        reached.set()
        await release.wait()
        return ["93.184.216.34"]

    async def write_project():
        async with app.state.sessionmaker() as db:
            writer_started.set()
            await db.execute(update(Project).where(Project.id == project["id"]).values(title="并发改名"))
            await db.commit()

    monkeypatch.setattr(endpoint_guard, "_resolve", blocked_dns)
    request = asyncio.create_task(client.post(
        f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1},
    ))
    writer = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        writer = asyncio.create_task(write_project())
        await asyncio.wait_for(writer_started.wait(), 5)
        try:
            await asyncio.wait_for(asyncio.shield(writer), 1)
            completed_while_dns_blocked = True
        except TimeoutError:
            completed_while_dns_blocked = False
    finally:
        release.set()
        if writer is not None:
            await asyncio.wait_for(writer, 8)
        result = await asyncio.wait_for(request, 8)
    assert completed_while_dns_blocked, "DNS 预检期间不应持有写屏障"
    assert result.status_code == 202, result.text


@pytest.mark.parametrize("change_kind", ["default", "actor"])
async def test_outline_admission_rejects_identity_changed_during_dns(app, client, monkeypatch, change_kind):
    from sqlalchemy import func, select

    from app.ai import endpoint_guard
    from app.models import Task, User

    user = await register(client, "outline-preflight-change-owner")
    project = await create_project(client)
    await create_ai_config(client, base_url="https://gated.example.test/v1/", model="original-model")
    replacement = await create_ai_config(client, model="replacement-model")
    await app.state.task_engine.stop()
    reached, release = asyncio.Event(), asyncio.Event()

    async def blocked_dns(host, port):
        assert host == "gated.example.test"
        reached.set()
        await release.wait()
        return ["93.184.216.34"]

    async def revoke_actor():
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
            await db.commit()

    monkeypatch.setattr(endpoint_guard, "_resolve", blocked_dns)
    request = asyncio.create_task(client.post(
        f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1},
    ))
    change = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        change = asyncio.create_task(
            client.post(f"/api/v1/ai-configs/{replacement['id']}/default")
            if change_kind == "default" else revoke_actor()
        )
        try:
            changed = await asyncio.wait_for(asyncio.shield(change), 1)
            changed_during_dns = True
        except TimeoutError:
            changed_during_dns = False
    finally:
        release.set()
        if change is not None:
            changed = await asyncio.wait_for(change, 8)
        result = await asyncio.wait_for(request, 8)
    assert changed_during_dns
    if change_kind == "default":
        assert changed.status_code == 200
    assert result.status_code in (401, 409), result.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == 0
