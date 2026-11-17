"""模板快照只保存显式选择的当前值和独立的局部引用。"""

import io
import random

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import event, update

from app.core.errors import BadRequestError
from app.models import (
    Career,
    CareerStage,
    Chapter,
    ChapterPlan,
    Character,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    Relationship,
    RelationshipType,
    Scene,
    SettingCategory,
    SettingCategoryRule,
    SettingFieldDefinition,
    SettingFieldOption,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
    WritingStyle,
)
from app.models.setting_images import SettingImageResource
from app.schemas.novel_template_snapshot import NovelTemplateSnapshot, TemplatePlan, TemplateSelection, TemplateStyle
from app.schemas.setting_field_value import EntityRef
from app.services import setting_field_service
from app.services.novel_template_snapshot import capture, validate_snapshot
from tests.helpers import create_project, register
from tests.test_card_ai_visibility import _settings
from tests.test_setting_fields import _versions
from tests.test_setting_fields_categories_roundtrip import _fields, _seed


async def test_empty_selection_keeps_config_without_chapters(app, client):
    await register(client)
    project = await create_project(client, title="原书", genre="奇幻", premise="原设定")
    async with app.state.sessionmaker() as db:
        result = await capture(db, project["id"], TemplateSelection())
    assert result.snapshot is not None
    assert result.snapshot.config.genre == "奇幻"
    assert result.snapshot.config.premise == "原设定"
    assert result.counts == {
        "characters": 0,
        "world_settings": 0,
        "organizations": 0,
        "careers": 0,
        "outlines": 0,
        "plans": 0,
        "images": 0,
    }
    assert result.missing_dependencies == []
    payload = result.snapshot.model_dump(mode="json")
    assert "title" not in payload["config"]
    assert "chapters" not in payload and "scenes" not in payload
    assert validate_snapshot(payload, {}) == result.snapshot


async def test_dependency_preview_requires_explicit_parent_and_outline_characters(app, client):
    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        character = Character(project_id=project["id"], name="阿青")
        parent = Organization(project_id=project["id"], name="上级")
        outline = Outline(project_id=project["id"], title="章纲")
        db.add_all((character, parent, outline))
        await db.flush()
        child = Organization(project_id=project["id"], name="下级", parent_id=parent.id)
        db.add_all(
            (
                child,
                ChapterPlan(
                    project_id=project["id"], outline_id=outline.id, seq=1, title="细纲", character_ids=[character.id]
                ),
            )
        )
        await db.commit()
        selection = TemplateSelection(include_config=False, organization_ids=[child.id], outline_ids=[outline.id])
        result = await capture(db, project["id"], selection)
        assert result.snapshot is None
        assert {(item.kind, item.id) for item in result.missing_dependencies} == {
            ("organization", parent.id),
            ("character", character.id),
        }
        reasons = {item.kind: item.reason for item in result.missing_dependencies}
        assert reasons["organization"] == "组织「下级」的上级组织"
        assert reasons["character"] == "大纲「章纲」的细纲角色"
        complete = await capture(
            db,
            project["id"],
            TemplateSelection(
                include_config=False,
                organization_ids=[child.id, parent.id],
                character_ids=[character.id],
                outline_ids=[outline.id],
            ),
        )
    assert complete.snapshot is not None
    assert complete.snapshot.organizations[1].parent_ref == complete.snapshot.organizations[0].ref
    assert complete.snapshot.outlines[0].plans[0].character_refs == [complete.snapshot.characters[0].ref]
    assert result.source_stamp != complete.source_stamp


async def test_same_name_characters_preserve_ordered_relationship_types(app, client):
    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        first = Character(project_id=project["id"], name="同名")
        second = Character(project_id=project["id"], name="同名")
        world = WorldSetting(project_id=project["id"], title="同名", category="place")
        kind = RelationshipType(project_id=project["id"], role_a="父亲", role_b="女儿", category="family", sort=13)
        db.add_all((first, second, world, kind))
        await db.flush()
        db.add(
            Relationship(
                project_id=project["id"],
                a_id=second.id,
                b_id=first.id,
                pair_low=first.id,
                pair_high=second.id,
                type_id=kind.id,
                intimacy=12,
                status="normal",
            )
        )
        await db.commit()
        result = await capture(
            db,
            project["id"],
            TemplateSelection(
                include_config=False,
                character_ids=[first.id, second.id],
                world_ids=[world.id],
            ),
        )
    assert result.snapshot is not None
    assert result.snapshot.characters[0].ref != result.snapshot.characters[1].ref
    assert result.snapshot.world_settings[0].ref != result.snapshot.characters[0].ref
    relation = result.snapshot.relationships[0]
    assert relation.a_ref == result.snapshot.characters[1].ref
    assert relation.b_ref == result.snapshot.characters[0].ref
    assert result.snapshot.relationship_types[0].model_dump() == {
        "ref": relation.type_ref,
        "origin": "project",
        "category": "family",
        "role_a": "父亲",
        "role_b": "女儿",
        "sort": 13,
    }
    assert [item.model_dump() for item in result.included_definitions.relationship_types] == [
        {"origin": "project", "category": "family", "role_a": "父亲", "role_b": "女儿"}
    ]


