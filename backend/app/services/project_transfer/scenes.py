"""v7 场景核心图的纯校验；不从投影猜边界、不修复有损备份。"""

from collections import defaultdict
from collections.abc import Callable

from pydantic import ValidationError

from app.schemas.planning import ScenePlanningRaw
from app.schemas.scene import SceneSnapshot


def validate_scene_tables(tables: dict[str, list[dict]], error: Callable[[str], None]) -> None:
    from app.services.chapter_service import count_words

    chapters = {row["id"]: row for row in tables.get("chapters", [])}
    scenes = {row["id"]: row for row in tables.get("scenes", [])}
    units = {row["id"]: row for row in tables.get("planning_units", [])}
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
    for version in tables.get("chapter_versions", []):
        snapshot = version.get("scene_snapshot")
        if snapshot is None:
            continue
        try:
            if type(snapshot.get("schema_version")) is not int:
                raise ValueError("快照版本不是整数")
            parsed = SceneSnapshot.model_validate(snapshot)
        except (ValidationError, TypeError, ValueError, AttributeError):
            error("章节版本的场景结构快照格式无效")
            continue
        projection = "".join(scene.separator_before + scene.content for scene in parsed.scenes)
        if projection != version.get("content", ""):
            error("章节历史正文与场景快照投影矛盾")
        for item in parsed.scenes:
            carrier = scenes.get(item.id)
            if carrier is None or carrier["chapter_id"] != version.get("chapter_id"):
                error("场景历史身份缺失或属于其他章节")
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
                # 合法后续改挂可以改变 scene_id / binding_epoch，但只能在同章。
                # 历史引用另一个章节的真实单元同样是坏核心图，不能等恢复时才发现。
                if source is None or source["chapter_id"] != version.get("chapter_id"):
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
