"""两个应用在同一数据库上按真实写入顺序受理和排空任务。"""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.ai.providers import AIProviderError
from app.models import AgentMessage, AIConfig, AnalysisIndexReceipt, Chapter, ChapterAnalysis, Task
from app.services import ai_config_service, analysis_service, authorization_service, task_service
from app.services.book_import import reverse
from tests.conftest import _new_client
from tests.helpers import (
    Gate,
    analysis_json,
    create_ai_config,
    create_project,
    parse_sse,
    register,
    stream_task,
    streamed_text,
    task_params,
    wait_task,
    wait_until,
)
from tests.schema_helpers import isolated_worker_database
from tests.test_agent_runtime import call, send_task, setup_agent
from tests.test_book_import import DIGESTS, SYNTHESIZE
from tests.test_book_import import _setup as prepare_book_import
from tests.test_book_import import _start as start_book_import
from tests.test_system_settings import save_settings
from tests.test_task_retry_admission_api import _failed_outline

_CLOSED = {"detail": "系统正在维护，暂不受理新任务，请稍后再试", "code": "task_admission_closed"}


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def _chapters(client, *contents):
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapters = []
    for index, content in enumerate(contents, 1):
        response = await client.post(f"{base}/chapters", json={"title": f"章节 {index}", "content": content})
        assert response.status_code == 201, response.text
        chapters.append(response.json())
    return base, chapters


async def _story(client, *contents):
    await register(client)
    await create_ai_config(client)
    return await _chapters(client, *contents)


def _assert_closed(response):
    assert response.status_code == 503, response.text
    assert response.json() == _CLOSED


async def test_drain_refuses_normal_task_entries_across_apps_before_reload(
    app, second_app, client, system_admin, fake_llm,
):
    await app.state.task_engine.stop()
    await second_app.state.system_settings.close()
    base, (chapter,) = await _story(client, "")
    created = await client.post(f"{base}/agent/conversations", json={})
    assert created.status_code == 201, created.text
    agent_url = f"{base}/agent/conversations/{created.json()['id']}/messages"
    generate_url = f"/api/v1/chapters/{chapter['id']}/generate"
    key = await client.post("/api/v1/api-keys", json={"name": "测试排空", "all_projects": True})
    assert key.status_code == 201, key.text
    providers_before = len(fake_llm._providers)
    async with _new_client(second_app) as remote:
        remote.cookies.update(client.cookies)
        await save_settings(system_admin, task_admission_closed=True)
        assert second_app.state.settings.task_admission_closed is False
        for connection in (client, remote):
            _assert_closed(await connection.post(generate_url, json={}))
            _assert_closed(await connection.post(f"{base}/batch-generate", json={
                "start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"],
            }))
            _assert_closed(await connection.post(agent_url, json={"content": "维护期间发送"}))
            _assert_closed(await connection.post(f"{base}/kb/rebuild"))
        preview = await remote.post(f"{base}/batch-generate/preview", json={
            "start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"],
        })
        assert preview.status_code == 200, preview.text
        assert len(fake_llm._providers) == providers_before
        assert fake_llm.calls == [] and fake_llm.embed_calls == []
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count(Task.id))) == 0
            assert await db.scalar(select(func.count(AgentMessage.id))) == 0
            current = await db.get(Chapter, chapter["id"])
            assert current.status == chapter["status"] and current.content == ""
            assert current.generation_claim_task_key is None
        public = await remote.get(
            f"/api/open/v1/novels/{chapter['project_id']}", headers={"X-API-Key": key.json()["key"]},
        )
        assert public.status_code == 200, public.text
        await save_settings(system_admin, task_admission_closed=False)
        accepted = await remote.post(generate_url, json={})
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "queued"