async def test_omitted_associations_are_counted_without_importing_other_endpoints(app, client):
    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        first = Character(project_id=project["id"], name="已选角色")
        second = Character(project_id=project["id"], name="未选角色")
        org = Organization(project_id=project["id"], name="已选组织")
        kind = RelationshipType(project_id=project["id"], role_a="同伴", role_b="同伴", category="social")
        db.add_all((first, second, org, kind))
        await db.flush()
        db.add_all(
            (
                Relationship(
                    project_id=project["id"],
                    a_id=first.id,
                    b_id=second.id,
                    pair_low=first.id,
                    pair_high=second.id,
                    type_id=kind.id,
                    status="normal",
                ),
                OrganizationMember(organization_id=org.id, character_id=first.id),
                OrganizationMember(organization_id=org.id, character_id=second.id),
            )
        )
        await db.commit()
        result = await capture(
            db,
            project["id"],
            TemplateSelection(
                include_config=False,
                character_ids=[first.id],
                organization_ids=[org.id],
            ),
        )
    assert result.snapshot is not None
    assert len(result.snapshot.characters) == 1
    assert result.snapshot.relationships == []
    assert len(result.snapshot.memberships) == 1
    assert result.included_relations == 1
    assert result.omitted_relations == 2


def test_snapshot_rejects_unknown_fields_and_missing_graph_refs():
    data = {"config": None, "format": "ainovel.novel-template", "version": 1}
    with pytest.raises(ValidationError):
        NovelTemplateSnapshot.model_validate({**data, "chapters": [{"content": "不能复制正文"}]})
    with pytest.raises(ValidationError):
        NovelTemplateSnapshot.model_validate(
            {
                **data,
                "relationships": [
                    {
                        "type_ref": "relationship_type.1",
                        "a_ref": "character.1",
                        "b_ref": "character.2",
                        "intimacy": 0,
                        "status": "normal",
                        "description": "",
                    }
                ],
            }
        )
    snapshot = NovelTemplateSnapshot.model_validate(data)
    snapshot.format = "unexpected"
    with pytest.raises(BadRequestError) as exc:
        validate_snapshot(snapshot, {})
    assert exc.value.extra["code"] == "invalid_novel_template"
    with pytest.raises(ValidationError):
        NovelTemplateSnapshot.model_validate(
            {
                **data,
                "categories": [
                    {
                        "ref": "same",
                        "name": "分类",
                        "applicable_types": ["character"],
                        "match_mode": "all",
                        "sort": 0,
                        "rules": [
                            {"kind": "tag", "sort": 0, "tag": "标签", "definition_ref": None, "option_ref": None}
                        ],
                    }
                ],
                "outlines": [
                    {
                        "ref": "same",
                        "volume_no": 1,
                        "volume_title": "",
                        "title": "大纲",
                        "summary": "",
                        "sort": 0,
                        "plans": [],
                    }
                ],
            }
        )
    plan = {
        "title": "细纲",
        "summary": "",
        "key_events": ["事件"],
        "character_refs": [],
        "emotion": "",
        "goal": "",
        "est_words": 3000,
        "seq": 1,
    }
    assert TemplatePlan.model_validate(plan).est_words == 3000
    with pytest.raises(ValidationError):
        TemplatePlan.model_validate({**plan, "est_words": 299})
    with pytest.raises(ValidationError):
        TemplatePlan.model_validate({**plan, "title": "   "})
    with pytest.raises(ValidationError):
        TemplatePlan.model_validate({**plan, "key_events": ["事件", "事件"]})


@pytest.mark.parametrize("group", ["characters", "world_settings"])
@pytest.mark.parametrize("aliases", [[f"别名{number}" for number in range(11)], ["别名", "别名"], [" 别名 "]])
def test_snapshot_requires_canonical_bounded_aliases(group, aliases):
    from tests.test_novel_template_apply import _snapshot

    payload = _snapshot().model_dump(mode="json")
    payload[group][0]["aliases"] = aliases
    with pytest.raises(ValidationError):
        NovelTemplateSnapshot.model_validate(payload)


def test_snapshot_rejects_blank_legacy_field_name():
    from tests.test_novel_template_apply import _snapshot

    payload = _snapshot().model_dump(mode="json")
    payload["characters"][0]["custom_text_fields"][0]["name"] = "   "
    with pytest.raises(ValidationError):
        NovelTemplateSnapshot.model_validate(payload)


def test_snapshot_style_limits_keep_valid_boundary_and_reject_damaged_content():
    from tests.test_novel_template_apply import _style_values

    style = _style_values() | {"sample": "样" * 2000, "extra": "附" * 5000}
    assert TemplateStyle.model_validate(style).sample == style["sample"]
    for patch in ({"sample": "样" * 2001}, {"extra": "附" * 5001}, {"name": "   "}):
        with pytest.raises(ValidationError):
            TemplateStyle.model_validate(style | patch)


async def test_capture_four_typed_field_kinds_and_classification(app, client):
    project, index = await _seed(client)
    entries = index["entries"]
    selection = TemplateSelection(
        character_ids=[entries["character"]["reference"]["id"]],
        world_ids=[entries["world"]["reference"]["id"]],
        organization_ids=[entries["organization"]["reference"]["id"]],
        career_ids=[entries["career"]["reference"]["id"]],
    )
    async with app.state.sessionmaker() as db:
        result = await capture(db, project["id"], selection)
    assert result.snapshot is not None
    snapshot = result.snapshot
    assert len(snapshot.field_definitions) == 4
    assert len(snapshot.categories) == 2
    included = result.included_definitions
    assert {field.name: field.field_type for field in included.fields} == {
        "成长纪要": "text",
        "阵营口令": "line",
        "阵营": "dropdown",
        "参照档案": "reference",
    }
    assert next(field.options for field in included.fields if field.name == "阵营") == ["旧阵营", "新阵营"]
    assert set(included.categories) == {"同盟且新阵营", "同盟或新阵营"}
    assert included.careers[0].name == "剑修"
    assert included.careers[0].stages == [stage.name for stage in snapshot.careers[0].stages]
    assert snapshot.categories[0].rules[1].definition_ref in {field.ref for field in snapshot.field_definitions}
    assert len(snapshot.characters[0].fields) == len(snapshot.world_settings[0].fields) == 4
    assert len(snapshot.organizations[0].fields) == len(snapshot.careers[0].fields) == 4
    for entry in (snapshot.characters[0], snapshot.world_settings[0], snapshot.organizations[0], snapshot.careers[0]):
        assert any(field.value == {"entry_ref": snapshot.organizations[0].ref} for field in entry.fields)
        assert any(isinstance(field.value, str) and "新口令" in field.value for field in entry.fields)


