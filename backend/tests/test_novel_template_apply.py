"""模板快照应用在调用者事务中建立新身份和 confirmed 基线。"""

import io
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select

from app.core.errors import BadRequestError
from app.models import (
    Career,
    CareerStage,
    Chapter,
    ChapterPlan,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    Relationship,
    RelationshipType,
    User,
    WorldSetting,
    WritingStyle,
)
from app.models.progressions import SettingProgression, SettingTextField, SettingTimeline
from app.models.scenes import Scene
from app.models.setting_categories import SettingCategory, SettingCategoryRule
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption
from app.models.setting_images import SettingImageResource, SettingThumbnailBinding
from app.schemas.novel_template_snapshot import NovelTemplateSnapshot, TemplateSelection
from app.services.novel_template_apply import apply_template
from app.services.novel_template_snapshot import capture
from app.services.setting_image_codec import normalize_image
from tests.helpers import create_project, purge_trashed_project, register

AI = {"ai_mode": "never", "mention_enabled": False, "mention_case_sensitive": True,
      "mention_exclusions": [], "ai_hidden_builtin_fields": []}


def _character(ref, name):
    return {"ref": ref, "name": name, "aliases": [], "role_type": "", "age": "28", "gender": "",
            "appearance": "", "personality": "", "background": "", "profile": "当前档案",
            "relations_text": "", "current_state": "已晋升", "life_status": "alive",
            "always_in_context": False, "sort": 0, "ai_settings": AI}


def _snapshot():
    character = _character("character.a", "同名")
    character.update({"image_ref": "character.a", "setting_tags": ["同盟"],
                      "fields": [{"definition_ref": "field.home", "value": {"entry_ref": "world.place"}},
                                 {"definition_ref": "field.side", "value": {"option_ref": "option.old"}},
                                 {"definition_ref": "field.text", "value": "换行\n文本"},
                                 {"definition_ref": "field.line", "value": "单行"}],
                      "custom_text_fields": [{"name": "私记", "value": "当前值", "ai_hidden": True}],
                      "ai_link_refs": ["world.place"]})
    all_types = ["character", "world.place", "organization", "career"]
    common_fields = [
        {"definition_ref": "field.home", "value": {"entry_ref": "world.place"}},
        {"definition_ref": "field.side", "value": {"option_ref": "option.old"}},
        {"definition_ref": "field.text", "value": "换行\n文本"},
        {"definition_ref": "field.line", "value": "单行"},
    ]
    return NovelTemplateSnapshot.model_validate({
        "format": "ainovel.novel-template", "version": 1,
        "config": {"genre": "奇幻", "premise": "新的开始", "synopsis": "", "theme": "",
                   "tags": [], "selling_points": "", "audience": "", "target_words": 1000,
                   "auto_analyze": False, "style": None},
        "field_definitions": [
            {"ref": "field.home", "label": "故乡", "field_type": "reference",
             "applicable_types": all_types, "reference_types": ["character", "world.place"]},
            {"ref": "field.side", "label": "阵营", "field_type": "dropdown",
             "applicable_types": all_types,
             "options": [{"ref": "option.old", "label": "旧阵营", "archived": True}]},
            {"ref": "field.text", "label": "记录", "field_type": "text", "applicable_types": all_types},
            {"ref": "field.line", "label": "口令", "field_type": "line", "applicable_types": all_types},
        ],
        "categories": [{"ref": "category.side", "name": "同盟", "applicable_types": ["character"],
                        "match_mode": "all", "sort": 1,
                        "rules": [{"kind": "dropdown", "sort": 0, "tag": None,
                                   "definition_ref": "field.side", "option_ref": "option.old"}]}],
        "characters": [character, _character("character.b", "同名")],
        "world_settings": [{"ref": "world.place", "title": "云港", "aliases": [], "category": "place",
                            "content": "当前世界观", "sort": 0, "ai_settings": AI,
                            "fields": [{"definition_ref": "field.home", "value": {"entry_ref": "character.a"}},
                                       *common_fields[1:]]}],
        "organizations": [{"ref": "organization.one", "name": "青云宗", "org_type": "宗门",
                           "purpose": "", "location": "", "power_level": 8, "description": "",
                           "parent_ref": None, "sort": 1, "ai_settings": AI, "fields": common_fields},
                          {"ref": "organization.two", "name": "青云别院", "org_type": "宗门",
                           "purpose": "", "location": "", "power_level": 1, "description": "",
                           "parent_ref": "organization.one", "sort": 2, "ai_settings": AI}],
        "careers": [{"ref": "career.one", "name": "剑修", "kind": "main", "description": "",
                     "requirements": "", "abilities": "", "rules": "", "sort": 1, "ai_settings": AI,
                     "fields": common_fields,
                     "stages": [{"ref": "stage.one", "name": "炼气", "description": "", "position": 1},
                                {"ref": "stage.two", "name": "筑基", "description": "", "position": 2}]}],
        "relationship_types": [{"ref": "type.parent", "origin": "project", "category": "family",
                                "role_a": "父亲", "role_b": "子女", "sort": 2}],
        "relationships": [{"type_ref": "type.parent", "a_ref": "character.a", "b_ref": "character.b",
                           "intimacy": 20, "status": "normal", "description": "亲情"}],
        "memberships": [{"organization_ref": "organization.one", "character_ref": "character.a",
                         "position": "弟子", "status": "active", "note": ""}],
        "assignments": [{"character_ref": "character.a", "career_ref": "career.one", "stage_ref": "stage.two",
                         "role": "main", "note": "刚刚晋升"}],
        "outlines": [{"ref": "outline.one", "volume_no": 1, "volume_title": "第一卷", "title": "开端",
                      "summary": "", "sort": 0, "plans": [{"title": "抵达", "summary": "",
                      "key_events": ["相遇"], "character_refs": ["character.b"], "emotion": "",
                      "goal": "", "est_words": 1000, "seq": 1}]}],
    })


