"""分析来源、旧指纹、格式变化及人工回写冲突必须使用同一文档指纹。"""

import hashlib

import pytest
from sqlalchemy import select

from app.models import Chapter, ChapterAnalysis, StoryMemory
from app.services import rich_document_ai, scene_service
from app.services.rich_document_core import analysis_content_fingerprint
from app.services.rich_document_store import capture_documents
from tests.helpers import Gate, analysis_json, run_task, wait_task
from tests.test_analysis import _analyze, _setup
from tests.test_project_transfer import _backup, _import
from tests.test_rich_document_ai_context import _add_visibility_examples
from tests.test_rich_document_api import add_section, save_payload
from tests.test_writeback import _analyze as analyze_changes
from tests.test_writeback import _by, _world


async def _endpoint(client, base, chapter_id):
    url = f"{base}/chapters/{chapter_id}/scenes"
    scene = (await client.get(url)).json()["items"][0]
    return f"{url}/{scene['id']}/document"


async def test_analysis_legacy_baseline_visible_sources_and_format_only_stability(app, client, fake_llm):
    project, base, (chapter,) = await _setup(client, contents=("正式正文甲乙",))
    fake_llm.push(analysis_json(foreshadows=[]))
    task, _events = await run_task(client, await _analyze(client, chapter))
    assert task["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        old = await db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.chapter_id == chapter["id"]))
        assert old.content_hash == hashlib.sha256(chapter["content"].encode()).hexdigest()
    endpoint = await _endpoint(client, base, chapter["id"])
    await _add_visibility_examples(client, endpoint)
    state = (await client.get(f"/api/v1/chapters/{chapter['id']}/analysis-status")).json()
    assert state["status"] == "stale"
    fake_llm.push(analysis_json(foreshadows=[]))
    task, _events = await run_task(client, await _analyze(client, chapter))
    assert task["status"] == "succeeded"
    messages = str(fake_llm.calls[-1]["messages"])
    assert "正式正文甲乙" in messages and "开启区块名称" in messages and "开启区块内容" in messages
    assert "关闭区块名称" not in messages and "关闭区块内容" not in messages
    assert (await client.get(f"/api/v1/chapters/{chapter['id']}/analysis-status")).json()["status"] == "analyzed"
    latest = (await client.get(f"/api/v1/chapters/{chapter['id']}/analyses")).json()[0]
    detail = (await client.get(f"/api/v1/analyses/{latest['id']}")).json()
    assert detail["metrics"]["words"] == len(chapter["content"])
    memories = (await client.get(f"{base}/memories")).json()["items"]
    assert memories and all(item["source_stale"] is False for item in memories if item["origin"] == "auto")
    manual = await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "人工钉住事实", "pinned": True})
    assert manual.status_code == 201 and manual.json()["source_stale"] is None
    async with app.state.sessionmaker() as db:
        legacy_memory = StoryMemory(project_id=project["id"], chapter_id=chapter["id"], chapter_no=1,
                                    kind="summary", content="来源未知的旧记忆", origin="auto", pinned=True)
        db.add(legacy_memory)
        await db.commit()
        legacy_memory_id = legacy_memory.id
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 0, "end": 1}]
    formatted = await client.put(endpoint, json=body)
    assert formatted.status_code == 200, formatted.text
    assert (await client.get(f"/api/v1/chapters/{chapter['id']}/analysis-status")).json()["status"] == "analyzed"
    memories = (await client.get(f"{base}/memories")).json()["items"]
    assert all(item["source_stale"] is False for item in memories
               if item["origin"] == "auto" and item["analysis_id"] is not None)
    assert next(item for item in memories if item["id"] == legacy_memory_id)["source_stale"] is None
    changed = save_payload(formatted.json())
    changed["document"]["sections"][0]["content"] = "修改过的开启区块"
    edited = await client.put(endpoint, json=changed)
    assert edited.status_code == 200, edited.text
    assert (await client.get(f"/api/v1/chapters/{chapter['id']}/analysis-status")).json()["status"] == "stale"
    assert (await client.get(f"/api/v1/analyses/{latest['id']}")).json()["stale"] is True
    memories = (await client.get(f"{base}/memories")).json()["items"]
    assert all(item["source_stale"] is True for item in memories
               if item["origin"] == "auto" and item["analysis_id"] is not None)
    assert next(item for item in memories if item["id"] == legacy_memory_id)["source_stale"] is None
    assert next(item for item in memories if item["id"] == manual.json()["id"])["source_stale"] is None
    disabled = save_payload(edited.json())
    disabled["document"]["sections"][0]["include_in_ai"] = False
    assert (await client.put(endpoint, json=disabled)).status_code == 200
    assert (await client.get(f"/api/v1/chapters/{chapter['id']}/analysis-status")).json()["status"] == "stale"
    assert project["id"] > 0