async def test_config_without_entries_captures_and_validates_complete_catalog(app, client):
    project, index = await _seed(client)
    async with app.state.sessionmaker() as db:
        result = await capture(db, project["id"], TemplateSelection())
        assert result.snapshot is not None
        assert not result.snapshot.characters and not result.snapshot.world_settings
        assert len(result.snapshot.field_definitions) == 4 and len(result.snapshot.categories) == 2
        dropdown = next(field for field in result.snapshot.field_definitions if field.label == "阵营")
        assert [option.label for option in dropdown.options] == ["旧阵营", "新阵营"]

        await db.execute(update(SettingCategoryRule).where(SettingCategoryRule.kind == "tag").values(tag=" 同盟 "))
        await db.commit()
        with pytest.raises(BadRequestError) as invalid_category:
            await capture(db, project["id"], TemplateSelection())
        assert invalid_category.value.extra["code"] == "invalid_novel_template"
        assert (await capture(db, project["id"], TemplateSelection(include_config=False))).snapshot is not None

        await db.execute(update(SettingCategoryRule).where(SettingCategoryRule.kind == "tag").values(tag="同盟"))
        db.add(SettingFieldOption(definition_id=index["definitions"]["成长纪要"]["id"], label="非法选项"))
        await db.commit()
        with pytest.raises(BadRequestError) as invalid_option:
            await capture(db, project["id"], TemplateSelection())
        assert invalid_option.value.extra["code"] == "invalid_novel_template"
        assert (await capture(db, project["id"], TemplateSelection(include_config=False))).snapshot is not None


async def test_config_rejects_overlong_source_style_without_truncating(app, client):
    user = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        style = WritingStyle(user_id=user["id"], name="长风格", sample="样" * 2001)
        db.add(style)
        await db.flush()
        await db.execute(update(Project).where(Project.id == project["id"]).values(default_style_id=style.id))
        await db.commit()
        with pytest.raises(BadRequestError) as invalid:
            await capture(db, project["id"], TemplateSelection())
        assert invalid.value.extra["code"] == "invalid_novel_template"
        assert style.sample == "样" * 2001
        assert (await capture(db, project["id"], TemplateSelection(include_config=False))).snapshot is not None


async def test_ai_links_typed_references_and_career_require_explicit_selection(app, client):
    project, index = await _seed(client)
    entries = index["entries"]
    ids = {kind: entry["reference"]["id"] for kind, entry in entries.items()}
    assignment = await client.post(
        f"{index['base']}/characters/{ids['character']}/careers", json={"career_id": ids["career"]}
    )
    assert assignment.status_code == 201, assignment.text
    await _settings(
        client,
        index["base"],
        "character",
        ids["character"],
        ai_mode="never",
        ai_hidden_builtin_fields=["background"],
        links=[{"kind": "world", "id": ids["world"]}],
    )
    async with app.state.sessionmaker() as db:
        incomplete = await capture(
            db,
            project["id"],
            TemplateSelection(
                include_config=False,
                character_ids=[ids["character"]],
            ),
        )
        complete = await capture(
            db,
            project["id"],
            TemplateSelection(
                include_config=False,
                character_ids=[ids["character"]],
                world_ids=[ids["world"]],
                organization_ids=[ids["organization"]],
                career_ids=[ids["career"]],
            ),
        )
    assert incomplete.snapshot is None
    assert {(item.kind, item.id) for item in incomplete.missing_dependencies} == {
        ("world", ids["world"]),
        ("organization", ids["organization"]),
        ("career", ids["career"]),
    }
    reasons = {item.kind: item.reason for item in incomplete.missing_dependencies}
    assert reasons["world"] == "角色「林岚」的 AI 关联"
    assert reasons["organization"] == "角色「林岚」的字段「参照档案」"
    assert reasons["career"] == "角色「林岚」的当前职业"
    assert {field.name for field in incomplete.included_definitions.fields} == {
        "成长纪要",
        "阵营口令",
        "阵营",
        "参照档案",
    }
    assert next(field.options for field in incomplete.included_definitions.fields if field.name == "阵营") == ["新阵营"]
    assert incomplete.included_definitions.categories == []
    assert incomplete.included_definitions.careers == []
    assert complete.snapshot is not None
    character = complete.snapshot.characters[0]
    assert character.ai_settings.ai_mode == "never"
    assert character.ai_settings.ai_hidden_builtin_fields == ["background"]
    assert character.ai_link_refs == [complete.snapshot.world_settings[0].ref]
    assert complete.snapshot.assignments[0].stage_ref == complete.snapshot.careers[0].stages[0].ref