def _image():
    data = io.BytesIO()
    Image.new("RGB", (20, 10), "red").save(data, format="PNG")
    return normalize_image(data.getvalue(), "image/png")


def _style_values():
    return {
        "name": "寒光", "description": "旧版简介", "pov": "第三人称", "rhythm": "紧凑",
        "dialogue": "克制", "description_density": "适中", "tone": "沉静", "diction": "精简",
        "taboos": "无旁白说教", "sample": "剑光一闪。", "extra": "保留节奏", "sort": 19,
    }


def _style_snapshot():
    data = _snapshot().model_dump(mode="json")
    for key in data.keys() - {"format", "version", "config"}:
        data[key] = []
    data["config"]["style"] = _style_values()
    return NovelTemplateSnapshot.model_validate(data)


async def test_apply_builds_confirmed_independent_graph_and_empty_body(app, client):
    user = await register(client)
    snapshot = _snapshot()
    image = _image()
    async with app.state.sessionmaker() as db:
        project = Project(user_id=user["id"], title="新书")
        db.add(project)
        await db.flush()
        await apply_template(db, project, snapshot, {"character.a": image})
        await db.commit()
        project_id = project.id

    async with app.state.sessionmaker() as db:
        project = await db.get(Project, project_id)
        assert (project.title, project.genre, project.premise, project.auto_analyze) == (
            "新书", "奇幻", "新的开始", False)
        assert await db.scalar(select(func.count()).select_from(Chapter).where(Chapter.project_id == project_id)) == 0
        assert await db.scalar(select(func.count()).select_from(Scene)) == 0
        characters = list(await db.scalars(
            select(Character).where(Character.project_id == project_id).order_by(Character.id)))
        assert len(characters) == 2 and characters[0].id != characters[1].id
        assert characters[0].current_state == "已晋升" and characters[0].ai_mode == "never"
        timelines = list(await db.scalars(select(SettingTimeline).where(SettingTimeline.project_id == project_id)))
        assert all(item.baseline_status == "confirmed" for item in timelines)
        assert await db.scalar(select(func.count()).select_from(SettingProgression)) == 0
        world = await db.scalar(select(WorldSetting).where(WorldSetting.project_id == project_id))
        assert world.title == "云港" and world.content == "当前世界观"
        fields = list(await db.scalars(select(SettingTextField)))
        assert any(field.label == "私记" and field.base_value == "当前值" and field.ai_hidden for field in fields)
        assert len(list(await db.scalars(select(SettingFieldDefinition)))) == 4
        assert (await db.scalar(select(SettingFieldOption))).archived is True
        category = await db.scalar(select(SettingCategory))
        assert category.name == "同盟" and (await db.scalar(select(SettingCategoryRule))).option_id is not None
        org = await db.scalar(select(Organization).where(Organization.name == "青云宗"))
        child = await db.scalar(select(Organization).where(Organization.name == "青云别院"))
        member = await db.scalar(select(OrganizationMember))
        career = await db.scalar(select(Career))
        stage = await db.scalar(select(CareerStage).where(CareerStage.name == "筑基"))
        assignment = await db.scalar(select(CharacterCareer))
        assert (org.name, member.character_id, career.name, assignment.stage_id) == (
            "青云宗", characters[0].id, "剑修", stage.id)
        assert child.parent_id == org.id and stage.position == 2
        relation = await db.scalar(select(Relationship))
        relation_type = await db.get(RelationshipType, relation.type_id)
        assert (relation.a_id, relation.b_id, relation_type.role_a, relation_type.role_b) == (
            characters[0].id, characters[1].id, "父亲", "子女")
        outline = await db.scalar(select(Outline))
        plan = await db.scalar(select(ChapterPlan))
        assert (outline.chapter_no, plan.character_ids, plan.chapter_id) == (1, [characters[1].id], None)
        binding = await db.scalar(select(SettingThumbnailBinding))
        resource = await db.get(SettingImageResource, binding.image_resource_id)
        assert resource.data == image.data and resource.project_id == project_id
        entry_ids = {"character": characters[0].id, "world": world.id, "organization": org.id, "career": career.id}
    for kind, entry_id in entry_ids.items():
        response = await client.get(f"/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields")
        assert response.status_code == 200, response.text
        slots = {item["label"]: item for item in response.json()["fields"]}
        assert slots["记录"]["value"] == "换行\n文本" and slots["口令"]["value"] == "单行"
        target = "character" if kind == "world" else "world"
        assert slots["故乡"]["value"] == {"kind": target, "id": entry_ids[target]}
        assert slots["阵营"]["options"][0]["label"] == "旧阵营"
        assert slots["阵营"]["options"][0]["archived"] is True


