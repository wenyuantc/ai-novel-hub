"""章级 AI 候选先持久暂停，作者确认前原章和结构绝不变化。"""

import asyncio

import pytest
from sqlalchemy import delete, select, update

from app.models import (
    Chapter,
    ChapterAnalysis,
    ChapterVersion,
    GenerationLog,
    SceneReviewAnalysisReceipt,
    SceneReviewPayloadChunk,
    SceneReviewProposal,
    Task,
    User,
)
from tests.helpers import Gate, analysis_json, create_ai_config, run_task, stream_task, wait_task
from tests.test_rich_document_api import KEY, add_section, save_payload
from tests.test_scenes import setup_scene


async def _pending(app, client, fake_llm):
    project, chapter, url, scene = await setup_scene(client, "原稿🙂甲乙")
    await create_ai_config(client)
    document_url = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await client.get(document_url)).json())
    add_section(draft, enabled=True)
    draft["document"]["sections"][0]["anchor"] = 2
    draft["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 1, "end": 4}]
    saved = await client.put(document_url, json=draft)
    assert saved.status_code == 200, saved.text
    source = saved.json()
    fake_llm.push("完全不同的候选稿")
    task, events = await run_task(client, await client.post(f"{url}/scenes/{scene['id']}/generate", json={
        "expected_content_revision": source["content_revision"],
        "expected_planning_revision": source["planning_revision"], "target_words": 300,
    }))
    assert task["status"] == "paused" and events[-1][0] == "paused", (task, events)
    result = events[-1][1]["result"]
    assert result["reason"] == "rich_document_review"
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(SceneReviewProposal).where(SceneReviewProposal.task_id == task["id"])))
        assert len(rows) == 1 and rows[0].status == "pending"
        assert rows[0].task_key == result["task_key"] and rows[0].run_key == result["run_key"]
    return project, chapter, url, scene, document_url, source, task, result


async def test_proposal_requires_confirmation_then_atomically_applies_once(app, client, fake_llm):
    _project, chapter, _url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    query = {"task_key": identity["task_key"], "run_key": identity["run_key"]}
    preview = await client.get(path, params=query)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["content_revision"] == source["content_revision"]
    assert data["scenes"][0]["unresolved_sections"] == [KEY] and data["scenes"][0]["format_loss"] is True
    assert (await client.post(f"/api/v1/tasks/{task['id']}/resume")).status_code == 409
    body = {**query, "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
            "confirm_format_loss": False}
    denied = await client.post(f"{path}/accept", json=body)
    assert denied.status_code in (409, 422) and (await client.get(document_url)).json() == source
    body["confirm_format_loss"] = True
    accepted = await client.post(f"{path}/accept", json=body)
    assert accepted.status_code == 200, accepted.text
    after = (await client.get(document_url)).json()
    assert after["document"]["content"] == "完全不同的候选稿"
    assert after["document"]["sections"][0]["anchor"] == 3
    assert after["document"]["sections"][0]["content"] == "作者🙂"
    repeat = await client.post(f"{path}/accept", json=body)
    assert repeat.status_code == 200 and repeat.json() == accepted.json()
    assert (await client.get(document_url)).json() == after and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task["id"])).status == "succeeded"
        assert len(list(await db.scalars(select(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter["id"], ChapterVersion.source == "generate",
        )))) == 1
        assert len(list(await db.scalars(select(GenerationLog).where(
            GenerationLog.chapter_id == chapter["id"], GenerationLog.status == "success",
        )))) == 1