async def test_scoped_projection_preserves_selected_definition_and_planned_legacy_fields(app, client):
    project, index = await _seed(client)
    character_id = index["entries"]["character"]["reference"]["id"]
    current = await _fields(client, index, "character")
    url = f"{index['base']}/setting-timelines/{current['timeline_id']}"
    created = await client.post(
        f"{url}/text-fields",
        json={"expected_revision": current["timeline_revision"], "label": "旧档标签", "base_value": "旧值"},
    )
    assert created.status_code == 201, created.text
    changed = await client.post(
        f"{url}/progressions",
        json={
            "expected_revision": created.json()["timeline_revision"],
            "custom_field_id": created.json()["item"]["id"],
            "operation": "append",
            "value": "计划值",
            "anchor_kind": "planned",
            "chapter_no": 23,
        },
    )
    assert changed.status_code == 201, changed.text
    entry = EntityRef(kind="character", id=character_id)
    definition_id = index["definitions"]["阵营口令"]["id"]
    async with app.state.sessionmaker() as db:
        full = (await setting_field_service.resolve_entry_fields(db, project["id"], [entry]))["character", character_id]
        scoped = (
            await setting_field_service.resolve_entry_fields(
                db, project["id"], [entry], definition_ids={definition_id}, include_legacy=True
            )
        )["character", character_id]
        old_scope = (
            await setting_field_service.resolve_entry_fields(db, project["id"], [entry], definition_ids={definition_id})
        )["character", character_id]
        legacy_only = (
            await setting_field_service.resolve_entry_fields(
                db, project["id"], [entry], definition_ids=set(), include_legacy=True
            )
        )["character", character_id]
        result = await capture(
            db,
            project["id"],
            TemplateSelection(include_config=False, character_ids=[character_id]),
        )
    assert len(full.fields) == 4 and full.legacy_fields[0].value == "旧值\n计划值"
    assert [(slot.label, slot.value) for slot in scoped.fields] == [("阵营口令", "林岚新口令")]
    assert scoped.legacy_fields[0].value == "旧值\n计划值"
    assert old_scope.legacy_fields == []
    assert legacy_only.fields == [] and legacy_only.legacy_fields[0].value == "旧值\n计划值"
    assert result.snapshot is None  # 引用的组织仍需显式选择，但不影响旧字段求值


async def _assert_current_projection_matches_timeline_order(app, client):
    project, index = await _seed(client)
    character_id = index["entries"]["character"]["reference"]["id"]
    current = await _fields(client, index, "character")
    field_id = next(slot["field_id"] for slot in current["fields"] if slot["label"] == "成长纪要")
    chapter_id = index["chapters"][21]
    async with app.state.sessionmaker() as db:
        first = Scene(chapter_id=chapter_id, sort=1)
        second = Scene(chapter_id=chapter_id, sort=2)
        inactive = Scene(chapter_id=chapter_id, sort=3, active=False)
        db.add_all((first, second, inactive))
        await db.flush()

        def change(
            value,
            *,
            anchor_kind="planned",
            chapter_no=None,
            scene_id=None,
            chapter_id=None,
            boundary="start",
            protection="manual",
            active=True,
        ):
            return SettingProgression(
                timeline_id=current["timeline_id"],
                custom_field_id=field_id,
                operation="append",
                value=value,
                anchor_kind=anchor_kind,
                chapter_no=chapter_no,
                scene_id=scene_id,
                chapter_id=chapter_id,
                boundary=boundary,
                protection=protection,
                active=active,
            )

        db.add_all(
            [
                change("计划下一章", chapter_no=22),
                change("场景二", anchor_kind="scene", scene_id=second.id),
                change("章尾", anchor_kind="chapter", chapter_id=chapter_id, boundary="end"),
                change("失效事件", chapter_no=24, active=False),
                change("缺少章节", anchor_kind="chapter"),
                change("缺少场景", anchor_kind="scene"),
                change("退场场景", anchor_kind="scene", scene_id=inactive.id),
                change("场景一结束", anchor_kind="scene", scene_id=first.id, boundary="end"),
                change("章首手动", chapter_no=21),
                change("场景一开始", anchor_kind="scene", scene_id=first.id),
                change("章首自动", anchor_kind="chapter", chapter_id=chapter_id, protection="automatic"),
                change("长追加" * 10000, chapter_no=23),
            ]
        )
        await db.commit()
        entry = EntityRef(kind="character", id=character_id)
        definitions = {row["id"] for row in index["definitions"].values()}
        classic = (
            await setting_field_service.resolve_entry_fields(
                db,
                project["id"],
                [entry],
                definition_ids=definitions,
                include_legacy=True,
            )
        )["character", character_id]
        streamed = (
            await setting_field_service.resolve_entry_fields(
                db,
                project["id"],
                [entry],
                definition_ids=definitions,
                include_legacy=True,
                current_limit_bytes=10 * 1024 * 1024,
            )
        )["character", character_id]
    expected = next(slot.value for slot in classic.fields if slot.label == "成长纪要")
    actual = next(slot.value for slot in streamed.fields if slot.label == "成长纪要")
    assert actual == expected
    ordered = ["章首自动", "章首手动", "场景一开始", "场景一结束", "场景二", "章尾", "计划下一章"]
    assert all(actual.index(left) < actual.index(right) for left, right in zip(ordered, ordered[1:], strict=False))
    assert "失效事件" not in actual and "缺少章节" not in actual and "退场场景" not in actual
    assert actual.endswith("长追加" * 10000)


async def test_current_projection_matches_chapter_scene_and_planned_order(app, client):
    await _assert_current_projection_matches_timeline_order(app, client)


