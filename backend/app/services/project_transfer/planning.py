"""规划备份的跨表不变量；预览和实际恢复共享，拒绝半有效的关系／交汇。"""

from collections import Counter
from collections.abc import Callable


def validate_planning_tables(tables: dict[str, list[dict]], error: Callable[[str], None]) -> None:
    units = {row["id"]: row for row in tables.get("planning_units", [])}
    chapters = {row["id"]: row for row in tables.get("chapters", [])}
    plans = {row["id"]: row for row in tables.get("chapter_plans", [])}
    timelines = {row["id"]: row for row in tables.get("setting_timelines", [])}
    source_ids = {"chapter_id": set(chapters), "plan_id": set(plans),
                  "pov_character_id": {row["id"] for row in tables.get("characters", [])}}
    occupied_chapters: set[int] = set()
    occupied_plans: set[int] = set()
    retired = {uid for uid, row in units.items() if row.get("retired", False)}
    for uid, unit in units.items():
        chapter_id, plan_id = unit.get("chapter_id"), unit.get("plan_id")
        if type(unit.get("binding_epoch", 0)) is not int or unit.get("binding_epoch", 0) < 0:
            error(f"规划单元 #{uid} 的绑定代次无效")
        if chapter_id is not None:
            if chapter_id in occupied_chapters:
                error(f"章节 #{chapter_id} 被多个规划单元占用")
            occupied_chapters.add(chapter_id)
        if plan_id is not None:
            if plan_id in occupied_plans:
                error(f"细纲 #{plan_id} 被多个规划单元占用")
            occupied_plans.add(plan_id)
            plan = plans.get(plan_id)
            if plan is not None and plan.get("chapter_id") != chapter_id:
                error(f"规划单元 #{uid} 的细纲／章节双向绑定不一致")
        if chapter_id is not None and plan_id is not None:
            chapter = chapters.get(chapter_id)
            if chapter is not None and chapter.get("plan_id") != plan_id:
                error(f"规划单元 #{uid} 的章节未绑定指定细纲")
        if uid in retired and any((chapter_id is not None, plan_id is not None,
                                   unit.get("pov_character_id") is not None, unit.get("detached_character_ids"))):
            error(f"退役规划单元 #{uid} 仍携带来源或有效资料")
        if (chapter_id is not None or plan_id is not None) and unit.get("detached_character_ids"):
            error(f"规划单元 #{uid} 已有来源，不能同时保留孤立角色副本")

    join_targets = {
        "planning_unit_tags": ("tag_id", "planning_tags"),
        "planning_unit_world": ("world_id", "world_settings"),
        "planning_unit_organizations": ("organization_id", "organizations"),
        "planning_unit_careers": ("career_id", "careers"),
        "planning_unit_relationships": ("timeline_id", "setting_timelines"),
    }
    for table, (column, target) in join_targets.items():
        keys = set()
        target_ids = {row["id"] for row in tables.get(target, [])}
        for row in tables.get(table, []):
            key = (row["unit_id"], row[column])
            if key in keys:
                error(f"{table} 存在重复规划关联")
            keys.add(key)
            if row["unit_id"] not in units or row[column] not in target_ids:
                error(f"{table} 引用了当前作品之外或不存在的资料")
            if row["unit_id"] in retired:
                error("退役规划单元仍有设定或标签关联")
            if table == "planning_unit_relationships":
                timeline = timelines.get(row[column])
                if timeline is not None and timeline.get("subject_kind") != "relationship":
                    error("规划人物关系引用的时间线不是关系主体")

    for uid, unit in units.items():
        for column, valid_ids in source_ids.items():
            value = unit.get(column)
            if value is not None and value not in valid_ids:
                error(f"规划单元 #{uid} 的 {column} 引用不属于当前作品")

    nodes = set()
    subplot_ids = {row["id"] for row in tables.get("subplots", [])}
    for row in tables.get("subplot_nodes", []):
        key = (row["unit_id"], row["subplot_id"])
        if key in nodes:
            error("同一规划单元的同一支线存在多个普通节点")
        nodes.add(key)
        if row["unit_id"] not in units or row["subplot_id"] not in subplot_ids or row["unit_id"] in retired:
            error("支线节点引用了失效或其他作品的规划单元／支线")
    crossings = {row["id"]: row for row in tables.get("subplot_crossings", [])}
    for row in crossings.values():
        if row["unit_id"] not in units or row["unit_id"] in retired:
            error("交汇事件引用了失效或其他作品的规划单元")
    participation: Counter[int] = Counter()
    pairs = set()
    for row in tables.get("subplot_crossing_participants", []):
        pair = (row["crossing_id"], row["subplot_id"])
        if pair in pairs:
            error("交汇事件有重复参与支线")
        pairs.add(pair)
        if row["crossing_id"] not in crossings or row["subplot_id"] not in subplot_ids:
            error("交汇事件参与关系引用了其他作品或不存在的支线／事件")
        participation[row["crossing_id"]] += 1
    for crossing_id in crossings:
        if not 2 <= participation[crossing_id] <= 20:
            error(f"交汇事件 #{crossing_id} 必须有 2–20 条互不重复的参与支线")

    for table, fields in {
        "planning_tags": (("name", 50, True),),
        "subplots": (("name", 100, True), ("description", 5000, False)),
        "subplot_nodes": (("content", 2000, True),),
        "subplot_crossings": (("title", 100, True), ("description", 2000, True)),
    }.items():
        for row in tables.get(table, []):
            for field, limit, required in fields:
                value = row.get(field, "")
                if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
                    error(f"{table} 的 {field} 必须为合法文本（上限 {limit} 字）")
    names = [row["name"].strip() for row in tables.get("planning_tags", [])]
    if len(names) != len(set(names)):
        error("作品内标签名称重复")
