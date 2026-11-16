"""按章或活动场景读取正式正文与摘要，正文和格式来自同一条 SQL。"""

from dataclasses import dataclass, field
from itertools import batched
from typing import Any

from sqlalchemy import and_, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, Scene, SceneDocumentMeta
from app.schemas.export import StorySelection
from app.schemas.rich_document import TextFormatting
from app.services.analysis_service import latest_analyses
from app.services.document_export.model import DocumentNode
from app.services.export_service import chapter_heading
from app.services.rich_document_ai import analysis_fingerprints
from app.services.rich_document_render import chapter_projection, prose_blocks


@dataclass
class StoryResult:
    nodes: list[DocumentNode] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SceneSnapshot:
    id: int
    title: str
    summary: str
    separator: str
    content: str
    formatting: dict[str, Any] | None


@dataclass
class ChapterSnapshot:
    chapter: Chapter
    scenes: list[SceneSnapshot]
    blocks: list | None


async def _selected_chapter_ids(db: AsyncSession, project_id: int, selection: StorySelection) -> list[int]:
    """显式选择先查所属身份；返回整章集合用于单条 SQL 验证场景投影。"""
    if selection.scope == "none":
        return []
    if selection.scope == "all":
        return list(await db.scalars(select(Chapter.id).where(Chapter.project_id == project_id)))
    found: dict[int, tuple[int, str]] = {}
    for batch in batched(selection.ids, 200):
        if selection.unit == "chapter":
            result = await db.execute(select(Chapter.id, Chapter.id, Chapter.status).where(
                Chapter.project_id == project_id, Chapter.id.in_(batch),
            ))
        else:
            result = await db.execute(select(Scene.id, Chapter.id, Chapter.status).join(
                Chapter, Chapter.id == Scene.chapter_id,
            ).where(Chapter.project_id == project_id, Scene.active.is_(True), Scene.id.in_(batch)))
        for item_id, chapter_id, status in result:
            found[item_id] = (chapter_id, status)
    if set(found) != set(selection.ids):
        raise NotFoundError("所选章节或场景不存在")
    if any(status == "generating" for _id, status in found.values()):
        raise ConflictError("所选章节正在生成，请稍后重试", extra={"code": "chapter_generating"})
    return list(dict.fromkeys(chapter_id for chapter_id, _status in found.values()))


async def _snapshots(
    db: AsyncSession, project_id: int, chapter_ids: list[int], *, include_formatting: bool,
) -> list[ChapterSnapshot]:
    snapshots = []
    # 窗口函数只在每章首景携带一次 Chapter.content，防止正文按场景数膨胀。
    fields = [getattr(Chapter, col.key) for col in Chapter.__mapper__.column_attrs if col.key != "content"]
    for batch in batched(chapter_ids, 200):
        first_content = case((func.row_number().over(
            partition_by=Chapter.id, order_by=(Scene.sort, Scene.id),
        ) == 1, Chapter.content), else_=None)
        formatting = SceneDocumentMeta.formatting if include_formatting else literal(None)
        statement = (select(Chapter, first_content, Scene.id, Scene.title, Scene.summary,
                            Scene.separator_before, Scene.content, formatting)
                     .options(load_only(*fields))
                     .outerjoin(Scene, and_(Scene.chapter_id == Chapter.id, Scene.active.is_(True))))
        if include_formatting:
            statement = statement.outerjoin(SceneDocumentMeta, SceneDocumentMeta.scene_id == Scene.id)
        statement = (statement.where(Chapter.project_id == project_id, Chapter.id.in_(batch))
                     .order_by(Chapter.chapter_no, Chapter.id, Scene.sort, Scene.id)
                     .execution_options(populate_existing=True))
        grouped: dict[int, tuple[Chapter, list[SceneSnapshot]]] = {}
        for chapter, content, scene_id, title, summary, separator, scene_content, raw in await db.execute(statement):
            if chapter.id not in grouped:
                if content is None:
                    raise ConflictError("章节正文快照不完整，无法导出", extra={"code": "scene_invariant_error"})
                set_committed_value(chapter, "content", content)
                grouped[chapter.id] = (chapter, [])
            if scene_id is not None:
                grouped[chapter.id][1].append(SceneSnapshot(scene_id, title, summary, separator, scene_content, raw))
        for chapter, scenes in grouped.values():
            if chapter.status == "generating":
                # 生成中章节由调用方决定排除或冲突，不需要读取其投影。
                snapshots.append(ChapterSnapshot(chapter, scenes, None))
                continue
            projection = chapter_projection(chapter, [
                (scene.id, scene.separator, scene.content, scene.formatting) for scene in scenes
            ])
            snapshots.append(ChapterSnapshot(chapter, scenes, projection.blocks))
    return sorted(snapshots, key=lambda value: (value.chapter.chapter_no, value.chapter.id))