async def test_current_projection_matches_existing_resolver_across_multiple_timelines(app, client):
    await register(client)
    project = await create_project(client)
    rng = random.Random(13)
    async with app.state.sessionmaker() as db:
        characters = [Character(project_id=project["id"], name=f"角色{i}") for i in range(2)]
        chapters = [Chapter(project_id=project["id"], chapter_no=number) for number in (2, 2, 5)]
        db.add_all([*characters, *chapters])
        await db.flush()
        scenes = [Scene(chapter_id=chapter.id, sort=index) for chapter in chapters for index in range(2)]
        scenes.append(Scene(chapter_id=chapters[0].id, sort=2, active=False))
        timelines = [
            SettingTimeline(project_id=project["id"], subject_kind="character", character_id=character.id)
            for character in characters
        ]
        db.add_all([*scenes, *timelines])
        await db.flush()
        bindings = [
            SettingTextField(timeline_id=timeline.id, label=f"字段{i}", base_value="" if i % 2 else "基础")
            for timeline in timelines for i in range(2)
        ]
        db.add_all(bindings)
        await db.flush()
        for binding in bindings:
            for number in range(90):
                anchor = rng.choice(("planned", "chapter", "scene", "orphan"))
                position = {
                    "anchor_kind": "chapter" if anchor == "orphan" else anchor,
                    "chapter_no": rng.randint(1, 7) if anchor == "planned" else None,
                    "chapter_id": rng.choice(chapters).id if anchor == "chapter" else None,
                    "scene_id": rng.choice(scenes).id if anchor == "scene" else None,
                }
                db.add(SettingProgression(
                    timeline_id=binding.timeline_id,
                    custom_field_id=binding.id,
                    operation=rng.choice(("replace", "append")),
                    value=f"值{number}",
                    boundary=rng.choice(("start", "end")),
                    protection=rng.choice(("automatic", "confirmed", "manual")),
                    sort_order=rng.randint(0, 4),
                    active=rng.choice((True, True, True, False)),
                    **position,
                ))
        await db.commit()
        entries = [EntityRef(kind="character", id=character.id) for character in characters]
        classic = await setting_field_service.resolve_entry_fields(
            db, project["id"], entries, definition_ids=set(), include_legacy=True,
        )
        streamed = await setting_field_service.resolve_entry_fields(
            db, project["id"], entries, definition_ids=set(), include_legacy=True, current_limit_bytes=4096,
        )
    for key in classic:
        assert [(slot.label, slot.value) for slot in streamed[key].legacy_fields] == [
            (slot.label, slot.value) for slot in classic[key].legacy_fields
        ]


async def test_current_projection_skips_large_overwritten_history(app, client):
    project, index = await _seed(client)
    character_id = index["entries"]["character"]["reference"]["id"]
    current = await _fields(client, index, "character")
    field_id = next(slot["field_id"] for slot in current["fields"] if slot["label"] == "成长纪要")
    async with app.state.sessionmaker() as db:
        db.add_all(
            [
                SettingProgression(
                    timeline_id=current["timeline_id"],
                    custom_field_id=field_id,
                    operation="replace",
                    value=f"旧值{number}",
                    anchor_kind="planned",
                    chapter_no=number + 30,
                )
                for number in range(1000)
            ]
        )
        db.add_all(
            [
                SettingProgression(
                    timeline_id=current["timeline_id"],
                    custom_field_id=field_id,
                    operation="replace",
                    value="x" * (10 * 1024 * 1024 + 1),
                    anchor_kind="planned",
                    chapter_no=1100,
                ),
                SettingProgression(
                    timeline_id=current["timeline_id"],
                    custom_field_id=field_id,
                    operation="replace",
                    value="新起点",
                    anchor_kind="planned",
                    chapter_no=1101,
                ),
                SettingProgression(
                    timeline_id=current["timeline_id"],
                    custom_field_id=field_id,
                    operation="append",
                    value="继续",
                    anchor_kind="planned",
                    chapter_no=1102,
                ),
            ]
        )
        await db.commit()
        entry = EntityRef(kind="character", id=character_id)
        definitions = {row["id"] for row in index["definitions"].values()}
        classic = (
            await setting_field_service.resolve_entry_fields(
                db,
                project["id"],
                [entry],
                definition_ids=definitions,
                include_legacy=True,
            )
        )["character", character_id]
        streamed = (
            await setting_field_service.resolve_entry_fields(
                db,
                project["id"],
                [entry],
                definition_ids=definitions,
                include_legacy=True,
                current_limit_bytes=10 * 1024 * 1024,
            )
        )["character", character_id]
        captured = await capture(
            db,
            project["id"],
            TemplateSelection(
                include_config=False,
                character_ids=[character_id],
                organization_ids=[index["entries"]["organization"]["reference"]["id"]],
            ),
        )
    assert next(slot.value for slot in classic.fields if slot.label == "成长纪要") == "新起点\n继续"
    assert next(slot.value for slot in streamed.fields if slot.label == "成长纪要") == "新起点\n继续"
    assert captured.snapshot is not None
    assert any(field.value == "新起点\n继续" for field in captured.snapshot.characters[0].fields)


async def test_current_projection_keeps_many_short_appends_in_order(app, client):
    project, index = await _seed(client)
    character_id = index["entries"]["character"]["reference"]["id"]
    current = await _fields(client, index, "character")
    field_id = next(slot["field_id"] for slot in current["fields"] if slot["label"] == "成长纪要")
    async with app.state.sessionmaker() as db:
        db.add_all(
            [
                SettingProgression(
                    timeline_id=current["timeline_id"],
                    custom_field_id=field_id,
                    operation="append",
                    value=f"片{number}",
                    anchor_kind="planned",
                    chapter_no=number + 30,
                )
                for number in range(4000)
            ]
        )
        await db.commit()
        entry = EntityRef(kind="character", id=character_id)
        definitions = {row["id"] for row in index["definitions"].values()}
        classic = (
            await setting_field_service.resolve_entry_fields(
                db,
                project["id"],
                [entry],
                definition_ids=definitions,
                include_legacy=True,
            )
        )["character", character_id]
        streamed = (
            await setting_field_service.resolve_entry_fields(
                db,
                project["id"],
                [entry],
                definition_ids=definitions,
                include_legacy=True,
                current_limit_bytes=10 * 1024 * 1024,
            )
        )["character", character_id]
    original = next(slot.value for slot in classic.fields if slot.label == "成长纪要")
    actual = next(slot.value for slot in streamed.fields if slot.label == "成长纪要")
    assert actual == original
    assert actual.splitlines()[-4000:] == [f"片{number}" for number in range(4000)]


