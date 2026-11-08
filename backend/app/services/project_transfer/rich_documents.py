"""v8 富文档核心图校验：格式、区块和标记必须属于同景。"""

from collections import defaultdict
from collections.abc import Callable
from uuid import UUID

from pydantic import ValidationError

from app.schemas.rich_document import (
    MAX_MARKER_EXCERPT,
    DocumentSection,
    SceneDocument,
    validate_chapter_document_boundaries,
)
from app.services.rich_document_core import validate_storage_bounds


def _validate_lineage(rows: list[dict], field: str, label: str, error: Callable[[str], None]) -> None:
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        origin_id = row.get(field)
        if origin_id is None:
            continue
        origin = by_id.get(origin_id) if type(origin_id) is int else None
        try:
            valid = origin is not None and UUID(origin["logical_key"]) == UUID(row["logical_key"])
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            error(f"{label}来源缺失、跨作品或逻辑身份不一致")
    done: set[int] = set()
    for row_id in by_id:
        chain: set[int] = set()
        current = row_id
        while current not in done and current in by_id:
            if current in chain:
                error(f"{label}来源存在循环")
                break
            chain.add(current)
            parent = by_id[current].get(field)
            if type(parent) is not int:
                break
            current = parent
        done.update(chain)


def validate_rich_tables(tables: dict[str, list[dict]], error: Callable[[str], None]) -> None:
    scenes = {row["id"]: row for row in tables.get("scenes", [])}
    meta_by_scene: dict[int, dict] = {}
    for row in tables.get("scene_documents", []):
        scene_id = row["scene_id"]
        if scene_id in meta_by_scene:
            error("同一场景的富格式记录重复")
        meta_by_scene[scene_id] = row

    sections_by_scene: dict[int, list[dict]] = defaultdict(list)
    sections_by_id: dict[int, dict] = {}
    _validate_lineage(tables.get("scene_sections", []), "origin_section_id", "区块", error)
    for row in tables.get("scene_sections", []):
        sections_by_scene[row["scene_id"]].append(row)
        sections_by_id[row["id"]] = row

    markers_by_scene: dict[int, list[dict]] = defaultdict(list)
    _validate_lineage(tables.get("scene_markers", []), "origin_marker_id", "标记", error)
    for row in tables.get("scene_markers", []):
        section_id = row.get("section_id")
        if section_id is not None:
            section = sections_by_id.get(section_id)
            if section is None or section["scene_id"] != row["scene_id"]:
                error("正文标记引用的区块缺失或不属于同一场景")
        try:
            UUID(row["logical_key"])
        except (ValueError, TypeError, AttributeError):
            error("正文标记身份不是合法 UUID")
        markers_by_scene[row["scene_id"]].append(row)

    documents: dict[int, SceneDocument] = {}
    for scene_id, scene in scenes.items():
        all_sections = sections_by_scene[scene_id]
        all_markers = markers_by_scene[scene_id]
        try:
            validate_storage_bounds(meta_by_scene.get(scene_id, {}).get("formatting", {}), all_sections, all_markers)
        except ValueError as exc:
            error(f"场景 #{scene_id} 的富文档无效：{exc}")

        active_sections, retired_keys = [], []
        by_id = {}
        for row in all_sections:
            try:
                section = DocumentSection.model_validate({
                    "key": row["logical_key"], "anchor": row["anchor"], "order": row["sort"],
                    "title": row["title"], "content": row.get("content", ""),
                    "formatting": row.get("formatting", {}), "include_in_ai": row.get("include_in_ai", False),
                })
            except ValidationError as exc:
                error(f"场景 #{scene_id} 的区块结构无效：{exc.errors()[0]['msg']}")
                continue
            by_id[row["id"]] = section
            if row.get("active", True):
                active_sections.append(section)
            else:
                retired_keys.append(section.key)

        markers = []
        marker_keys: set[UUID] = set()
        for row in all_markers:
            try:
                marker_key = UUID(row["logical_key"])
            except (ValueError, TypeError, AttributeError):
                continue  # 前面已经报告无效身份。
            if marker_key in marker_keys:
                error(f"场景 #{scene_id} 的标记身份重复")
            marker_keys.add(marker_key)
            for field in ("excerpt", "draft_excerpt"):
                value = row.get(field)
                if value is not None and (not isinstance(value, str) or len(value) > MAX_MARKER_EXCERPT):
                    error(f"场景 #{scene_id} 的标记摘录无效或超过上限")
            for field in ("start_offset", "end_offset", "last_start", "draft_last_start"):
                value = row.get(field)
                if value is not None and (type(value) is not int or not 0 <= value <= 2_147_483_647):
                    error(f"场景 #{scene_id} 的标记位置无效")
            draft = row.get("draft_excerpt")
            draft_start = row.get("draft_last_start")
            draft_truncated = row.get("draft_excerpt_truncated", False)
            if ((draft is None and (draft_start is not None or draft_truncated))
                    or (draft is not None and (draft == "" or draft_start is None))
                    or type(draft_truncated) is not bool):
                error(f"场景 #{scene_id} 的草稿标记摘录无效")
            if row["status"] == "deleted":
                continue
            section = by_id.get(row.get("section_id"))
            markers.append({
                "key": row["logical_key"], "section_key": section.key if section is not None else None,
                "status": row["status"], "color": row["color"], "start": row.get("start_offset"),
                "end": row.get("end_offset"), "last_start": row.get("last_start"), "excerpt": row.get("excerpt", ""),
                "excerpt_truncated": row.get("excerpt_truncated", False),
                "draft_excerpt": row.get("draft_excerpt"),
                "draft_excerpt_truncated": row.get("draft_excerpt_truncated", False),
                "draft_last_start": row.get("draft_last_start"),
            })
        try:
            documents[scene_id] = SceneDocument.model_validate({
                "content": scene["content"], "formatting": meta_by_scene.get(scene_id, {}).get("formatting", {}),
                "sections": active_sections, "retired_section_keys": retired_keys, "markers": markers,
            })
        except ValidationError as exc:
            error(f"场景 #{scene_id} 的富文档结构无效：{exc.errors()[0]['msg']}")

    by_chapter: dict[int, list[dict]] = defaultdict(list)
    for scene in scenes.values():
        if scene.get("active", True):
            by_chapter[scene["chapter_id"]].append(scene)
    for chapter_id, items in by_chapter.items():
        if any(item["id"] not in documents for item in items):
            continue  # 同景结构错误已报告，不能对不完整章节做推测。
        items.sort(key=lambda item: (item.get("sort", 0), item["id"]))
        try:
            validate_chapter_document_boundaries([
                (item.get("separator_before", ""), documents[item["id"]]) for item in items
            ])
        except ValueError as exc:
            error(f"章节 #{chapter_id} 的富文档结构无效：{exc}")