@pytest.mark.parametrize("model_state", ("missing", "damaged"))
async def test_drain_checked_after_access_and_before_model_configuration(
    app, client, make_client, system_admin, fake_llm, model_state,
):
    await register(client)
    _, (chapter,) = await _chapters(client, "")
    outsider = make_client()
    await register(outsider, "drain-outsider")
    if model_state == "damaged":
        config = await create_ai_config(client)
        async with app.state.sessionmaker() as db:
            row = await db.get(AIConfig, config["id"])
            row.api_key_enc = "invalid-encrypted-key"
            await db.commit()
    generate_url = f"/api/v1/chapters/{chapter['id']}/generate"
    baseline = await client.post(generate_url, json={})
    assert baseline.status_code == 400, baseline.text
    await save_settings(system_admin, task_admission_closed=True)
    _assert_closed(await client.post(generate_url, json={}))
    hidden = await outsider.post(generate_url, json={})
    assert hidden.status_code == 404 and hidden.json().get("code") is None, hidden.text
    assert fake_llm.calls == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0


async def test_drain_retry_checks_access_and_source_before_revalidating_original_model(
    app, client, system_admin, monkeypatch,
):
    task, original, _, _ = await _failed_outline(app, client, monkeypatch)
    async with app.state.sessionmaker() as db:
        row = await db.get(AIConfig, original["id"])
        row.api_key_enc = "invalid-encrypted-key"
        await db.commit()
    retry_url = f"/api/v1/tasks/{task['id']}/retry"
    baseline = await client.post(retry_url, params=task_params(task))
    assert baseline.status_code == 400, baseline.text
    await save_settings(system_admin, task_admission_closed=True)
    wrong_task_key = await client.post(retry_url, params={"task_key": str(uuid4())})
    assert wrong_task_key.status_code == 404, wrong_task_key.text
    _assert_closed(await client.post(retry_url, params=task_params(task)))
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 1


async def test_drain_preserves_running_agent_preview_and_synchronous_rewrite(
    app, client, system_admin, fake_llm,
):
    project, _, conversation_url = await setup_agent(client)
    created = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"content": "临时正文"})
    assert created.status_code == 201, created.text
    chapter_id = created.json()["id"]
    model = Gate()
    fake_llm.push_tools([model, call("start_task", {
        "kind": "chapter.generate", "params": {"chapter_id": chapter_id},
    })])
    turn = await send_task(client, conversation_url)
    try:
        await model.wait_reached(within=10)
        await save_settings(system_admin, task_admission_closed=True)
    finally:
        model.release()
    completed = await wait_task(client, turn, statuses=("paused", "failed", "succeeded"), within=10)
    assert completed["status"] == "paused", completed
    details = await client.get(conversation_url)
    assert details.status_code == 200, details.text
    change, = details.json()["changes"]
    assert (change["tool_name"], change["status"]) == ("start_task", "proposed")

    fake_llm.push("同步改写结果")
    rewritten = await client.post(f"/api/v1/chapters/{chapter_id}/rewrite", json={
        "op": "rewrite", "start": 0, "end": 4,
    })
    assert rewritten.status_code == 200 and parse_sse(rewritten.text)[-1][0] == "done", rewritten.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 1


async def test_drain_saved_after_preflight_still_refuses_the_locked_write(
    app, second_app, client, system_admin, monkeypatch, fake_llm,
):
    await app.state.task_engine.stop()
    _, (chapter,) = await _story(client, "")
    preflight = Gate()
    original_build = ai_config_service.build_provider

    async def observe_build(*args, **kwargs):
        provider = await original_build(*args, **kwargs)
        preflight.reached.set()
        await preflight.released.wait()
        return provider

    monkeypatch.setattr(ai_config_service, "build_provider", observe_build)
    async with _new_client(second_app) as remote:
        remote.cookies.update(client.cookies)
        request = asyncio.create_task(remote.post(f"/api/v1/chapters/{chapter['id']}/generate", json={}))
        try:
            await preflight.wait_reached()
            await save_settings(system_admin, task_admission_closed=True)
        finally:
            preflight.release()
            result = await asyncio.wait_for(request, 10)
        _assert_closed(result)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        current = await db.get(Chapter, chapter["id"])
        assert current.status == chapter["status"] and current.generation_claim_task_key is None
    assert fake_llm.calls == []


