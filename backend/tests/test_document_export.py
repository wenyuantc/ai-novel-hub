"""通过真实站内 HTTP 接口验证文件、选择及旧接口兼容。"""

import asyncio
import io
import re
import threading
from urllib.parse import unquote

from docx import Document
from sqlalchemy import select, update

from app.models import AgentConversation, AgentMessage, Chapter, User
from app.services.document_export import service
from tests.helpers import create_project, register
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scenes import setup_scene


async def test_documents_support_selected_scenes_summaries_notes_and_word(client, make_client):
    project, chapter, url, first = await setup_scene(client, "第一景🙂的原文")
    base = f"/api/v1/projects/{project['id']}"
    second_response = await client.post(f"{url}/scenes", json={
        "expected_content_revision": 0, "title": "第二景", "summary": "第二景概要", "content": "第二景正文",
    })
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    note = await client.post(f"{base}/notes", json={"title": "导出笔记", "content": "笔记正文" * 100})
    assert note.status_code == 201, note.text
    omitted = await client.post(f"{base}/notes", json={"title": "不应出现的笔记", "content": "未选笔记秘密"})
    assert omitted.status_code == 201
    character = await client.post(f"{base}/characters", json={"name": "景岚", "profile": "已选人物档案"})
    omitted_character = await client.post(f"{base}/characters", json={"name": "阿雪", "profile": "不应导出档案"})
    assert character.status_code == omitted_character.status_code == 201
    document_url = f"{url}/scenes/{first['id']}/document"
    rich = save_payload((await client.get(document_url)).json())
    add_section(rich)
    rich["document"]["sections"][0]["content"] = "绝不可导出的作者区块"
    assert (await client.put(document_url, json=rich)).status_code == 200
    second_document_url = f"{url}/scenes/{second['id']}/document"
    second_rich = save_payload((await client.get(second_document_url)).json())
    second_rich["document"]["formatting"]["inline"] = [
        {"kind": "bold", "start": 0, "end": 3},
        {"kind": "italic", "start": 2, "end": 5},
    ]
    assert (await client.put(second_document_url, json=second_rich)).status_code == 200
    options = {
        "story": {"scope": "selected", "unit": "scene", "content": "both", "ids": [second["id"]]},
        "materials": {"settings": [{"kind": "character", "id": character.json()["id"]}],
                      "conversation_ids": [], "note_ids": [note.json()["id"]]},
    }
    for fmt in ("txt", "md", "docx"):
        response = await client.post(f"{base}/export-document", json={"format": fmt, **options})
        assert response.status_code == 200, response.text if fmt != "docx" else response.status_code
        assert response.headers["cache-control"] == "no-store"
        assert f"novel.{fmt}" in response.headers["content-disposition"]
        if fmt == "docx":
            assert response.headers["content-type"].startswith(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            document = Document(io.BytesIO(response.content))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            assert any(run.bold and run.italic and run.text == "景"
                       for paragraph in document.paragraphs for run in paragraph.runs)
        else:
            text = response.text
            if fmt == "md":
                assert "<strong><em>景</em></strong>" in text
        visible = re.sub(r"</?(?:strong|em)>", "", text)
        assert "第二景概要" in visible and "第二景正文" in visible and "笔记正文" * 100 in visible
        assert "景岚" in visible and "已选人物档案" in visible and "不应导出档案" not in visible
        assert visible.index("第二景概要") < visible.index("第二景正文") < visible.index("导出笔记")
        assert "第一景🙂的原文" not in text and "不应出现的笔记" not in text
        assert "绝不可导出的作者区块" not in text

    # 资料可独立导出；选择空并不回退为整书正文。
    response = await client.post(f"{base}/export-document", json={"format": "txt", "story": {
        "scope": "none", "unit": "chapter", "content": "prose", "ids": [],
    }, "materials": {"note_ids": [note.json()["id"]]}})
    assert response.status_code == 200 and "笔记正文" in response.text
    assert "第二景正文" not in response.text and "第一景🙂的原文" not in response.text
    outsider = make_client()
    await register(outsider, "outsider")
    assert (await outsider.post(f"{base}/export-document", json={"format": "docx"})).status_code == 404
    # EPUB/既有 GET 及无章节时的原有行为不因新接口改变。
    assert (await client.get(f"{base}/export", params={"format": "epub"})).status_code == 200
    assert (await client.get(f"{base}/export", params={"format": "docx"})).status_code == 422


async def test_export_validation_and_generating_and_empty_warnings(app, client):
    project, chapter, url, _scene = await setup_scene(client, "可读正文")
    base = f"/api/v1/projects/{project['id']}"
    empty = await client.post(f"{base}/chapters", json={"title": "空章"})
    assert empty.status_code == 201, empty.text
    assert (await client.post(f"{base}/export-document", json={"format": "txt", "story": {
        "scope": "selected", "unit": "chapter", "content": "prose", "ids": [],
    }})).status_code == 422
    assert (await client.post(f"{base}/export-document", json={"format": "txt", "story": {
        "scope": "none", "unit": "chapter", "content": "prose", "ids": [],
    }})).status_code == 400
    response = await client.post(f"{base}/export-document", json={"format": "txt"})
    assert response.status_code == 200 and "可读正文" in response.text
    assert "已跳过 1 个正文为空的章节" in unquote(response.headers["x-export-warnings"])
    summaries = await client.post(f"{base}/export-document", json={
        "format": "md", "story": {"scope": "all", "unit": "chapter", "content": "summary", "ids": []},
    })
    assert summaries.status_code == 200 and summaries.text.count("暂无摘要") == 2
    assert "有 2 个所选单元暂无摘要" in unquote(summaries.headers["x-export-warnings"])
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == chapter["id"]).values(status="generating"))
        await db.commit()
    selected = await client.post(f"{base}/export-document", json={"format": "txt", "story": {
        "scope": "selected", "unit": "chapter", "content": "prose", "ids": [chapter["id"]],
    }})
    assert selected.status_code == 409 and selected.json()["code"] == "chapter_generating"
    all_summaries = await client.post(f"{base}/export-document", json={
        "format": "txt", "story": {"scope": "all", "unit": "chapter", "content": "summary", "ids": []},
    })
    assert all_summaries.status_code == 200 and "正在生成的章节" in unquote(all_summaries.headers["x-export-warnings"])
    other = await create_project(client, title="另一部作品")
    wrong_chapter = await client.post(f"/api/v1/projects/{other['id']}/chapters", json={"title": "不属于本书"})
    rejected = await client.post(f"{base}/export-document", json={"format": "txt", "story": {
        "scope": "selected", "unit": "chapter", "content": "prose", "ids": [wrong_chapter.json()["id"]],
    }})
    assert rejected.status_code == 404 and "不属于本书" not in rejected.text