async def test_apply_failure_at_image_stage_rolls_back_entire_new_project(app, client, monkeypatch):
    from app.services import setting_image_service

    user = await register(client)
    image = _image()
    original = setting_image_service.set_book_image_without_commit

    async def fail_after_image(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("注入图片阶段故障")

    monkeypatch.setattr(setting_image_service, "set_book_image_without_commit", fail_after_image)
    payload = _snapshot().model_dump(mode="json")
    payload["config"]["style"] = _style_values()
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError, match="图片阶段"):
            async with db.begin():
                project = Project(user_id=user["id"], title="失败不留半书")
                db.add(project)
                await db.flush()
                await apply_template(db, project, NovelTemplateSnapshot.model_validate(payload), {"character.a": image})
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Project)) == 0
        for model in (Character, WorldSetting, Organization, Career, SettingImageResource, WritingStyle):
            stmt = select(func.count()).select_from(model)
            if model is WritingStyle:
                stmt = stmt.where(WritingStyle.user_id == user["id"])
            assert await db.scalar(stmt) == 0


async def test_config_only_preserves_definitions_and_categories_without_entries(app, client):
    user = await register(client)
    payload = _snapshot().model_dump(mode="json")
    for key in payload.keys() - {"format", "version", "config", "field_definitions", "categories"}:
        payload[key] = []
    async with app.state.sessionmaker() as db:
        project = Project(user_id=user["id"], title="只有配置")
        db.add(project)
        await db.flush()
        await apply_template(db, project, NovelTemplateSnapshot.model_validate(payload), {})
        await db.commit()
        assert project.genre == "奇幻"
        assert await db.scalar(select(func.count()).select_from(SettingFieldDefinition)) == 4
        assert await db.scalar(select(func.count()).select_from(SettingCategory)) == 1
        for model in (Character, WorldSetting, Organization, Career, Outline, Chapter, SettingTimeline):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_partial_outlines_keep_order_with_new_consecutive_chapters_and_plans(app, client):
    user = await register(client)
    payload = _snapshot().model_dump(mode="json")
    first = payload["outlines"][0]
    first.update({"volume_no": 2, "volume_title": "同名卷", "sort": 3})
    first["plans"][0]["seq"] = 4
    first["plans"].append({**first["plans"][0], "title": "离开", "seq": 9})
    payload["outlines"].extend([
        {"ref": "outline.two", "volume_no": 2, "volume_title": "同名卷", "title": "远行",
         "summary": "同一卷的后续大纲", "sort": 8, "plans": []},
        {"ref": "outline.three", "volume_no": 5, "volume_title": "同名卷", "title": "重逢",
         "summary": "另一卷", "sort": 20,
         "plans": [{**first["plans"][0], "title": "山中", "seq": 5},
                   {**first["plans"][0], "title": "归来", "seq": 12}]},
        {"ref": "outline.four", "volume_no": 5, "volume_title": "同名卷", "title": "终章",
         "summary": "另一卷的后续大纲", "sort": 40, "plans": []},
    ])
    async with app.state.sessionmaker() as db:
        project = Project(user_id=user["id"], title="部分大纲")
        db.add(project)
        await db.flush()
        await apply_template(db, project, NovelTemplateSnapshot.model_validate(payload), {"character.a": _image()})
        outlines = list(await db.scalars(select(Outline).where(Outline.project_id == project.id)
                                        .order_by(Outline.chapter_no)))
        assert [(item.chapter_no, item.volume_no, item.volume_title, item.sort, item.title) for item in outlines] == [
            (1, 1, "同名卷", 1, "开端"), (2, 1, "同名卷", 2, "远行"),
            (3, 2, "同名卷", 3, "重逢"), (4, 2, "同名卷", 4, "终章"),
        ]
        plans = list(await db.scalars(select(ChapterPlan).where(ChapterPlan.outline_id == outlines[0].id)
                                     .order_by(ChapterPlan.seq)))
        assert [(item.seq, item.title, item.chapter_id) for item in plans] == [
            (1, "抵达", None), (2, "离开", None),
        ]
        second_volume_plans = list(await db.scalars(select(ChapterPlan).where(
            ChapterPlan.outline_id == outlines[2].id).order_by(ChapterPlan.seq)))
        assert [(item.seq, item.title, item.chapter_id) for item in second_volume_plans] == [
            (1, "山中", None), (2, "归来", None),
        ]
        assert await db.scalar(select(func.count()).select_from(Chapter).where(Chapter.project_id == project.id)) == 0
        assert await db.scalar(select(func.count()).select_from(Scene)) == 0