async def test_current_projection_streams_only_effective_events_and_rejects_empty_append(app, client, monkeypatch):
    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        character = Character(project_id=project["id"], name="角色")
        db.add(character)
        await db.flush()
        timeline = SettingTimeline(project_id=project["id"], subject_kind="character", character_id=character.id)
        db.add(timeline)
        await db.flush()
        field = SettingTextField(timeline_id=timeline.id, label="纪要", base_value="基础")
        db.add(field)
        await db.flush()
        for number in range(1500):
            db.add_all([
                SettingProgression(
                    timeline_id=timeline.id, custom_field_id=field.id, operation="append", value="",
                    anchor_kind="planned", chapter_no=number + 1, active=False,
                ),
                SettingProgression(
                    timeline_id=timeline.id, custom_field_id=field.id, operation="append", value="",
                    anchor_kind="chapter", chapter_id=None,
                ),
                SettingProgression(
                    timeline_id=timeline.id, custom_field_id=field.id, operation="replace", value="",
                    anchor_kind="planned", chapter_no=number + 1,
                ),
            ])
        db.add(SettingProgression(
            timeline_id=timeline.id, custom_field_id=field.id, operation="replace", value="最终",
            anchor_kind="planned", chapter_no=2000,
        ))
        await db.commit()
        entry = EntityRef(kind="character", id=character.id)
        original_stream = db.stream
        delivered = 0

        class CountingResult:
            def __init__(self, result):
                self.result = result

            def __aiter__(self):
                return self

            async def __anext__(self):
                nonlocal delivered
                row = await self.result.__anext__()
                delivered += 1
                return row

            async def close(self):
                await self.result.close()

        async def counted_stream(statement, *args, **kwargs):
            result = await original_stream(statement, *args, **kwargs)
            return CountingResult(result) if str(statement).startswith("SELECT setting_progressions.value,") else result

        monkeypatch.setattr(db, "stream", counted_stream)
        result = await setting_field_service.resolve_entry_fields(
            db, project["id"], [entry], definition_ids=set(), include_legacy=True, current_limit_bytes=4096,
        )
        assert result["character", character.id].legacy_fields[0].value == "最终"
        assert delivered == 1

        db.add(SettingProgression(
            timeline_id=timeline.id, custom_field_id=field.id, operation="append", value="",
            anchor_kind="planned", chapter_no=2001,
        ))
        await db.commit()
        for limit in (None, 4096):
            with pytest.raises(ValueError, match="追加内容不能为空"):
                await setting_field_service.resolve_entry_fields(
                    db, project["id"], [entry], definition_ids=set(), include_legacy=True, current_limit_bytes=limit,
                )


async def test_unrelated_corrupt_data_does_not_block_small_selection(app, client):
    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        selected = Character(project_id=project["id"], name="已选")
        other = Character(project_id=project["id"], name="未选损坏", setting_tags=["  "])
        db.add_all([selected, other, *[Character(project_id=project["id"], name=f"未选{i}") for i in range(300)]])
        await db.flush()
        timeline = SettingTimeline(project_id=project["id"], subject_kind="character", character_id=other.id)
        db.add(timeline)
        await db.flush()
        field = SettingTextField(timeline_id=timeline.id, label="损坏字段", base_value={"unexpected": "value"})
        db.add(field)
        await db.commit()
        result = await capture(db, project["id"], TemplateSelection(include_config=False, character_ids=[selected.id]))
        assert result.snapshot is not None
        assert [item.name for item in result.snapshot.characters] == ["已选"]
        with pytest.raises(BadRequestError) as invalid:
            await capture(db, project["id"], TemplateSelection(include_config=False, character_ids=[other.id]))
        assert invalid.value.extra["code"] == "invalid_novel_template"


async def test_missing_reference_of_incompatible_type_fails_before_preview(app, client):
    project, index = await _seed(client)
    character_id = index["entries"]["character"]["reference"]["id"]
    reference_id = index["definitions"]["参照档案"]["id"]
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(SettingFieldDefinition)
            .where(SettingFieldDefinition.id == reference_id)
            .values(reference_types=["world.place"])
        )
        await db.commit()
        with pytest.raises(BadRequestError) as invalid:
            await capture(db, project["id"], TemplateSelection(include_config=False, character_ids=[character_id]))
    assert invalid.value.extra["code"] == "invalid_novel_template"


async def test_unselected_career_with_large_text_is_only_reported_as_dependency(app, client):
    project, index = await _seed(client)
    entries = {kind: item["reference"]["id"] for kind, item in index["entries"].items()}
    assigned = await client.post(
        f"{index['base']}/characters/{entries['character']}/careers", json={"career_id": entries["career"]}
    )
    assert assigned.status_code == 201, assigned.text
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Career).where(Career.id == entries["career"]).values(description="x" * (11 * 1024 * 1024))
        )
        await db.commit()
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            result = await capture(
                db,
                project["id"],
                TemplateSelection(
                    include_config=False,
                    character_ids=[entries["character"]],
                    organization_ids=[entries["organization"]],
                ),
            )
            assert result.snapshot is None
            assert any(item.kind == "career" and item.id == entries["career"] for item in result.missing_dependencies)
            assert not any("careers.description" in statement for statement in statements)
            with pytest.raises(BadRequestError) as too_large:
                await capture(
                    db,
                    project["id"],
                    TemplateSelection(
                        include_config=False,
                        character_ids=[entries["character"]],
                        organization_ids=[entries["organization"]],
                        career_ids=[entries["career"]],
                    ),
                )
            assert too_large.value.extra["code"] == "template_too_large"
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_selected_stage_budget_is_checked_before_loading_description(app, client):
    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        career = Career(project_id=project["id"], name="剑修", kind="main")
        db.add(career)
        await db.flush()
        db.add(CareerStage(career_id=career.id, position=1, name="初阶", description="x" * (11 * 1024 * 1024)))
        await db.commit()
        career_id = career.id
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            assert (await capture(db, project["id"], TemplateSelection(include_config=False))).snapshot is not None
            with pytest.raises(BadRequestError) as too_large:
                await capture(db, project["id"], TemplateSelection(include_config=False, career_ids=[career_id]))
        assert too_large.value.extra["code"] == "template_too_large"
        assert not any(s.startswith("select career_stages.id, career_stages.career_id") for s in statements)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.parametrize("column", ["key_events", "character_ids"])