async def test_drain_cannot_split_a_pending_analysis_submission(
    app, second_app, client, system_admin, monkeypatch,
):
    await app.state.task_engine.stop()
    base, chapters = await _story(client, "第一章正文", "第二章正文")
    first_row = Gate()
    switch_waiting = asyncio.Event()
    original_create = task_service.create_task
    original_gate = authorization_service.gate

    async def observe_first_row(*args, **kwargs):
        result = await original_create(*args, **kwargs)
        if not first_row.reached.is_set():
            first_row.reached.set()
            await first_row.released.wait()
        return result

    async def observe_gate(db):
        if asyncio.current_task().get_name() == "system-settings-write":
            switch_waiting.set()
        await original_gate(db)

    monkeypatch.setattr(task_service, "create_task", observe_first_row)
    monkeypatch.setattr(authorization_service, "gate", observe_gate)
    async with _new_client(second_app) as remote:
        remote.cookies.update(client.cookies)
        request = asyncio.create_task(remote.post(f"{base}/analysis/analyze-pending"))
        switch = None
        try:
            await first_row.wait_reached()
            async with app.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(Task.id))) == 0
            switch = asyncio.create_task(save_settings(system_admin, task_admission_closed=True))
            await asyncio.wait_for(switch_waiting.wait(), 10)
        finally:
            first_row.release()
            result = await asyncio.wait_for(request, 10)
            if switch is not None:
                await asyncio.wait_for(switch, 10)

        assert result.status_code == 200 and result.json()["queued"] == 2, result.text
        _assert_closed(await remote.post(f"{base}/analysis/analyze-pending"))
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(Task).order_by(Task.id)))
        assert [(row.chapter_id, row.kind, row.status) for row in rows] == [
            (chapter["id"], "chapter.analyze", "queued") for chapter in chapters
        ]
        assert [row.id for row in rows] == result.json()["task_ids"]


async def _paused_batch(client, fake_llm, content):
    base, (first, last) = await _chapters(client, "", "")
    fake_llm.push(content)
    response = await client.post(f"{base}/batch-generate", json={
        "start_chapter_id": first["id"], "end_chapter_id": last["id"], "review_every": 1,
    })
    assert response.status_code == 202, response.text
    return await wait_task(client, response.json(), statuses=("paused",), within=10)


async def test_drain_denies_retry_and_continue_but_allows_finish_and_cancel(
    app, second_app, client, system_admin, fake_llm,
):
    _, (chapter,) = await _story(client, "")
    fake_llm.push(AIProviderError("生成失败"))
    original = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
    assert original.status_code == 202, original.text
    failed = await wait_task(client, original.json(), statuses=("failed",), within=10)
    finishable = await _paused_batch(client, fake_llm, "待提前结束的正文")
    cancellable = await _paused_batch(client, fake_llm, "待取消的正文")

    await save_settings(system_admin, task_admission_closed=True)
    providers_before = len(fake_llm._providers)
    async with _new_client(second_app) as remote:
        remote.cookies.update(client.cookies)
        for task, action in ((failed, "retry"), (finishable, "resume"), (cancellable, "resume")):
            _assert_closed(await remote.post(f"/api/v1/tasks/{task['id']}/{action}", params=task_params(task)))
        assert len(fake_llm._providers) == providers_before
        async with app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(Task).order_by(Task.id)))
            assert [row.status for row in rows] == ["failed", "paused", "paused"]
            assert all(row.retry_of_id is None for row in rows)

        finished = await remote.post(f"/api/v1/tasks/{finishable['id']}/finish", params=task_params(finishable))
        assert finished.status_code == 200 and finished.json()["status"] == "succeeded", finished.text
        cancelled = await remote.post(f"/api/v1/tasks/{cancellable['id']}/cancel", params=task_params(cancellable))
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text