async def test_partial_outline_volumes_round_trip_through_template_api(app, client):
    await register(client)
    source = await create_project(client, title="包含五卷的来源")
    base = f"/api/v1/projects/{source['id']}"
    chapter = await client.post(f"{base}/chapters", json={"title": "来源章节", "content": "只属于原书的正文"})
    assert chapter.status_code == 201, chapter.text
    async with app.state.sessionmaker.begin() as db:
        source_rows = []
        for number, volume in enumerate((1, 2, 2, 5, 5), start=1):
            outline = Outline(project_id=source["id"], volume_no=volume, volume_title="同名卷",
                              chapter_no=number + 5, title=f"来源章纲 {number}", sort=number * 10)
            db.add(outline)
            await db.flush()
            source_rows.append(outline.id)
            if number in (2, 4):
                for seq in (4, 9):
                    db.add(ChapterPlan(project_id=source["id"], outline_id=outline.id, seq=seq,
                                       title=f"细纲 {number}-{seq}"))

    selection = {"include_config": False, "outline_ids": list(reversed(source_rows[1:]))}
    preview = await client.post(f"{base}/novel-template-preview", json=selection)
    assert preview.status_code == 200, preview.text
    template = await client.post("/api/v1/novel-templates", json={
        "name": "部分卷模板", "source_project_id": source["id"], "selection": selection,
        "source_stamp": preview.json()["source_stamp"],
    })
    assert template.status_code == 201, template.text
    created = await client.post(f"/api/v1/novel-templates/{template.json()['id']}/projects", json={
        "expected_revision": template.json()["revision"], "intent_key": str(uuid4()), "title": "模板新书",
    })
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["chapter_count"] == 0 and project["total_words"] == 0
    tree = await client.get(f"/api/v1/projects/{project['id']}/outlines/tree")
    assert tree.status_code == 200, tree.text
    outlines = tree.json()
    assert [(row["chapter_no"], row["volume_no"], row["volume_title"], row["sort"], row["title"])
            for row in outlines] == [
        (1, 1, "同名卷", 1, "来源章纲 2"), (2, 1, "同名卷", 2, "来源章纲 3"),
        (3, 2, "同名卷", 3, "来源章纲 4"), (4, 2, "同名卷", 4, "来源章纲 5"),
    ]
    for row in (outlines[0], outlines[2]):
        assert [plan["seq"] for plan in row["plans"]] == [1, 2]
        assert all(plan["chapter_id"] is None for plan in row["plans"])
    async with app.state.sessionmaker() as db:
        count = await db.scalar(select(func.count()).select_from(Chapter).where(Chapter.project_id == project["id"]))
        assert count == 0