async def test_plan_json_budget_is_checked_before_loading_values(app, client, monkeypatch, column):
    from app.services import novel_template_snapshot

    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        outline = Outline(project_id=project["id"], title="章纲")
        db.add(outline)
        await db.flush()
        plan = ChapterPlan(project_id=project["id"], outline_id=outline.id, seq=1, title="细纲")
        setattr(plan, column, ["x" * 25000] if column == "key_events" else [1] * 10000)
        db.add(plan)
        await db.commit()
        outline_id = outline.id
    monkeypatch.setattr(novel_template_snapshot, "MAX_JSON_BYTES", 4096)
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError) as too_large:
                await capture(db, project["id"], TemplateSelection(include_config=False, outline_ids=[outline_id]))
        assert too_large.value.extra["code"] == "template_too_large"
        assert not any(s.startswith("select chapter_plans.id,") for s in statements)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_plan_json_budget_accepts_normal_escaped_unicode(app, client, monkeypatch):
    from app.services import novel_template_snapshot

    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        outline = Outline(project_id=project["id"], title="章纲")
        db.add(outline)
        await db.flush()
        db.add(ChapterPlan(
            project_id=project["id"], outline_id=outline.id, seq=1, title="细纲", key_events=["中" * 200],
        ))
        await db.commit()
        outline_id = outline.id
    monkeypatch.setattr(novel_template_snapshot, "MAX_JSON_BYTES", 4096)
    async with app.state.sessionmaker() as db:
        result = await capture(db, project["id"], TemplateSelection(include_config=False, outline_ids=[outline_id]))
    assert result.snapshot is not None
    assert result.snapshot.outlines[0].plans[0].key_events == ["中" * 200]