async def test_drain_keeps_processing_admitted_tasks_and_their_verified_followup(
    app, second_app, client, system_admin, fake_llm,
):
    await save_settings(system_admin, task_max_concurrency=1, auto_analyze_enabled=True)
    _, (parent_chapter,) = await _story(client, "")
    await create_ai_config(client, name="剧情记忆向量", model_type="embedding", model="drain-embed")
    base, chapters = await _chapters(client, "", "", "")
    disabled = await client.patch(base, json={"auto_analyze": False})
    assert disabled.status_code == 200 and disabled.json()["auto_analyze"] is False, disabled.text
    model = Gate()
    fake_llm.push(["初见", model, "后续正文"])
    for content in ["第二本第一章", "第二本第二章", "第二本第三章"]:
        fake_llm.push(content)
    fake_llm.push(analysis_json(summary="排空期间写入的摘要"))
    started = await client.post(f"/api/v1/chapters/{parent_chapter['id']}/generate", json={})
    assert started.status_code == 202, started.text
    first = started.json()
    async with _new_client(second_app) as remote:
        remote.cookies.update(client.cookies)
        queue = []
        stream = None
        try:
            await model.wait_reached(within=10)
            stream = asyncio.create_task(stream_task(client, first, within=20))
            for chapter in chapters:
                response = await remote.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
                assert response.status_code == 202 and response.json()["status"] == "queued", response.text
                queue.append(response.json())
            await wait_until(lambda: app.state.task_engine.hubs[first["id"]].subscriber_count > 0)
            await save_settings(system_admin, task_admission_closed=True)
        finally:
            model.release()
            if stream is not None:
                frames = await stream
        assert streamed_text(frames) == "初见后续正文"
        assert frames[-1][0] == "done" and frames[-1][1]["task"]["status"] == "succeeded"
        for item in queue:
            assert (await wait_task(remote, item, within=10))["status"] == "succeeded"
        followup, = (await remote.get("/api/v1/tasks", params={
            "chapter_id": parent_chapter["id"], "kind": "chapter.analyze",
        })).json()
        assert (await wait_task(remote, followup, within=10))["status"] == "succeeded"
        async with asyncio.timeout(10):
            while True:
                response = await remote.get("/api/v1/tasks", params={
                    "project_id": parent_chapter["project_id"], "kind": "kb.index",
                })
                assert response.status_code == 200, response.text
                if response.json():
                    index, = response.json()
                    break
                await asyncio.sleep(0.03)
        assert (await wait_task(remote, index, within=10))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(Task).where(Task.id == followup["id"]))
        assert row.input["auto"] is True
        assert await db.scalar(select(ChapterAnalysis.summary)) == "排空期间写入的摘要"
        receipt = await db.scalar(select(AnalysisIndexReceipt))
        assert receipt.source_task_key == row.statistics_key and receipt.child_task_key == index["task_key"]
    assert any(call["model"] == "drain-embed" for call in fake_llm.embed_calls)
    assert len(fake_llm.calls) == 5


async def test_running_book_import_can_admit_its_bound_deep_analysis(
    app, client, system_admin, fake_llm, monkeypatch,
):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    record = await prepare_book_import(client)
    enqueue = analysis_service.enqueue_pending
    rejected_stale_execution = asyncio.Event()

    async def check_source(db, project, *, execution, **kwargs):
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await enqueue(db, project, execution=replace(execution, task_key=str(uuid4())), **kwargs)
        rejected_stale_execution.set()
        return await enqueue(db, project, execution=execution, **kwargs)

    monkeypatch.setattr(analysis_service, "enqueue_pending", check_source)
    digest = Gate()
    fake_llm.push([digest, DIGESTS[0]])
    for text in (*DIGESTS[1:], SYNTHESIZE):
        fake_llm.push(text)
    for ordinal in range(1, 4):
        fake_llm.push(analysis_json(summary=f"拆书第 {ordinal} 章摘要"))
    response = await start_book_import(client, record, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    try:
        await digest.wait_reached(within=10)
        await save_settings(system_admin, task_admission_closed=True)
    finally:
        digest.release()
    assert (await wait_task(client, parent, within=15))["status"] == "succeeded"
    assert rejected_stale_execution.is_set()
    listing = await client.get("/api/v1/tasks", params={
        "project_id": parent["project_id"], "kind": "chapter.analyze",
    })
    assert listing.status_code == 200, listing.text
    children = listing.json()
    assert len(children) == 3
    for child in children:
        assert (await wait_task(client, child, within=10))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, parent["id"])
        rows = list(await db.scalars(select(Task).where(Task.kind == "chapter.analyze")))
        assert all(row.user_id == original.user_id and row.project_id == original.project_id for row in rows)
        assert all(row.input["_task_actor_token_version"] == original.input["_task_actor_token_version"]
                   for row in rows)