@pytest.mark.parametrize("same_content", [True, False])
async def test_style_uses_frozen_content_and_reuses_only_an_exact_match(app, client, same_content):
    user = await register(client)
    style_values = _style_values()
    async with app.state.sessionmaker() as db:
        existing_values = dict(style_values)
        if not same_content:
            existing_values["tone"] = "之后修改的风格"
        existing = WritingStyle(user_id=user["id"], preset_key=None, **existing_values)
        project = Project(user_id=user["id"], title="冻结风格")
        db.add_all([existing, project])
        await db.flush()
        await apply_template(db, project, _style_snapshot(), {})
        chosen = await db.get(WritingStyle, project.default_style_id)
        assert (chosen.id == existing.id) is same_content
        assert chosen.user_id == user["id"] and chosen.preset_key is None
        assert {name: getattr(chosen, name) for name in style_values} == style_values


@pytest.mark.parametrize("mode", ["same_global", "changed_global", "project"])
async def test_relationship_type_frozen_semantics_and_direction(app, client, mode):
    user = await register(client)
    async with app.state.sessionmaker() as db:
        preset = await db.scalar(select(RelationshipType).where(RelationshipType.project_id.is_(None))
                                 .order_by(RelationshipType.id))
        definition = {"ref": "type.parent", "origin": "project" if mode == "project" else "global",
                      "category": preset.category, "role_a": preset.role_a, "role_b": preset.role_b,
                      "sort": preset.sort + (1 if mode == "changed_global" else 0)}
        expected_global_id = preset.id
        payload = _snapshot().model_dump(mode="json")
        payload["relationship_types"] = [definition]
        project = Project(user_id=user["id"], title="关系类型")
        db.add(project)
        await db.flush()
        await apply_template(db, project, NovelTemplateSnapshot.model_validate(payload), {"character.a": _image()})
        relation = await db.scalar(select(Relationship).where(Relationship.project_id == project.id))
        saved_type = await db.get(RelationshipType, relation.type_id)
        assert (saved_type.category, saved_type.role_a, saved_type.role_b, saved_type.sort) == (
            definition["category"], definition["role_a"], definition["role_b"], definition["sort"])
        assert (saved_type.id == expected_global_id) is (mode == "same_global")
        assert (saved_type.project_id is None) is (mode == "same_global")
        ids = list(await db.scalars(select(Character.id).where(Character.project_id == project.id)
                                    .order_by(Character.id)))
        assert (relation.a_id, relation.b_id) == (ids[0], ids[1])


async def test_relationship_type_collision_aborts_before_inserting_types(app, client):
    user = await register(client)
    payload = _snapshot().model_dump(mode="json")
    payload["relationship_types"].append({"ref": "type.other", "origin": "global", "category": "social",
                                          "role_a": "父亲", "role_b": "子女", "sort": 10000})
    payload["characters"].extend([_character("character.c", "甲"), _character("character.d", "乙")])
    payload["relationships"].append({"type_ref": "type.other", "a_ref": "character.c", "b_ref": "character.d",
                                      "intimacy": 0, "status": "normal", "description": ""})
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError) as exc:
            async with db.begin():
                project = Project(user_id=user["id"], title="关系冲突不留书")
                db.add(project)
                await db.flush()
                await apply_template(db, project, NovelTemplateSnapshot.model_validate(payload),
                                     {"character.a": _image()})
        assert exc.value.extra["code"] == "template_relationship_type_conflict"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Project)) == 0
        assert await db.scalar(select(func.count()).select_from(RelationshipType)
                               .where(RelationshipType.project_id.is_not(None))) == 0