async def test_proposal_chunks_round_trip_and_corruption_never_applies(app, client, fake_llm, monkeypatch):
    from app.services import rich_document_review

    # 以较小单块稳定覆盖跨批、Unicode 与转义接缝，而非创建巨大测试数据库。
    monkeypatch.setattr(rich_document_review, "PROPOSAL_CHUNK_CHARS", 17)
    monkeypatch.setattr(rich_document_review, "PROPOSAL_CHUNK_BATCH", 3)
    _project, _chapter, _url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    keys = {"task_key": identity["task_key"], "run_key": identity["run_key"]}
    preview = await client.get(path, params=keys)
    assert preview.status_code == 200 and preview.json()["scenes"][0]["candidate"] == "完全不同的候选稿"
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        chunks = list(await db.scalars(select(SceneReviewPayloadChunk).where(
            SceneReviewPayloadChunk.proposal_id == proposal.id,
        ).order_by(SceneReviewPayloadChunk.ordinal)))
        assert len(chunks) == proposal.payload["chunk_count"] and len(chunks) > 3
        assert all(len(item.content) <= 17 for item in chunks)
        chunks[1].content += "破坏"
        await db.commit()
    assert (await client.get(path, params=keys)).status_code == 409
    rejected = await client.post(f"{path}/accept", json={
        **keys, "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
        "confirm_format_loss": True,
    })
    assert rejected.status_code == 409
    assert (await client.get(document_url)).json() == source and len(fake_llm.calls) == 1


async def test_missing_proposal_chunk_closes_stream_on_early_rejection(app, client, fake_llm, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services import rich_document_review

    monkeypatch.setattr(rich_document_review, "PROPOSAL_CHUNK_CHARS", 17)
    _project, _chapter, _url, _scene, _document_url, _source, task, identity = await _pending(app, client, fake_llm)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        await db.execute(delete(SceneReviewPayloadChunk).where(
            SceneReviewPayloadChunk.proposal_id == proposal.id, SceneReviewPayloadChunk.ordinal == 1,
        ))
        await db.commit()
    streams = []
    original = AsyncSession.stream

    async def observe(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        streams.append(result)
        return result

    monkeypatch.setattr(AsyncSession, "stream", observe)
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    response = await client.get(path, params={"task_key": identity["task_key"], "run_key": identity["run_key"]})
    assert response.status_code == 409 and streams and all(stream.closed for stream in streams)


async def test_pending_review_is_recovered_from_running_checkpoint_without_model_replay(app, client, fake_llm):
    _project, chapter, _url, _scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task["id"]).values(status="running", result=None))
        await db.commit()
    await app.state.task_engine.recover()
    current = (await client.get(f"/api/v1/tasks/{task['id']}")).json()
    assert current["status"] == "paused"
    assert (await stream_task(client, task["id"]))[-1][1]["result"] == identity
    assert (await client.get(document_url)).json() == source and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).status != "generating"


async def test_edit_during_review_keeps_stale_proposal_readable_but_cannot_apply(app, client, fake_llm):
    _project, _chapter, _url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    edited = save_payload(source)
    edited["document"]["sections"][0]["include_in_ai"] = False
    changed = await client.put(document_url, json=edited)
    assert changed.status_code == 200, changed.text
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    keys = {"task_key": identity["task_key"], "run_key": identity["run_key"]}
    accepted = await client.post(f"{path}/accept", json={
        **keys, "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
        "confirm_format_loss": True,
    })
    assert accepted.status_code == 409
    preview = await client.get(path, params=keys)
    assert preview.status_code == 200 and preview.json()["status"] == "pending"
    assert (await client.get(document_url)).json() == changed.json() and len(fake_llm.calls) == 1


@pytest.mark.parametrize("action", ["cancel", "finish"])
async def test_generic_termination_discards_review_without_faking_adoption(app, client, fake_llm, action):
    _project, _chapter, _url, _scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    assert (await client.post(f"/api/v1/tasks/{task['id']}/{action}")).status_code == 409
    ended = await client.post(f"/api/v1/tasks/{task['id']}/{action}", json={
        key: identity[key] for key in ("task_key", "run_key", "token")
    })
    assert ended.status_code == 200 and ended.json()["status"] == "cancelled", ended.text
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "discarded"
    assert (await client.get(document_url)).json() == source and len(fake_llm.calls) == 1


