"""Agent 单 Scene 范围与 v6 结构签名；真实旧效果只在可证明安全时兼容。"""

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import Chapter, ChapterVersion, Project
from app.services import scene_service
from app.services.agent import effects
from app.services.agent.tools.base import ToolCtx
from app.services.agent.tools.write_text import TOOLS
from tests.test_scene_planning import scene_setup


async def _ctx(app, client, db, project_id):
    user = (await client.get("/api/v1/auth/me")).json()
    project = await db.get(Project, project_id)
    return ToolCtx(db, project, user["id"], app.state.task_engine.ctx)


def _text_effect(result):
    return effects.chapter_text_effect(
        **{
            key: result[key]
            for key in (
                "chapter_id",
                "before_version_id",
                "after_version_id",
                "before_hash",
                "after_hash",
            )
        }
    )


async def test_agent_v6_replaces_only_one_scene_and_undo_preserves_stable_structure(client, app):
    project, _, chapter, url, scenes = await scene_setup(client, content="前😀原文后", count=3)
    async with app.state.sessionmaker() as db:
        ctx = await _ctx(app, client, db, project["id"])
        before = await effects.snapshot(db, project["id"])
        args = {"chapter_id": chapter["id"], "original": "😀原文", "replacement": "新局部"}
        preview = await TOOLS[0].preview(ctx, args)
        assert preview["scene_id"] == scenes[0]["id"] and preview["start"] == 1
        result = await TOOLS[0].execute(ctx, args)
        assert "error" not in result, result
        after = await effects.snapshot(db, project["id"])
        changes = effects.diff(before, after) + [_text_effect(result)]
        assert all(effect["format_version"] == 6 for effect in changes)
        assert {"chapters", "scenes", "chapter_text"} <= {effect["table"] for effect in changes}
        await db.commit()
        stored = await db.get(Chapter, chapter["id"])
        revision = stored.content_revision
        assert stored.content == "前新局部后\n\n第二段\n\n第二段"
        await effects.undo(db, project["id"], changes)
        await db.commit()
        await db.refresh(stored)
        assert stored.content == "前😀原文后\n\n第二段\n\n第二段"
        assert stored.content_revision > revision
        assert await effects.snapshot(db, project["id"]) == before
        assert [row.id for row in await scene_service.list_active_scenes(db, chapter["id"])] == [
            scene["id"] for scene in scenes
        ]
    assert len((await client.get(f"{url}")).json()["items"]) == 3


async def test_agent_cross_scene_preview_and_execution_refuse_before_any_write(client, app):
    project, _, chapter, _, _ = await scene_setup(client, content="左景", count=2)
    async with app.state.sessionmaker() as db:
        ctx = await _ctx(app, client, db, project["id"])
        args = {"chapter_id": chapter["id"], "original": "左景\n\n第二段", "replacement": "不能吞并场景"}
        before = await effects.snapshot(db, project["id"])
        preview = await TOOLS[0].preview(ctx, args)
        assert "error" in preview
        executed = await TOOLS[0].execute(ctx, args)
        assert "error" in executed
        assert await effects.snapshot(db, project["id"]) == before


async def test_v6_text_undo_detects_metadata_change_even_when_complete_chapter_text_matches(client, app):
    project, _, chapter, _, scenes = await scene_setup(client, content="旧稿", count=2)
    async with app.state.sessionmaker() as db:
        ctx = await _ctx(app, client, db, project["id"])
        result = await TOOLS[0].execute(ctx, {"chapter_id": chapter["id"], "original": "旧稿", "replacement": "新稿"})
        assert "error" not in result, result
        effect = _text_effect(result)
        await db.commit()
        row = await db.get(Chapter, chapter["id"])
        scene = await scene_service.require_scene(db, row, scenes[1]["id"])
        scene.summary = "作者后加的重要摘要"
        await scene_service.touch_revision(db, row)
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        with pytest.raises(ConflictError, match="无法撤销"):
            await effects.undo(db, project["id"], [effect])
        await db.rollback()
        assert await effects.snapshot(db, project["id"]) == before