async def test_round_trip_planned_world_and_required_field_without_configuration(app, client):
    user = await register(client)
    source = await create_project(client, title="旧书", genre="不沿用")
    base = f"/api/v1/projects/{source['id']}"
    character_response = await client.post(f"{base}/characters", json={"name": "阿岚"})
    world_response = await client.post(f"{base}/world", json={"title": "山门", "category": "place", "content": "完好"})
    assert character_response.status_code == world_response.status_code == 201
    character, world = character_response.json(), world_response.json()
    image = _image()
    thumbnail = await client.put(
        f"{base}/settings/character/{character['id']}/thumbnail",
        files={"file": ("source.webp", image.data, "image/webp")}, data={"expected_version": ""},
    )
    assert thumbnail.status_code == 200, thumbnail.text
    timeline_response = await client.get(f"{base}/setting-timelines", params={
        "subject_kind": "world", "world_setting_id": world["id"]})
    timeline = timeline_response.json()["items"][0]
    changed = await client.post(f"{base}/setting-timelines/{timeline['id']}/progressions", json={
        "expected_revision": timeline["revision"], "field_key": "content", "custom_field_id": None,
        "operation": "replace", "value": "计划：山门已毁", "anchor_kind": "planned", "chapter_no": 20,
        "chapter_id": None, "boundary": "start",
    })
    assert changed.status_code == 201, changed.text
    created = await client.post(f"{base}/setting-field-definitions", json={
        "label": "故乡", "field_type": "reference", "applicable_types": ["character"],
        "reference_types": ["world.place"], "expected_fields_revision": 0,
    })
    assert created.status_code == 201, created.text
    definition = created.json()["item"]
    current = (await client.get(f"{base}/setting-entries/character/{character['id']}/fields")).json()
    saved = await client.patch(f"{base}/setting-entries/character/{character['id']}/fields/{definition['id']}/baseline",
                               json={"field_type": "reference", "value": {"kind": "world", "id": world["id"]},
                                     "expected_fields_revision": current["fields_revision"],
                                     "expected_timeline_revision": current["timeline_revision"]})
    assert saved.status_code == 200, saved.text
    chapter = await client.post(f"{base}/chapters", json={"chapter_no": 1, "title": "源章节"})
    assert chapter.status_code == 201
    selection = TemplateSelection(include_config=False, character_ids=[character["id"]], world_ids=[world["id"]])
    async with app.state.sessionmaker() as db:
        captured = await capture(db, source["id"], selection)
        source_image_id = await db.scalar(select(SettingImageResource.id).where(
            SettingImageResource.project_id == source["id"]))
    assert captured.snapshot is not None and captured.missing_dependencies == []
    assert captured.snapshot.config is None
    assert [item.label for item in captured.snapshot.field_definitions] == ["故乡"]
    async with app.state.sessionmaker() as db:
        project = Project(user_id=user["id"], title="模板新书")
        db.add(project)
        await db.flush()
        await apply_template(db, project, captured.snapshot, captured.images)
        await db.commit()
        project_id = project.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as admin:
        admin_user = await register(admin, username="recycle_manager")
        async with app.state.sessionmaker() as db:
            (await db.get(User, admin_user["id"])).is_admin = True
            await db.commit()
        removed = await client.delete(f"/api/v1/projects/{source['id']}")
        assert removed.status_code == 204, removed.text
        await purge_trashed_project(admin, source["id"])
    async with app.state.sessionmaker() as db:
        new_world = await db.scalar(select(WorldSetting).where(WorldSetting.project_id == project_id))
        timeline = await db.scalar(select(SettingTimeline).where(
            SettingTimeline.project_id == project_id, SettingTimeline.world_setting_id == new_world.id))
        assert new_world.id != world["id"] and new_world.content == "计划：山门已毁"
        assert timeline.baseline_status == "confirmed" and timeline.baseline == {"content": "计划：山门已毁"}
        assert await db.scalar(select(func.count()).select_from(SettingProgression).where(
            SettingProgression.timeline_id == timeline.id)) == 0
        assert await db.scalar(select(func.count()).select_from(Chapter).where(Chapter.project_id == project_id)) == 0
        new_image = await db.scalar(select(SettingImageResource).where(SettingImageResource.project_id == project_id))
        assert new_image.id != source_image_id and new_image.data == image.data
        new_character_id = await db.scalar(select(Character.id).where(Character.project_id == project_id))
    response = await client.get(f"/api/v1/projects/{project_id}/settings/character/{new_character_id}/thumbnail")
    assert response.status_code == 200 and response.content == image.data
