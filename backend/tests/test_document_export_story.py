"""从同快照按章或按活动场景选择正文及各自摘要。"""

import hashlib

import pytest
from pydantic import ValidationError

from app.core.errors import NotFoundError
from app.models import Chapter, ChapterAnalysis, Scene
from app.schemas.export import DocumentExportRequest, StorySelection
from app.services.document_export.model import DocumentModel
from app.services.document_export.renderers import render_text
from app.services.document_export.story import load_story
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scenes import setup_scene


def test_selection_schema_distinguishes_all_none_and_empty_selected():
    for body in (
        {"story": {"scope": "selected", "ids": []}},
        {"story": {"scope": "all", "ids": [1]}},
        {"story": {"scope": "selected", "ids": [1, 1]}},
        {"story": {"scope": "selected", "ids": [True]}},
        {"materials": {"settings": [{"kind": "world", "id": 1}, {"kind": "world", "id": 1}]}},
        {"materials": {"note_ids": None}},
        {"story": {"extra": "不允许"}},
    ):
        with pytest.raises(ValidationError):
            DocumentExportRequest.model_validate({"format": "docx", **body})
    assert DocumentExportRequest.model_validate({"format": "md"}).story.scope == "all"


async def test_selected_scene_includes_only_its_summary_and_prose(app, client):
    project, _chapter, url, first = await setup_scene(client, "第一景秘密🙂")
    created = await client.post(f"{url}/scenes", json={
        "expected_content_revision": 0, "title": "第二景", "content": "第二景正文", "summary": "第二景摘要",
    })
    assert created.status_code == 201, created.text
    second = created.json()
    selection = StorySelection(scope="selected", unit="scene", content="both", ids=[second["id"]])
    async with app.state.sessionmaker() as db:
        result = await load_story(db, project["id"], selection, include_formatting=True)
        invalid = StorySelection(scope="selected", unit="scene", content="both", ids=[first["id"], 2147483647])
        with pytest.raises(NotFoundError):
            await load_story(db, project["id"], invalid, include_formatting=True)
    text = render_text(DocumentModel(nodes=result.nodes), markdown=False)
    assert "第二景摘要" in text and "第二景正文" in text and "第一景秘密" not in text
    assert text.index("第二景摘要") < text.index("第二景正文")


async def test_chapter_latest_analysis_stale_and_missing_summary(app, client):
    project, chapter, _url, _scene = await setup_scene(client, "当前正文")
    async with app.state.sessionmaker() as db:
        db.add(ChapterAnalysis(project_id=project["id"], chapter_id=chapter["id"], chapter_no=1,
                               content_hash=hashlib.sha256("旧正文".encode()).hexdigest(), summary="历史摘要"))
        await db.commit()
        result = await load_story(db, project["id"], StorySelection(content="both"), include_formatting=False)
    text = render_text(DocumentModel(nodes=result.nodes), markdown=False)
    assert "历史摘要" in text and "摘要可能过期" in text and "当前正文" in text

    async with app.state.sessionmaker() as db:
        db.add(ChapterAnalysis(project_id=project["id"], chapter_id=chapter["id"], chapter_no=1,
                               content_hash=hashlib.sha256("当前正文".encode()).hexdigest(), summary=""))
        await db.commit()
        result = await load_story(db, project["id"], StorySelection(content="summary"), include_formatting=False)
    text = render_text(DocumentModel(nodes=result.nodes), markdown=False)
    assert "暂无摘要" in text and "历史摘要" not in text and "当前正文" not in text
    assert result.warnings == ["有 1 个所选单元暂无摘要"]


async def test_chapter_export_marks_enabled_section_change_stale_without_exporting_section(app, client, fake_llm):
    project, chapter, url, scene = await setup_scene(client, "未更改的正文")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    document = save_payload((await client.get(endpoint)).json())
    add_section(document, enabled=True)
    document["document"]["sections"][0]["content"] = "仅用于分析的作者区块"
    response = await client.put(endpoint, json=document)
    assert response.status_code == 200, response.text

    # 模拟创建区块前的旧分析：正文未改，区块已进入分析来源，因此旧摘要必须标记过期。
    async with app.state.sessionmaker() as db:
        db.add(ChapterAnalysis(project_id=project["id"], chapter_id=chapter["id"], chapter_no=1,
                               content_hash=hashlib.sha256("未更改的正文".encode()).hexdigest(), summary="旧摘要"))
        await db.commit()
    response = await client.post(f"/api/v1/projects/{project['id']}/export-document", json={
        "format": "txt", "story": {"scope": "all", "unit": "chapter", "content": "both", "ids": []},
    })
    assert response.status_code == 200, response.text
    assert "旧摘要" in response.text and "摘要可能过期" in response.text and "未更改的正文" in response.text
    assert "仅用于分析的作者区块" not in response.text
    assert not fake_llm.calls


async def test_scene_selection_batches_ids_and_preserves_book_order(app, client):
    project, chapter, _url, first = await setup_scene(client, "起景")
    async with app.state.sessionmaker() as db:
        current = await db.get(Chapter, chapter["id"])
        scenes = [Scene(chapter_id=chapter["id"], title=f"场景{i:03}", content=f"正文{i:03}", sort=i,
                        separator_before="\n\n") for i in range(1, 206)]
        db.add_all(scenes)
        current.content = "起景" + "".join(f"\n\n正文{i:03}" for i in range(1, 206))
        current.content_revision += 1
        await db.commit()
        ids = [first["id"], *(scene.id for scene in scenes)]
        selection = StorySelection(scope="selected", unit="scene", content="prose", ids=ids[::-1])
        story = await load_story(db, project["id"], selection, include_formatting=False)
    text = render_text(DocumentModel(nodes=story.nodes), markdown=False)
    assert text.count("正文") == 205
    assert text.index("起景") < text.index("正文001") < text.index("正文205")