async def test_true_legacy_multi_scene_text_effect_refused_but_unrelated_v5_edit_still_works(client, app):
    project, _, chapter, _, _ = await scene_setup(client, content="旧稿", count=2)
    async with app.state.sessionmaker() as db:
        ctx = await _ctx(app, client, db, project["id"])
        result = await TOOLS[0].execute(ctx, {"chapter_id": chapter["id"], "original": "旧稿", "replacement": "新稿"})
        assert "error" not in result, result
        await db.commit()
        legacy = _text_effect(result)
        legacy["format_version"] = 5
        before = await effects.snapshot(db, project["id"])
        with pytest.raises(ConflictError) as error:
            await effects.undo(db, project["id"], [legacy])
        assert error.value.extra["code"] == "legacy_effect_requires_manual_correction"
        await db.rollback()
        assert await effects.snapshot(db, project["id"]) == before
        stored = await db.get(Project, project["id"])
        stored.title = "仅修改书名"
        after = await effects.snapshot(db, project["id"])
        unrelated = effects.diff(before, after)
        assert [effect["table"] for effect in unrelated] == ["projects"]
        for effect in unrelated:
            effect["format_version"] = 5
        await db.commit()
        await effects.undo(db, project["id"], unrelated)
        await db.commit()
        await db.refresh(stored)
        assert stored.title == project["title"]
        versions = list(await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter["id"])))
        assert any(version.source == "agent" for version in versions)