async def test_cancelling_old_paused_proposal_does_not_release_new_task_chapter(app, client, fake_llm):
    _project, chapter, url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    gate = Gate()
    fake_llm.push([gate, "另一轮候选"])
    response = await client.post(f"{url}/scenes/{scene['id']}/generate", json={
        "expected_content_revision": source["content_revision"],
        "expected_planning_revision": source["planning_revision"], "target_words": 300,
    })
    assert response.status_code == 202, response.text
    await gate.wait_reached()
    try:
        ended = await client.post(f"/api/v1/tasks/{task['id']}/cancel", json={
            key: identity[key] for key in ("task_key", "run_key", "token")
        })
        assert ended.status_code == 200 and ended.json()["status"] == "cancelled"
        async with app.state.sessionmaker() as db:
            assert (await db.get(Chapter, chapter["id"])).status == "generating"
    finally:
        gate.release()
    later = await wait_task(client, response.json()["id"], statuses=("paused", "failed", "succeeded"))
    assert later["status"] == "paused" and (await client.get(document_url)).json() == source


async def test_cancel_and_accept_compete_without_partial_write(app, client, fake_llm):
    _project, _chapter, _url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    accept = {"task_key": identity["task_key"], "run_key": identity["run_key"],
              "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
              "confirm_format_loss": True}
    first, second = await asyncio.gather(client.post(f"{path}/accept", json=accept),
                                         client.post(f"/api/v1/tasks/{task['id']}/cancel", json={
                                             key: identity[key] for key in ("task_key", "run_key", "token")
                                         }))
    async with app.state.sessionmaker() as db:
        stored_task = await db.get(Task, task["id"])
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        outcome = stored_task.status, proposal.status
    after = (await client.get(document_url)).json()
    if outcome == ("succeeded", "applied"):
        assert first.status_code == 200 and after["document"]["content"] == "完全不同的候选稿"
    else:
        assert outcome == ("cancelled", "discarded") and second.status_code == 200 and after == source


@pytest.mark.parametrize("change", ["disabled", "token_version"])
async def test_review_accept_rechecks_account_identity_after_authentication(app, client, fake_llm, monkeypatch, change):
    from app.services import setting_ai_visibility

    _project, _chapter, _url, scene, document_url, source, task, identity = await _pending(app, client, fake_llm)
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    original = setting_ai_visibility.lock_author_visibility_write
    reached, proceed = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        reached.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(setting_ai_visibility, "lock_author_visibility_write", delayed)
    body = {"task_key": identity["task_key"], "run_key": identity["run_key"],
            "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
            "confirm_format_loss": True}
    request = asyncio.create_task(client.post(f"{path}/accept", json=body))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        async with app.state.sessionmaker() as db:
            values = {"is_active": False} if change == "disabled" else {"token_version": User.token_version + 1}
            await db.execute(update(User).where(User.id == 1).values(**values))
            await db.commit()
        proceed.set()
        result = await asyncio.wait_for(request, 5)
        assert result.status_code == 401, result.text
    finally:
        proceed.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task["id"])).status == "paused"
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending"
        chapter = await db.get(Chapter, proposal.chapter_id)
        assert chapter.content == source["document"]["content"]


