"""场景核心图与移动证据的纯校验。"""

from collections import defaultdict
from collections.abc import Callable
from uuid import UUID

from pydantic import ValidationError

from app.schemas.planning import ScenePlanningRaw
from app.schemas.rich_document import SceneDocument
from app.schemas.scene import SceneSnapshotV2, parse_scene_snapshot


def _validate_moves(
    tables: dict[str, list[dict]], scenes: dict[int, dict], units: dict[int, dict], error: Callable[[str], None],
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    scene_positions: set[tuple[int, int]] = set()
    unit_positions: set[tuple[int, int]] = set()
    last_positions: dict[int, int | None] = {}
    origins: set[int] = set()
    last_seqs: dict[int, int] = {}
    max_seqs: dict[int, int] = {}
    for row in tables.get("chapter_versions", []):
        cid = row["chapter_id"]
        max_seqs[cid] = max(max_seqs.get(cid, 0), row["seq"])
    for row in sorted(tables.get("scene_moves", []), key=lambda item: item["id"]):
        scene_id = row["scene_id"]
        source, target = row.get("from_chapter_id"), row.get("to_chapter_id")
        if (row["first_move"] != (scene_id not in last_positions)
                or scene_id in last_positions and last_positions[scene_id] != source
                or source is not None and source == target):
            error("场景移动记录的首条归属或移动顺序不一致")
        last_positions[scene_id] = target
        if row["first_move"] and source is not None:
            origins.add(source)
        unit_id = row.get("planning_unit_id")
        if unit_id is not None and units[unit_id].get("source_level", "chapter") != "scene":
            error("场景移动记录的规划来源不是场景")
        for cid, seq in ((source, row.get("from_version_seq")), (target, row.get("to_version_seq"))):
            if seq is not None and (
                seq <= 0 or cid is not None and not last_seqs.get(cid, 0) < seq <= max_seqs.get(cid, 0)
            ):
                error("场景移动的章节版本边界不合法")
            if cid is None:
                continue
            if seq is not None:
                last_seqs[cid] = seq
            scene_positions.add((scene_id, cid))
            if unit_id is not None:
                unit_positions.add((unit_id, cid))
    if any(scenes[sid]["chapter_id"] != target for sid, target in last_positions.items()):
        error("场景当前归属与最后一次移动的目标章节不一致")
    origins.update(row["chapter_id"] for sid, row in scenes.items() if sid not in last_positions)
    if any(row["id"] not in origins for row in tables.get("chapters", [])):
        error("章节缺少原生场景身份")
    return scene_positions, unit_positions


def validate_scene_tables(tables: dict[str, list[dict]], error: Callable[[str], None]) -> set[tuple[int, int]]:
    from app.services.chapter_service import count_words

    chapters = {row["id"]: row for row in tables.get("chapters", [])}
    scenes = {row["id"]: row for row in tables.get("scenes", [])}
    units = {row["id"]: row for row in tables.get("planning_units", [])}
    scene_positions, unit_positions = _validate_moves(tables, scenes, units, error)
    by_chapter = defaultdict(list)
    for scene in scenes.values():
        if scene.get("chapter_id") not in chapters:
            error("场景引用了不存在的所属章节")
        if type(scene.get("sort", 0)) is not int or scene.get("sort", 0) < 0:
            error("场景顺序必须为非负整数")
        if scene.get("word_count", 0) != count_words(scene.get("content", "")):
            error("场景派生字数与正文不一致")
        if scene.get("active", True):
            by_chapter[scene["chapter_id"]].append(scene)
    for chapter_id, chapter in chapters.items():
        active = sorted(by_chapter[chapter_id], key=lambda row: (row.get("sort", 0), row["id"]))
        ranks = [row.get("sort", 0) for row in active]
        if not active or len(set(ranks)) != len(ranks):
            error(f"章节 #{chapter_id} 缺少活动场景或存在重复顺序")
        projection = "".join(row.get("separator_before", "") + row.get("content", "") for row in active)
        if projection != chapter.get("content", ""):
            error(f"章节 #{chapter_id} 正文与活动场景投影矛盾")
    targets = {"character": "characters", "world": "world_settings", "organization": "organizations",
               "career": "careers", "relationship": "setting_timelines"}
    ids = {name: {row["id"] for row in rows if "id" in row} for name, rows in tables.items()}
    relationships = {row["id"]: row for row in tables.get("setting_timelines", [])}
    rich_identities: dict[str, dict[int, set[UUID]]] = {}
    for table in ("scene_sections", "scene_markers"):
        per_scene: dict[int, set[UUID]] = defaultdict(set)
        for row in tables.get(table, []):
            try:
                per_scene[row["scene_id"]].add(UUID(row["logical_key"]))
            except (ValueError, TypeError, AttributeError):
                error("富文档持久身份不是合法 UUID")
        rich_identities[table] = per_scene
    for version in tables.get("chapter_versions", []):
        snapshot = version.get("scene_snapshot")
        if snapshot is None:
            continue
        try:
            if type(snapshot.get("schema_version")) is not int:
                raise ValueError("快照版本不是整数")
            parsed = parse_scene_snapshot(snapshot)
        except (ValidationError, TypeError, ValueError, AttributeError):
            error("章节版本的场景结构快照格式无效")
            continue
        projection = "".join(scene.separator_before + scene.content for scene in parsed.scenes)
        if projection != version.get("content", ""):
            error("章节历史正文与场景快照投影矛盾")
        for item in parsed.scenes:
            carrier = scenes.get(item.id)
            chapter_id = version["chapter_id"]
            if carrier is None or (
                carrier["chapter_id"] != chapter_id and (item.id, chapter_id) not in scene_positions
            ):
                error("场景历史身份缺失或属于其他章节")
            if isinstance(parsed, SceneSnapshotV2):
                document = SceneDocument.model_validate({"content": item.content, **item.document})
                section_keys = {section.key for section in document.sections} | set(document.retired_section_keys)
                if not section_keys <= rich_identities["scene_sections"].get(item.id, set()):
                    error("场景历史的区块身份缺失或属于其他场景")
                marker_keys = {marker.key for marker in document.markers}
                if not marker_keys <= rich_identities["scene_markers"].get(item.id, set()):
                    error("场景历史的标记身份缺失或属于其他场景")
            planning = item.planning
            if planning is None:
                continue
            try:
                if set(planning) != {"unit_id", "binding_epoch", "raw"}:
                    raise ValueError("规划快照键无效")
                if type(planning["unit_id"]) is not int or type(planning["binding_epoch"]) is not int:
                    raise ValueError("规划快照身份无效")
                if planning["binding_epoch"] < 0:
                    raise ValueError("规划快照代次无效")
                raw_value = planning["raw"]
                if set(raw_value) != {"modes", "pov_character_id", "tag_ids", "references"} or set(
                    raw_value["modes"]
                ) != {"pov", "tags", "character", "world", "organization", "career", "relationship"}:
                    raise ValueError("规划快照缺少原始覆盖意图")
                raw = ScenePlanningRaw.model_validate(raw_value)
            except (ValidationError, KeyError, TypeError, ValueError):
                error("场景历史中的本地规划格式无效")
                continue
            unit = units.get(planning["unit_id"])
            if unit is None or unit.get("source_level", "chapter") != "scene":
                error("场景历史引用的真实规划身份缺失")
            else:
                source = scenes.get(unit.get("scene_id"))
                # 改挂可以改变真实场景，显式移动记录可证明旧章中的规划身份。
                if (source is None or source["chapter_id"] != chapter_id) and (
                    (unit["id"], chapter_id) not in unit_positions
                ):
                    error("场景历史规划身份不属于该章节")
            if raw.pov_character_id is not None and raw.pov_character_id not in ids.get("characters", set()):
                error("场景历史 POV 引用缺失")
            if not set(raw.tag_ids) <= ids.get("planning_tags", set()):
                error("场景历史标签引用缺失")
            for ref in raw.references:
                identity = ref.timeline_id if ref.kind == "relationship" else ref.id
                if identity not in ids.get(targets[ref.kind], set()):
                    error("场景历史设定引用缺失")
                elif ref.kind == "relationship" and relationships[identity]["subject_kind"] != "relationship":
                    error("场景历史关系引用不是关系时间线")
    return scene_positions