@pytest.mark.parametrize("mutation,expected", [("none", 200), ("format", 200), ("section", 409), ("disable", 409)])
async def test_analysis_writeback_confirmation_uses_the_same_fingerprint(client, fake_llm, mutation, expected):
    base, _chars, _rel, _career, _stages, _org, chapters = await _world(client)
    chapter = chapters[3]
    endpoint = await _endpoint(client, base, chapter["id"])
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    changes = await analyze_changes(
        client, fake_llm, chapter, character_changes=[{"name": "苏晚", "life_status": "dead"}],
    )
    pending = _by(changes, "life_status")[0]
    assert pending["status"] == "pending" and "_timeline_revision" in pending["target"]
    if mutation != "none":
        edit = save_payload((await client.get(endpoint)).json())
        if mutation == "format":
            edit["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 0, "end": 1}]
        elif mutation == "section":
            edit["document"]["sections"][0]["content"] = "新的可见区块"
        else:
            edit["document"]["sections"][0]["include_in_ai"] = False
        response = await client.put(endpoint, json=edit)
        assert response.status_code == 200, response.text
    confirmed = await client.post(f"/api/v1/analysis-changes/{pending['id']}/confirm")
    assert confirmed.status_code == expected, confirmed.text


async def test_shared_fingerprint_matches_pure_projection_for_multi_scene_unicode(app, client):
    _project, base, (chapter,) = await _setup(client, contents=("首🙂\r\n景",))
    scenes_url = f"{base}/chapters/{chapter['id']}/scenes"
    first = (await client.get(scenes_url)).json()["items"][0]
    second = await client.post(scenes_url, json={"expected_content_revision": 0, "content": "次\n景"})
    assert second.status_code == 201, second.text
    for scene_id in (first["id"], second.json()["id"]):
        endpoint = f"{scenes_url}/{scene_id}/document"
        body = save_payload((await client.get(endpoint)).json())
        add_section(body, enabled=True)
        assert (await client.put(endpoint, json=body)).status_code == 200
    async with app.state.sessionmaker() as db:
        chapter_row = await db.get(Chapter, chapter["id"])
        scenes = await scene_service.list_active_scenes(db, chapter["id"])
        documents, _ = await capture_documents(db, scenes)
        expected = analysis_content_fingerprint([
            (scene.separator_before + scene.content, documents[scene.id].sections) for scene in scenes
        ])
        actual = (await rich_document_ai.analysis_fingerprints(db, [chapter_row]))[chapter["id"]]
    assert expected == actual


async def test_backup_id_remap_preserves_analysis_freshness_for_enabled_sections(client, fake_llm):
    project, base, (chapter,) = await _setup(client, contents=("正文🙂\r\n",))
    endpoint = await _endpoint(client, base, chapter["id"])
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    assert (await client.put(endpoint, json=body)).status_code == 200
    fake_llm.push(analysis_json(foreshadows=[]))
    task, _events = await run_task(client, await _analyze(client, chapter))
    assert task["status"] == "succeeded"
    backup = await _backup(client, project["id"])
    imported = await _import(client, backup)
    new_project = imported["project"]["id"]
    after = await _backup(client, new_project)
    new_chapter = after["tables"]["chapters"][0]["id"]
    assert new_chapter != chapter["id"]
    state = await client.get(f"/api/v1/chapters/{new_chapter}/analysis-status")
    assert state.status_code == 200 and state.json()["status"] == "analyzed"


@pytest.mark.parametrize("mutation,expected", [("format", "succeeded"), ("section", "failed"), ("disable", "failed")])
async def test_inflight_analysis_rechecks_actual_source_before_write(app, client, fake_llm, mutation, expected):
    _project, base, (chapter,) = await _setup(client, contents=("可分析正文",))
    endpoint = await _endpoint(client, base, chapter["id"])
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    assert (await client.put(endpoint, json=body)).status_code == 200
    gate = Gate()
    fake_llm.push([gate, analysis_json(foreshadows=[])])
    started = await _analyze(client, chapter)
    assert started.status_code == 202, started.text
    try:
        await gate.wait_reached()
        edit = save_payload((await client.get(endpoint)).json())
        if mutation == "format":
            edit["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 0, "end": 1}]
        elif mutation == "section":
            edit["document"]["sections"][0]["content"] = "新资料"
        else:
            edit["document"]["sections"][0]["include_in_ai"] = False
        assert (await client.put(endpoint, json=edit)).status_code == 200
    finally:
        gate.release()
    result = await wait_task(client, started.json()["id"])
    assert result["status"] == expected, result
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(ChapterAnalysis).where(ChapterAnalysis.chapter_id == chapter["id"])))
    assert len(rows) == (1 if expected == "succeeded" else 0)