async def test_batch_review_resumes_at_next_phase_without_regenerating_current_chapter(app, client, fake_llm):
    from tests.test_batch_generate import _setup, _start

    _project, base, chapters, _ = await _setup(client, ["第一章", "需要核对的原稿", "第三章"])
    second = chapters[1]
    url = f"{base}/chapters/{second['id']}"
    scene = (await client.get(f"{url}/scenes")).json()["items"][0]
    endpoint = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await client.get(endpoint)).json())
    add_section(draft)
    assert (await client.put(endpoint, json=draft)).status_code == 200
    fake_llm.push("第一章新稿")
    fake_llm.push("第二章候选")
    fake_llm.push("第三章新稿")
    task, events = await run_task(client, await _start(client, base, chapters[0], chapters[2], include_written=True))
    assert task["status"] == "paused" and events[-1][1]["result"]["reason"] == "rich_document_review"
    identity = events[-1][1]["result"]
    assert len(fake_llm.calls) == 2
    assert (await client.get(f"{base}/chapters/{chapters[0]['id']}")).json()["content"] == "第一章新稿"
    assert (await client.get(url)).json()["content"] == "需要核对的原稿"
    assert (await client.get(f"{base}/chapters/{chapters[2]['id']}")).json()["content"] == "第三章"
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    accepted = await client.post(f"{path}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 1}],
        "confirm_format_loss": False,
    })
    assert accepted.status_code == 200, accepted.text
    finished = await wait_task(client, task["id"], statuses=("succeeded", "failed"), within=10)
    assert finished["status"] == "succeeded", finished
    assert [item["state"] for item in finished["checkpoint"]["items"]] == ["done", "done", "done"]
    assert (await client.get(url)).json()["content"] == "第二章候选"
    assert (await client.get(f"{base}/chapters/{chapters[2]['id']}")).json()["content"] == "第三章新稿"
    assert len(fake_llm.calls) == 3


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_batch_review_analysis_committed_before_checkpoint_is_not_sent_again(app, client, fake_llm, monkeypatch):
    from app.services import batch_service
    from tests.test_batch_generate import _setup, _start

    _project, base, (chapter,), _ = await _setup(client, ["原有章节"])
    url = f"{base}/chapters/{chapter['id']}"
    scene = (await client.get(f"{url}/scenes")).json()["items"][0]
    endpoint = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await client.get(endpoint)).json())
    add_section(draft)
    assert (await client.put(endpoint, json=draft)).status_code == 200
    fake_llm.push("新的候选正文")
    fake_llm.push(analysis_json())
    task, events = await run_task(client, await _start(client, base, chapter, chapter, include_written=True))
    assert task["status"] == "paused"
    identity = events[-1][1]["result"]
    original = batch_service._save

    async def fail_after_analysis(ctx, job):
        if job.cursor == 1 and job.checkpoint["items"][0].get("analysis") == "ok":
            raise RuntimeError("模拟分析提交后、游标提交前退出")
        return await original(ctx, job)

    monkeypatch.setattr(batch_service, "_save", fail_after_analysis)
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    accepted = await client.post(f"{path}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 1}],
        "confirm_format_loss": False,
    })
    assert accepted.status_code == 200, accepted.text
    failed = await wait_task(client, task["id"], statuses=("failed",), within=10)
    assert failed["checkpoint"]["phase"] == "analyze" and len(fake_llm.calls) == 2
    async with app.state.sessionmaker() as db:
        receipt = await db.get(SceneReviewAnalysisReceipt, identity["run_key"])
        assert receipt.status == "completed" and receipt.analysis_id is not None
        assert len(list(await db.scalars(select(ChapterAnalysis)))) == 1
    monkeypatch.setattr(batch_service, "_save", original)
    retry = await client.post(f"/api/v1/tasks/{task['id']}/retry")
    assert retry.status_code == 202, retry.text
    completed = await wait_task(client, retry.json()["id"], statuses=("succeeded", "failed"), within=10)
    assert completed["status"] == "succeeded" and len(fake_llm.calls) == 2
    async with app.state.sessionmaker() as db:
        assert len(list(await db.scalars(select(ChapterAnalysis)))) == 1


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_batch_review_analysis_interrupted_in_flight_is_not_automatically_resent(app, client, fake_llm):
    from tests.test_batch_generate import _setup, _start

    _project, base, (chapter,), _ = await _setup(client, ["原有章节"])
    url = f"{base}/chapters/{chapter['id']}"
    scene = (await client.get(f"{url}/scenes")).json()["items"][0]
    endpoint = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await client.get(endpoint)).json())
    add_section(draft)
    assert (await client.put(endpoint, json=draft)).status_code == 200
    gate = Gate()
    fake_llm.push("新的候选正文")
    fake_llm.push([gate, analysis_json()])
    task, events = await run_task(client, await _start(client, base, chapter, chapter, include_written=True))
    identity = events[-1][1]["result"]
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}"
    accepted = await client.post(f"{path}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 1}],
        "confirm_format_loss": False,
    })
    assert accepted.status_code == 200, accepted.text
    await gate.wait_reached()
    await app.state.task_engine.stop()
    await app.state.task_engine.start()
    retry = await client.post(f"/api/v1/tasks/{task['id']}/retry")
    assert retry.status_code == 202, retry.text
    completed = await wait_task(client, retry.json()["id"], statuses=("succeeded", "failed"), within=10)
    assert completed["status"] == "succeeded" and len(fake_llm.calls) == 2
    assert completed["checkpoint"]["items"][0]["analysis"] == "failed"
    assert "未自动重复" in completed["checkpoint"]["items"][0]["warning"]
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(ChapterAnalysis))) == []
