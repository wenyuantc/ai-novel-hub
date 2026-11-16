"""站内文档导出：授权、快照、纯渲染与交付前复核。"""

import asyncio
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.schemas.export import DocumentExportRequest
from app.services import project_access
from app.services.agent.export_reads import capture_document_access, verify_document_access
from app.services.document_export.materials import load_materials
from app.services.document_export.model import DocumentModel, DocumentNode
from app.services.document_export.renderers import render_docx, render_text
from app.services.document_export.story import load_story
from app.services.export_service import ExportFile

_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


def _material_nodes(model: DocumentModel, title: str, items) -> None:
    if not items:
        return
    model.nodes.append(DocumentNode("heading", title, level=1, new_page=True, source=title))
    if title == "设定":
        model.nodes.append(DocumentNode("text", "设定为当前值，可能包含计划章节的变化。", source=title))
    for item in items:
        model.nodes.append(DocumentNode("heading", item.title, level=2, source=title))
        for label, content in item.fields:
            if content:
                model.nodes.append(DocumentNode("text", f"{label}：{content}" if label else content, source=title))


def _chat_nodes(model: DocumentModel, conversations: list[dict]) -> None:
    if not conversations:
        return
    model.nodes.append(DocumentNode("heading", "聊天", level=1, new_page=True, source="聊天"))
    for conversation in conversations:
        title = conversation["title"] or "未命名会话"
        model.nodes.append(DocumentNode("heading", title, level=2, source="聊天"))
        for message in conversation["messages"]:
            role = "用户" if message["role"] == "user" else "助手"
            timestamp = message.get("created_at")
            if timestamp and hasattr(timestamp, "isoformat"):
                timestamp = timestamp.isoformat()
            label = f"{role}（{timestamp}）" if timestamp else role
            model.nodes.append(DocumentNode("heading", label, level=3, source="聊天"))
            model.nodes.append(DocumentNode("text", message["content"], source="聊天"))


async def export_document(
    db: AsyncSession, user_id: int, project_id: int, request: DocumentExportRequest,
) -> ExportFile:
    project = await project_access.require_project(db, user_id, project_id, "export")
    capture = await capture_document_access(db, user_id, project_id, request.materials.conversation_ids)
    if (request.story.scope == "none" and not request.materials.settings
            and not request.materials.conversation_ids and not request.materials.note_ids):
        raise BadRequestError("请至少选择正文、摘要或一项创作资料")

    # ORM 对象与 session 都不进入工作线程。
    title, genre, premise = project.title, project.genre, project.premise
    story = await load_story(db, project_id, request.story,
                             include_formatting=request.format != "txt" and request.story.content != "summary")
    settings, notes = await load_materials(db, project_id, request.materials.settings, request.materials.note_ids)
    if not story.nodes and not settings and not capture.conversations and not notes:
        raise BadRequestError("没有可导出的内容，请调整选择后重试")
    model = DocumentModel(nodes=[DocumentNode("heading", title, level=0, source="作品标题")],
                          warnings=story.warnings)
    if genre:
        model.nodes.append(DocumentNode("text", f"题材：{genre}", source="作品信息"))
    if premise:
        model.nodes.append(DocumentNode("text", f"简介：{premise}", source="作品信息"))
    model.nodes.extend(story.nodes)
    _material_nodes(model, "设定", settings)
    _chat_nodes(model, capture.conversations)
    _material_nodes(model, "笔记", notes)
    base = _UNSAFE_FILENAME.sub("_", title).strip() or "novel"

    # 完成读取后释放快照，避免长时间持有数据库事务。
    await db.rollback()
    if request.format == "docx":
        content = await asyncio.to_thread(render_docx, model)
    else:
        content = await asyncio.to_thread(render_text, model, markdown=request.format == "md")
    await verify_document_access(db, capture)
    return ExportFile(f"{base}.{request.format}", content, _MIME[request.format], model.warnings)