def _body_nodes(
    selection: StorySelection, source: str, content: str, blocks: list | None,
    summary: str, summary_note: str,
) -> tuple[list[DocumentNode], bool]:
    nodes = []
    missing = False
    if selection.content in {"summary", "both"}:
        nodes.append(DocumentNode("heading", "摘要", level=4, source=source))
        if summary_note:
            nodes.append(DocumentNode("text", summary_note, source=source))
        missing = not summary.strip()
        nodes.append(DocumentNode("text", summary if not missing else "暂无摘要", source=source))
    if selection.content in {"prose", "both"} and content.strip():
        if selection.content == "both":
            nodes.append(DocumentNode("heading", "正文", level=4, source=source))
        nodes.append(DocumentNode("prose", content, blocks=blocks, source=source))
    return nodes, missing


async def load_story(
    db: AsyncSession, project_id: int, selection: StorySelection, *, include_formatting: bool,
) -> StoryResult:
    result = StoryResult()
    if selection.scope == "none":
        return result
    chapter_ids = await _selected_chapter_ids(db, project_id, selection)
    snapshots = await _snapshots(db, project_id, chapter_ids, include_formatting=include_formatting)
    by_id = {snap.chapter.id: snap for snap in snapshots}
    if selection.scope == "selected" and set(by_id) != set(chapter_ids):
        raise NotFoundError("所选章节或场景不存在")
    generating = [snap for snap in snapshots if snap.chapter.status == "generating"]
    if generating and selection.scope == "selected":
        raise ConflictError("所选章节正在生成，请稍后重试", extra={"code": "chapter_generating"})
    if generating:
        result.warnings.append(f"已跳过 {len(generating)} 个正在生成的章节")
    snapshots = [snap for snap in snapshots if snap.chapter.status != "generating"]
    if selection.scope == "selected" and selection.unit == "scene":
        available = {scene.id for snap in snapshots for scene in snap.scenes}
        if not set(selection.ids).issubset(available):
            raise NotFoundError("所选章节或场景不存在")

    analyses = {}
    fingerprints = {}
    if selection.unit == "chapter" and selection.content in {"summary", "both"}:
        chapters = [snap.chapter for snap in snapshots]
        for batch in batched([chapter.id for chapter in chapters], 200):
            analyses.update(await latest_analyses(db, list(batch)))
        fingerprints = await analysis_fingerprints(db, chapters)

    skipped = 0
    missing = 0
    selected = set(selection.ids)
    for snap in snapshots:
        chapter = snap.chapter
        chapter_title = chapter_heading(chapter)
        if selection.unit == "chapter":
            if selection.content == "prose" and not chapter.content.strip():
                skipped += 1
                continue
            analysis = analyses.get(chapter.id)
            note = ""
            if analysis:
                note = f"章节分析时间：{analysis.created_at.isoformat()}"
                if analysis.content_hash != fingerprints.get(chapter.id):
                    note += "；正文或分析来源已修改，摘要可能过期"
            nodes, no_summary = _body_nodes(selection, chapter_title, chapter.content, snap.blocks,
                                             analysis.summary if analysis else "", note)
            result.nodes.append(DocumentNode("heading", chapter_title, level=1, new_page=True, source=chapter_title))
            result.nodes.extend(nodes)
            missing += int(no_summary)
            continue

        chapter_nodes = []
        for index, scene in enumerate(snap.scenes, 1):
            if selection.scope == "selected" and scene.id not in selected:
                continue
            if selection.content == "prose" and not scene.content.strip():
                skipped += 1
                continue
            title = scene.title.strip() or f"场景 {index}"
            blocks = None
            if include_formatting and scene.formatting:
                formatting = TextFormatting.model_validate(scene.formatting)
                if formatting.inline or formatting.blocks:
                    blocks = prose_blocks(scene.content, formatting)
            nodes, no_summary = _body_nodes(selection, title, scene.content, blocks, scene.summary,
                                             "已保存的场景摘要" if selection.content != "prose" else "")
            chapter_nodes.append(DocumentNode("heading", title, level=2, source=title))
            chapter_nodes.extend(nodes)
            missing += int(no_summary)
        if chapter_nodes:
            result.nodes.append(DocumentNode("heading", chapter_title, level=1, new_page=True, source=chapter_title))
            result.nodes.extend(chapter_nodes)
    if skipped:
        result.warnings.append(f"已跳过 {skipped} 个正文为空的{'章节' if selection.unit == 'chapter' else '场景'}")
    if missing:
        result.warnings.append(f"有 {missing} 个所选单元暂无摘要")
    return result