async def test_full_config_budget_checks_option_and_category_catalogs_before_loading(app, client, monkeypatch):
    from app.services import novel_template_snapshot

    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        definition = SettingFieldDefinition(
            project_id=project["id"],
            label="阵营",
            field_type="dropdown",
            applicable_types=["character"],
            reference_types=[],
        )
        db.add(definition)
        await db.flush()
        db.add_all(SettingFieldOption(definition_id=definition.id, label=f"选项{number}" * 10) for number in range(40))
        await db.commit()
    monkeypatch.setattr(novel_template_snapshot, "MAX_JSON_BYTES", 1500)
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            assert (await capture(db, project["id"], TemplateSelection(include_config=False))).snapshot is not None
            with pytest.raises(BadRequestError) as too_large:
                await capture(db, project["id"], TemplateSelection())
        assert too_large.value.extra["code"] == "template_too_large"
        assert not any(
            s.startswith("select setting_field_options.id, setting_field_options.definition_id") for s in statements
        )
        async with app.state.sessionmaker() as db:
            await db.execute(
                update(SettingFieldOption)
                .where(SettingFieldOption.definition_id == definition.id)
                .values(
                    label=SettingFieldOption.id,
                )
            )
            category = SettingCategory(
                project_id=project["id"], name="分类", applicable_types=["character"], match_mode="all"
            )
            db.add(category)
            await db.flush()
            db.add_all(SettingCategoryRule(category_id=category.id, kind="tag", tag="长" * 50) for _ in range(20))
            await db.commit()
        # 另设独立较高上限，确保先通过选项预检，再由分类规则预检拦截。
        monkeypatch.setattr(novel_template_snapshot, "MAX_JSON_BYTES", 4000)
        statements.clear()
        async with app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError) as too_large:
                await capture(db, project["id"], TemplateSelection())
        assert too_large.value.extra["code"] == "template_too_large"
        assert not any(s.startswith("select setting_categories.id, setting_categories.name") for s in statements)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_partial_selection_queries_only_current_dropdown_options(app, client):
    project, index = await _seed(client)
    definition_id = index["definitions"]["阵营"]["id"]
    entries = {kind: item["reference"]["id"] for kind, item in index["entries"].items()}
    async with app.state.sessionmaker() as db:
        db.add_all(SettingFieldOption(definition_id=definition_id, label=f"未选{number}") for number in range(200))
        await db.commit()
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            result = await capture(
                db,
                project["id"],
                TemplateSelection(
                    include_config=False,
                    character_ids=[entries["character"]],
                    organization_ids=[entries["organization"]],
                ),
            )
        assert result.snapshot is not None
        assert next(field.options for field in result.included_definitions.fields if field.name == "阵营") == ["新阵营"]
        option_queries = [s for s in statements if "from setting_field_options" in s]
        assert option_queries and all("setting_field_options.id in" in statement for statement in option_queries)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_capture_planned_values_and_image_snapshot_are_independent(app, client):
    project, index = await _seed(client)
    character_id = index["entries"]["character"]["reference"]["id"]
    definition = index["definitions"]["阵营口令"]
    current = await _fields(client, index, "character")
    response = await client.post(
        f"{index['base']}/setting-entries/character/{character_id}/fields/{definition['id']}/progressions",
        json={
            **_versions(current),
            "field_type": "line",
            "value": "三十章计划口令",
            "operation": "replace",
            "anchor_kind": "planned",
            "chapter_no": 30,
            "boundary": "end",
        },
    )
    assert response.status_code == 201, response.text
    png = io.BytesIO()
    Image.new("RGB", (16, 8), "blue").save(png, format="PNG")
    upload = await client.put(
        f"{index['base']}/settings/character/{character_id}/thumbnail",
        data={"expected_version": ""},
        files={"file": ("a.png", png.getvalue(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    entries = index["entries"]
    selection = TemplateSelection(
        character_ids=[character_id],
        world_ids=[entries["world"]["reference"]["id"]],
        organization_ids=[entries["organization"]["reference"]["id"]],
        career_ids=[entries["career"]["reference"]["id"]],
    )
    async with app.state.sessionmaker() as db:
        first = await capture(db, project["id"], selection)
    assert first.snapshot is not None
    character = first.snapshot.characters[0]
    assert any(field.value == "三十章计划口令" for field in character.fields)
    assert character.image_ref == character.ref
    original_image = first.images[character.ref].data
    assert original_image[:4] == b"RIFF"
    png = io.BytesIO()
    Image.new("RGB", (16, 8), "red").save(png, format="PNG")
    upload2 = await client.put(
        f"{index['base']}/settings/character/{character_id}/thumbnail",
        data={"expected_version": upload.json()["version"]},
        files={"file": ("b.png", png.getvalue(), "image/png")},
    )
    assert upload2.status_code == 200, upload2.text
    async with app.state.sessionmaker() as db:
        second = await capture(db, project["id"], selection)
    assert first.images[character.ref].data == original_image
    assert second.images[character.ref].data != original_image
    assert first.source_stamp != second.source_stamp


async def test_image_size_preflight_precedes_reading_binary(app, client, monkeypatch):
    from app.core.errors import BadRequestError
    from app.services import novel_template_snapshot

    await register(client)
    project = await create_project(client)
    character = await client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": "角色"})
    assert character.status_code == 201
    png = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(png, format="PNG")
    uploaded = await client.put(
        f"/api/v1/projects/{project['id']}/settings/character/{character.json()['id']}/thumbnail",
        data={"expected_version": ""},
        files={"file": ("a.png", png.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200
    monkeypatch.setattr(novel_template_snapshot, "MAX_IMAGE_BYTES", 1)
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError) as exc:
                await capture(db, project["id"], TemplateSelection(character_ids=[character.json()["id"]]))
        assert exc.value.extra["code"] == "template_too_large"
        assert not any(
            "setting_image_resources.mime_type, setting_image_resources.data" in statement for statement in statements
        )
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_capture_rejects_selection_over_limit_before_query():
    from app.core.errors import BadRequestError

    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError(f"超限前不应访问数据库: {name}")

    with pytest.raises(BadRequestError) as exc:
        await capture(NoDatabase(), 1, TemplateSelection(character_ids=list(range(1, 2002))))
    assert exc.value.extra["code"] == "template_too_large"


async def test_image_actual_bytes_override_corrupted_size_metadata(app, client, monkeypatch):
    from app.core.errors import BadRequestError
    from app.services import novel_template_snapshot

    await register(client)
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": "角色"})
    char_id = response.json()["id"]
    png = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(png, format="PNG")
    uploaded = await client.put(
        f"/api/v1/projects/{project['id']}/settings/character/{char_id}/thumbnail",
        data={"expected_version": ""},
        files={"file": ("a.png", png.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(SettingImageResource)
            .where(SettingImageResource.id == uploaded.json()["version"])
            .values(byte_size=1)
        )
        await db.commit()
    selection = TemplateSelection(character_ids=[char_id])
    monkeypatch.setattr(novel_template_snapshot, "MAX_IMAGE_BYTES", 1)
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError) as exc:
            await capture(db, project["id"], selection)
    assert exc.value.extra["code"] == "template_too_large"
    monkeypatch.setattr(novel_template_snapshot, "MAX_IMAGE_BYTES", 50 * 1024 * 1024)
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError) as exc:
            await capture(db, project["id"], selection)
    assert exc.value.extra["code"] == "invalid_novel_template"


async def test_oversized_corrupt_image_is_rejected_in_sql_before_blob_read(app, client):
    await register(client)
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": "角色"})
    char_id = response.json()["id"]
    png = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(png, format="PNG")
    uploaded = await client.put(
        f"/api/v1/projects/{project['id']}/settings/character/{char_id}/thumbnail",
        data={"expected_version": ""},
        files={"file": ("a.png", png.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(SettingImageResource)
            .where(SettingImageResource.id == uploaded.json()["version"])
            .values(
                byte_size=1,
                data=b"x" * (512 * 1024 + 1),
            )
        )
        await db.commit()
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError) as exc:
                await capture(db, project["id"], TemplateSelection(character_ids=[char_id]))
        assert exc.value.extra["code"] == "invalid_novel_template"
        assert any("length(setting_image_resources.data)" in statement for statement in statements)
        assert not any("setting_image_resources.mime_type, setting_image_resources.data" in s for s in statements)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_text_budget_checks_utf8_bytes_before_loading_entry(app, client, monkeypatch):
    from app.core.errors import BadRequestError
    from app.services import novel_template_snapshot

    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        character = Character(project_id=project["id"], name="甲", profile="测" * 11)
        db.add(character)
        await db.commit()
        char_id = character.id
    monkeypatch.setattr(novel_template_snapshot, "MAX_JSON_BYTES", 30)
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with app.state.sessionmaker() as db:
            with pytest.raises(BadRequestError) as exc:
                await capture(db, project["id"], TemplateSelection(character_ids=[char_id]))
        assert exc.value.extra["code"] == "template_too_large"
        assert not any(statement.startswith("select characters.id, characters.project_id") for statement in statements)
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)