@pytest.mark.parametrize("later_data", ["none", "node", "scene", "local_character"])
async def test_true_v5_planning_bridges_only_defaults_and_preserves_later_author_data(client, app, later_data):
    import hashlib
    import json

    from app.models import Character
    from app.models.planning import PlanningUnit, PlanningUnitCharacter, Subplot, SubplotNode

    project, _, chapter, _, scenes = await scene_setup(client, count=2)
    async with app.state.sessionmaker() as db:
        unit = PlanningUnit(project_id=project["id"], chapter_id=chapter["id"])
        db.add(unit)
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        unit.last_source_label = "旧智能体更新的标签"
        after = await effects.snapshot(db, project["id"])
        changes = effects.diff(before, after)
        assert [effect["table"] for effect in changes] == ["planning_units"]
        # 真0038的签名没有场景本地角色表；不能用当前实现输出冒充旧资料。
        old_children = {
            table: {}
            for table in (
                "planning_unit_tags",
                "planning_unit_world",
                "planning_unit_organizations",
                "planning_unit_careers",
                "planning_unit_relationships",
                "subplot_nodes",
                "subplot_crossings",
                "subplot_crossing_participants",
            )
        }
        old_children["source_cast"] = []
        old_signature = hashlib.sha256(
            json.dumps(old_children, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        for effect in changes:
            effect["format_version"] = 5
            for side in ("before", "after"):
                row = effect[side]
                for name in (
                    "scene_id",
                    "source_level",
                    "pov_mode",
                    "tags_mode",
                    "character_mode",
                    "world_mode",
                    "organization_mode",
                    "career_mode",
                    "relationship_mode",
                ):
                    row.pop(name)
                row["_planning_content_hash"] = old_signature
        await db.commit()
        if later_data != "none":
            if later_data == "node":
                subplot = Subplot(project_id=project["id"], name="作者后来支线")
                db.add(subplot)
                await db.flush()
                db.add(SubplotNode(unit_id=unit.id, subplot_id=subplot.id, content="不可静默抹掉的新推进"))
            else:
                unit.chapter_id = None
                unit.scene_id = scenes[0]["id"]
                unit.source_level = "scene"
                unit.pov_mode = "override"
                if later_data == "local_character":
                    character = Character(project_id=project["id"], name="后来本地角色")
                    db.add(character)
                    await db.flush()
                    db.add(PlanningUnitCharacter(unit_id=unit.id, character_id=character.id))
            await db.commit()
            with pytest.raises(ConflictError, match="无法撤销"):
                await effects.undo(db, project["id"], changes)
            await db.rollback()
            await db.refresh(unit)
            assert unit.last_source_label == "旧智能体更新的标签"
            if later_data == "node":
                assert (
                    await db.scalar(select(SubplotNode.content).where(SubplotNode.unit_id == unit.id))
                    == "不可静默抹掉的新推进"
                )
            else:
                assert unit.scene_id == scenes[0]["id"] and unit.pov_mode == "override"
        else:
            await effects.undo(db, project["id"], changes)
            await db.commit()
            await db.refresh(unit)
            assert unit.last_source_label == ""
            assert unit.source_level == "chapter" and unit.scene_id is None and unit.pov_mode == "inherit"


@pytest.mark.parametrize(
    ("kind", "co_remove_history"),
    [
        ("character", False),
        ("world", False),
        ("organization", False),
        ("career", False),
        ("relationship", False),
        ("character", True),
    ],
)
async def test_agent_entity_undo_respects_retained_scene_history_and_same_batch_removal(
    client,
    app,
    kind,
    co_remove_history,
):
    from app.models import RelationshipType, SettingTimeline
    from app.services.agent.tools import get_tool
    from tests.test_project_transfer import _backup, _import
    from tests.test_scene_planning import planning_batch

    project, base, chapter, url, scenes = await scene_setup(client, count=1)
    arguments = {"name": f"Agent创建的{kind}"}
    tool_name = f"create_{kind}"
    if kind == "world":
        arguments = {"title": "Agent创建的世界资料", "content": "世界资料"}
        tool_name = "create_world_setting"
    elif kind == "career":
        arguments["stages"] = [{"name": "初阶"}]
    elif kind == "relationship":
        alice = (await client.post(f"{base}/characters", json={"name": "关系甲"})).json()
        bob = (await client.post(f"{base}/characters", json={"name": "关系乙"})).json()
        async with app.state.sessionmaker() as db:
            type_id = await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)).limit(1))
        arguments = {"a_id": alice["id"], "b_id": bob["id"], "type_id": type_id}
    async with app.state.sessionmaker() as db:
        ctx = await _ctx(app, client, db, project["id"])
        before = await effects.snapshot(db, project["id"])
        created = await get_tool(tool_name).execute(ctx, arguments)
        assert "error" not in created, created
        after = await effects.snapshot(db, project["id"])
        changes = effects.diff(before, after)
        identity = created["id"]
        if kind == "relationship":
            timeline_id = await db.scalar(
                select(SettingTimeline.id).where(
                    SettingTimeline.project_id == project["id"],
                    SettingTimeline.subject_kind == "relationship",
                    SettingTimeline.character_id == alice["id"],
                    SettingTimeline.related_character_id == bob["id"],
                )
            )
        await db.commit()
    operation = {
        "kind": kind,
        "op": "replace",
        "timeline_ids" if kind == "relationship" else "ids": [timeline_id if kind == "relationship" else identity],
    }
    saved = await planning_batch(client, base, url, [{"kind": "scene", "id": scenes[0]["id"]}], references=[operation])
    assert saved.status_code == 200, saved.text
    cleared = await planning_batch(
        client, base, url, [{"kind": "scene", "id": scenes[0]["id"]}], references=[{"kind": kind, "op": "inherit"}]
    )
    assert cleared.status_code == 200, cleared.text
    # 当前raw已没有typed FK；保留版本中的JSON才是唯一依赖，不能用live关联来掩盖此RED。
    current = (await client.get(f"{url}/{scenes[0]['id']}")).json()
    assert current["planning"]["raw"]["references"] == []
    target = (
        "setting_timelines"
        if kind == "relationship"
        else {
            "character": "characters",
            "world": "world_settings",
            "organization": "organizations",
            "career": "careers",
        }[kind],
        timeline_id if kind == "relationship" else identity,
    )
    async with app.state.sessionmaker() as db:
        if co_remove_history:
            # 同一effect集明确包含历史行插入的撤销时，引用与目标一起删除是合法赢家。
            rule = effects._BY_TABLE["chapter_versions"]
            for row in await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter["id"])):
                if target not in effects._setting_references(
                    "chapter_versions", {"scene_snapshot": row.scene_snapshot}
                ):
                    continue
                after_row = await effects._current(db, rule, {"id": row.id}, project["id"])
                changes.append(
                    {
                        "table": "chapter_versions",
                        "pk": {"id": row.id},
                        "before": None,
                        "after": after_row,
                        "format_version": 6,
                    }
                )
            assert any(effect["table"] == "chapter_versions" for effect in changes)
            await effects.undo(db, project["id"], changes)
            await db.commit()
            assert (
                await db.scalar(
                    select(effects._BY_TABLE[target[0]].model.id).where(
                        effects._BY_TABLE[target[0]].model.id == target[1],
                    )
                )
                is None
            )
        else:
            with pytest.raises(ConflictError, match="无法撤销") as error:
                await effects.undo(db, project["id"], changes)
            assert error.value.extra["code"] == "undo_conflict"
            await db.rollback()
            assert (
                await db.scalar(
                    select(effects._BY_TABLE[target[0]].model.id).where(
                        effects._BY_TABLE[target[0]].model.id == target[1],
                    )
                )
                == target[1]
            )
    # 阻止不安全撤销后，以及合法同批删除后，整个实际备份都必须仍可导入。
    backup = await _backup(client, project["id"], versions="1")
    imported = await _import(client, backup)
    assert imported["project"]["id"] != project["id"]