async def test_http_exports_own_chat_and_excludes_tools(app, client):
    project, _chapter, _url, _scene = await setup_scene(client, "作品正文")
    base = f"/api/v1/projects/{project['id']}"
    async with app.state.sessionmaker() as db:
        user_id = await db.scalar(select(User.id).where(User.username == "alice"))
        chat = AgentConversation(user_id=user_id, project_id=project["id"], title="构思交流")
        db.add(chat)
        await db.flush()
        chat_id = chat.id
        db.add_all([
            AgentMessage(conversation_id=chat_id, role="user", content="我们讨论情节"),
            AgentMessage(conversation_id=chat_id, role="assistant", content="这是故事构思"),
            AgentMessage(conversation_id=chat_id, role="tool", content="工具不应输出"),
        ])
        await db.commit()
    response = await client.post(f"{base}/export-document", json={"format": "txt", "story": {"scope": "none"},
                              "materials": {"conversation_ids": [chat_id]}})
    assert response.status_code == 200, response.text
    assert "构思交流" in response.text and "我们讨论情节" in response.text and "这是故事构思" in response.text
    assert "工具不应输出" not in response.text and "作品正文" not in response.text


async def test_http_rechecks_original_token_after_word_render(app, client, monkeypatch):
    project, _chapter, _url, _scene = await setup_scene(client, "导出前已保存的正文")
    entered = threading.Event()
    release = threading.Event()
    original = service.render_docx

    def paused_render(model):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("测试未能释放渲染")
        return original(model)

    monkeypatch.setattr(service, "render_docx", paused_render)
    request = asyncio.create_task(client.post(f"/api/v1/projects/{project['id']}/export-document", json={
        "format": "docx",
    }))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.username == "alice").values(token_version=User.token_version + 1))
            await db.commit()
    finally:
        release.set()
    response = await request
    assert response.status_code == 401
    assert "Content-Disposition" not in response.headers and "未登录" in response.json()["detail"]
