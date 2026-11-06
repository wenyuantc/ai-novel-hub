"""四类字段在真实模型消息中共用目标章初投影；不扩大选角或递归引用资料。"""

import pytest
from sqlalchemy import func, select, update

from app.ai.prompts.registry import get_template
from app.core.errors import ConflictError
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
    PromptTemplateOverride,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
)
from app.models.planning import PlanningUnit, PlanningUnitCareer, PlanningUnitOrganization, PlanningUnitWorld
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption, SettingReferenceHandle
from app.schemas.setting_fields import EntityRef, EntryFieldsOut, FieldSlot, OptionOut, SettingEntry
from app.services import analysis_service, context_builder, planning_context_service, setting_field_service
from app.services.setting_field_formatting import format_entry_fields
from tests.helpers import analysis_json, create_ai_config, create_project, parse_sse, register, run_task


async def _seed(app, client, *, planning=False, legacy_override=False):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    pid = project["id"]
    async with app.state.sessionmaker() as db:
        hero = Character(project_id=pid, name="林远", role_type="主角", current_state="原始人物状态", ai_mode=None)
        world = WorldSetting(project_id=pid, title="旧城", category="place", content="原始世界正文", ai_mode=None)
        org = Organization(project_id=pid, name="观星阁", purpose="观察星象", ai_mode=None)
        career = Career(project_id=pid, name="符师", kind="main", rules="符术消耗灵力", ai_mode=None)
        unselected_org = Organization(project_id=pid, name="远方组织", ai_mode=None)
        unselected_career = Career(project_id=pid, name="远方职业", kind="sub", ai_mode=None)
        targets = [
            Career(
                project_id=pid, name=name, kind="sub", description="引用目标正文禁止注入",
                rules="引用规则禁止注入", ai_mode=None,
            )
            for name in ("旧参照职业", "新参照职业")
        ]
        chapters = {
            n: Chapter(project_id=pid, chapter_no=n, title=f"章节{n}", content="林远走进旧城。")
            for n in (5, 20, 21, 22)
        }
        db.add_all([hero, world, org, career, unselected_org, unselected_career, *targets, *chapters.values()])
        await db.flush()
        stage = CareerStage(career_id=career.id, position=1, name="入门")
        db.add(stage)
        await db.flush()
        db.add_all(
            [
                OrganizationMember(character_id=hero.id, organization_id=org.id, position="学徒"),
                CharacterCareer(character_id=hero.id, career_id=career.id, role="main", stage_id=stage.id),
            ]
        )
        definitions = {
            kind: SettingFieldDefinition(
                project_id=pid,
                label=label,
                field_type=kind,
                applicable_types=["character", "world.place", "organization", "career"],
                reference_types=["career"] if kind == "reference" else [],
                sort=i,
            )
            for i, (kind, label) in enumerate(
                (
                    ("text", "成长纪要"),
                    ("line", "阵营口令"),
                    ("dropdown", "资历级别"),
                    ("reference", "参照档案"),
                )
            )
        }
        handles = [SettingReferenceHandle(project_id=pid, kind="career", career_id=target.id) for target in targets]
        db.add_all([*definitions.values(), *handles])
        await db.flush()
        options = [
            SettingFieldOption(definition_id=definitions["dropdown"].id, label=label) for label in ("旧阶名", "新阶名")
        ]
        db.add_all(options)
        await db.flush()
        entities = [
            ("character", hero, "角色"),
            ("world", world, "世界"),
            ("organization", org, "组织"),
            ("career", career, "职业"),
            ("organization", unselected_org, "未选组织"),
            ("career", unselected_career, "未选职业"),
        ]
        for kind, entity, prefix in entities:
            baseline = {}
            if kind == "character":
                baseline = {
                    key: getattr(hero, key)
                    for key in (
                        "profile",
                        "appearance",
                        "personality",
                        "background",
                        "current_state",
                        "age",
                        "gender",
                        "life_status",
                    )
                }
            elif kind == "world":
                baseline = {"content": world.content}
            column = {
                "character": "character_id",
                "world": "world_setting_id",
                "organization": "organization_id",
                "career": "career_id",
            }[kind]
            timeline = SettingTimeline(
                project_id=pid,
                subject_kind="career_entry" if kind == "career" else kind,
                **{column: entity.id},
                baseline=baseline,
            )
            db.add(timeline)
            await db.flush()
            values = {
                "text": f"{prefix}初始纪要",
                "line": f"{prefix}旧口令",
                "dropdown": {"option_id": options[0].id},
                "reference": {"reference_handle_id": handles[0].id},
            }
            later_values = {
                "text": f"{prefix}后期追加",
                "line": f"{prefix}新口令",
                "dropdown": {"option_id": options[1].id},
                "reference": {"reference_handle_id": handles[1].id},
            }
            for field_type, definition in definitions.items():
                field = SettingTextField(
                    timeline_id=timeline.id, definition_id=definition.id, label=None, base_value=values[field_type]
                )
                db.add(field)
                await db.flush()
                db.add(
                    SettingProgression(
                        timeline_id=timeline.id,
                        custom_field_id=field.id,
                        operation="append" if field_type == "text" else "replace",
                        value=later_values[field_type],
                        anchor_kind="chapter",
                        chapter_id=chapters[20].id,
                        boundary="end",
                        source="manual",
                    )
                )
                if field_type != "text":
                    db.add(
                        SettingProgression(
                            timeline_id=timeline.id,
                            custom_field_id=field.id,
                            operation="replace",
                            value="" if field_type == "line" else None,
                            anchor_kind="planned",
                            chapter_no=22,
                            boundary="start",
                            source="manual",
                        )
                    )
            if kind in ("character", "world"):
                db.add(SettingTextField(timeline_id=timeline.id, label="旧版备注", base_value=f"{prefix}旧字段仍可读"))
        plan_id = None
        if planning:
            outline = Outline(project_id=pid, title="尚未建章", chapter_no=23, volume_no=1)
            db.add(outline)
            await db.flush()
            plan = ChapterPlan(project_id=pid, outline_id=outline.id, seq=1, title="计划", character_ids=[])
            db.add(plan)
            await db.flush()
            plan_id = plan.id
            units = [
                PlanningUnit(project_id=pid, chapter_id=chapter.id, pov_character_id=hero.id)
                for chapter in chapters.values()
            ]
            units.append(PlanningUnit(project_id=pid, plan_id=plan.id, pov_character_id=hero.id))
            db.add_all(units)
            await db.flush()
            for unit in units:
                db.add_all(
                    [
                        PlanningUnitWorld(unit_id=unit.id, world_id=world.id),
                        PlanningUnitOrganization(unit_id=unit.id, organization_id=org.id),
                        PlanningUnitCareer(unit_id=unit.id, career_id=career.id),
                    ]
                )
        if legacy_override:
            project_row = await db.get(Project, pid)
            for key, variable in (("chapter.rewrite", "setting_context"), ("chapter.generate", "planning_context")):
                template = get_template(key)
                user = template.user.replace("{{ " + variable + " }}", "") + "\n旧模板标记"
                db.add(
                    PromptTemplateOverride(
                        user_id=project_row.user_id,
                        key=key,
                        system=template.system,
                        user=user,
                        base_system=template.system,
                        base_user=user,
                        base_fingerprint="old",
                    )
                )
        await db.commit()
        return {"pid": pid, "chapters": {n: c.id for n, c in chapters.items()}, "plan_id": plan_id}


def _prompt(call):
    return "\n".join(message["content"] for message in call["messages"])


def _assert_fields(text, no, *, analysis=False):
    prefixes = ("角色", "组织", "职业") if analysis else ("角色", "世界", "组织", "职业")
    for prefix in prefixes:
        assert f"成长纪要：{prefix}初始纪要" in text
        if no <= 20:
            assert f"阵营口令：{prefix}旧口令" in text
            assert f"{prefix}后期追加" not in text
            assert f"阵营口令：{prefix}新口令" not in text
        else:
            assert f"{prefix}初始纪要\n{prefix}后期追加" in text
            assert f"阵营口令：{prefix}旧口令" not in text
            if no == 21:
                assert f"阵营口令：{prefix}新口令" in text
    if no <= 20:
        assert "资历级别：旧阶名" in text and "资历级别：新阶名" not in text
        assert "参照档案：旧参照职业（职业）" in text
        assert "参照档案：新参照职业（职业）" not in text
    elif no == 21:
        assert "资历级别：新阶名" in text and "资历级别：旧阶名" not in text
        assert "参照档案：新参照职业（职业）" in text
        assert "参照档案：旧参照职业（职业）" not in text
    else:
        assert "阵营口令：" not in text and "资历级别：" not in text and "参照档案：" not in text
    assert "旧版备注：角色旧字段仍可读" in text
    assert "引用目标正文禁止注入" not in text and "引用规则禁止注入" not in text
    assert "未选职业初始纪要" not in text
    assert "reference_handle_id" not in text and "option_id" not in text
    if analysis:
        # 分析原本列项目全部组织，世界观原本不注入；字段继承该范围。
        assert "未选组织初始纪要" in text
        assert "世界初始纪要" not in text and "原始世界正文" not in text
    else:
        assert "未选组织初始纪要" not in text
        assert "旧版备注：世界旧字段仍可读" in text


@pytest.mark.parametrize("no", [5, 20, 21, 22])
@pytest.mark.parametrize("planning", [False, True], ids=["ordinary", "planning"])
async def test_generation_messages_and_previews_share_typed_fields(app, client, fake_llm, no, planning):
    data = await _seed(app, client, planning=planning, legacy_override=planning)
    cid = data["chapters"][no]
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["pid"])
        chapter = await db.get(Chapter, cid)
        if planning:
            preview = await planning_context_service.load_planning_context(db, project, "chapter", cid, budget=20000)
            _assert_fields(preview.text, no)
        else:
            preview = await context_builder.build_setting_context(db, project, chapter, budget=20000)
            _assert_fields(preview, no)
        assert await db.scalar(select(func.count()).select_from(OrganizationMember)) == 1
        assert await db.scalar(select(func.count()).select_from(CharacterCareer)) == 1
    fake_llm.push("生成正文。")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{cid}/generate", json={"target_words": 300}))
    assert task["status"] == "succeeded", task
    prompt = _prompt(fake_llm.calls[-1])
    _assert_fields(prompt, no)
    assert prompt.count("成长纪要：角色初始纪要") == 1
    if planning:
        assert "旧模板标记" in prompt


@pytest.mark.parametrize("op", ["rewrite", "expand", "condense", "deslop", "continue"])
@pytest.mark.parametrize("no", [5, 21])
async def test_local_operations_actual_messages_keep_typed_story_time(app, client, fake_llm, op, no):
    data = await _seed(app, client, legacy_override=True)
    content = "林远走进旧城。"
    end = len(content)
    fake_llm.push("局部输出。")
    response = await client.post(
        f"/api/v1/chapters/{data['chapters'][no]}/rewrite",
        json={
            "op": op,
            "start": end if op == "continue" else 0,
            "end": end,
        },
    )
    assert response.status_code == 200, response.text
    assert parse_sse(response.text)[-1][0] == "done", response.text
    prompt = _prompt(fake_llm.calls[-1])
    _assert_fields(prompt, no)
    assert "旧模板标记" in prompt and "【本章有效设定】" in prompt


@pytest.mark.parametrize("no", [5, 20, 21, 22])
async def test_analysis_messages_use_fields_without_expanding_archive_scope(app, client, fake_llm, no):
    data = await _seed(app, client)
    fake_llm.push(analysis_json())
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{data['chapters'][no]}/analyze"))
    assert task["status"] == "succeeded", task
    _assert_fields(_prompt(fake_llm.calls[-1]), no, analysis=True)


async def test_revise_and_automatic_continuation_use_the_same_field_snapshot(app, client, fake_llm):
    data = await _seed(app, client, planning=True)
    cid = data["chapters"][5]
    fake_llm.push(analysis_json())
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{cid}/analyze"))
    assert task["status"] == "succeeded", task
    analyses = (await client.get(f"/api/v1/chapters/{cid}/analyses")).json()
    fake_llm.push("重写正文。")
    task, _ = await run_task(
        client,
        await client.post(
            f"/api/v1/chapters/{cid}/revise",
            json={
                "analysis_id": analyses[0]["id"],
                "instruction": "调整句子",
            },
        ),
    )
    assert task["status"] == "succeeded", task
    _assert_fields(_prompt(fake_llm.calls[-1]), 5)
    start = len(fake_llm.calls)
    fake_llm.push("开" * 50)
    fake_llm.push("续" * 300)
    task, _ = await run_task(
        client,
        await client.post(
            f"/api/v1/chapters/{cid}/generate",
            json={
                "target_words": 300,
                "auto_continue": True,
            },
        ),
    )
    assert task["status"] == "succeeded", task
    assert len(fake_llm.calls) == start + 2
    for call in fake_llm.calls[start:]:
        _assert_fields(_prompt(call), 5)


async def test_batch_generation_resolves_each_chapter_fields(app, client, fake_llm):
    data = await _seed(app, client)
    for _ in range(3):
        fake_llm.push("林远继续前行。")
    task, _ = await run_task(
        client,
        await client.post(
            f"/api/v1/projects/{data['pid']}/batch-generate",
            json={
                "start_chapter_id": data["chapters"][5],
                "end_chapter_id": data["chapters"][21],
                "include_written": True,
                "auto_continue": False,
                "target_words": 300,
            },
        ),
    )
    assert task["status"] == "succeeded", task
    assert len(fake_llm.calls) == 3
    for no, call in zip((5, 20, 21), fake_llm.calls, strict=True):
        _assert_fields(_prompt(call), no)


async def test_character_reference_does_not_add_a_full_cast_card(app, client, fake_llm):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        target = Character(
            project_id=data["pid"], name="被引用配角", role_type="配角", profile="引用角色正文禁止注入", ai_mode=None,
        )
        definition = await db.scalar(
            select(SettingFieldDefinition).where(
                SettingFieldDefinition.project_id == data["pid"],
                SettingFieldDefinition.field_type == "reference",
            )
        )
        definition.reference_types = ["career", "character"]
        db.add(target)
        await db.flush()
        handle = SettingReferenceHandle(project_id=data["pid"], kind="character", character_id=target.id)
        db.add(handle)
        await db.flush()
        field = await db.scalar(
            select(SettingTextField)
            .join(SettingTimeline)
            .where(
                SettingTimeline.project_id == data["pid"],
                SettingTimeline.subject_kind == "character",
                SettingTextField.definition_id == definition.id,
            )
        )
        field.base_value = {"reference_handle_id": handle.id}
        await db.commit()
    fake_llm.push("林远继续前行。")
    task, _ = await run_task(
        client,
        await client.post(
            f"/api/v1/chapters/{data['chapters'][5]}/generate",
            json={"target_words": 300},
        ),
    )
    assert task["status"] == "succeeded", task
    prompt = _prompt(fake_llm.calls[-1])
    assert "参照档案：被引用配角（角色）" in prompt
    assert "■ 被引用配角" not in prompt and "引用角色正文禁止注入" not in prompt
    assert "【其他角色（仅名单）】\n被引用配角（配角）" in prompt


async def test_planned_without_real_chapter_does_not_invent_effective_fields(app, client):
    data = await _seed(app, client, planning=True)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["pid"])
        preview = await planning_context_service.load_planning_context(
            db, project, "plan", data["plan_id"], budget=20000
        )
        assert "视角人物" in preview.text and "指定世界观" in preview.text
        assert "成长纪要" not in preview.text and "资历级别" not in preview.text
        assert preview.warnings


@pytest.mark.parametrize(
    ("entry_type", "kind", "label"),
    [
        ("character", "character", "角色"),
        ("organization", "organization", "组织"),
        ("career", "career", "职业"),
        ("world.place", "world", "世界观·地点"),
        ("world.faction", "world", "世界观·势力"),
        ("world.item", "world", "世界观·物品"),
        ("world.rule", "world", "世界观·规则"),
        ("world.history", "world", "世界观·历史"),
        ("world.other", "world", "世界观·其他"),
    ],
)
def test_formatter_uses_public_names_and_keeps_archived_values(entry_type, kind, label):
    target = SettingEntry(reference=EntityRef(kind=kind, id=42), type=entry_type, name="当前显示名")
    fields = EntryFieldsOut(
        entry=SettingEntry(reference=EntityRef(kind="character", id=1), type="character", name="林远"),
        timeline_id=1,
        timeline_revision=0,
        fields_revision=0,
        position=None,
        fields=[
            FieldSlot(
                field_id=1,
                definition_id=1,
                label="文本",
                field_type="text",
                source="shared",
                base_value="旧",
                value=" 起点\n追加 ",
            ),
            FieldSlot(
                field_id=2,
                definition_id=2,
                label="单行",
                field_type="line",
                source="shared",
                base_value="",
                value="口令",
            ),
            FieldSlot(
                field_id=3,
                definition_id=3,
                label="下拉",
                field_type="dropdown",
                source="shared",
                base_value=None,
                value={"option_id": 8},
                archived=True,
                options=[OptionOut(id=8, definition_id=3, label="停用名称", sort=0, archived=True)],
            ),
            FieldSlot(
                field_id=4,
                definition_id=4,
                label="引用",
                field_type="reference",
                source="shared",
                base_value=None,
                value=target.reference.model_dump(),
                reference=target,
            ),
            FieldSlot(
                field_id=None,
                definition_id=5,
                label="空引用",
                field_type="reference",
                source="shared",
                base_value=None,
                value=None,
            ),
            FieldSlot(
                field_id=None,
                definition_id=6,
                label="空文本",
                field_type="text",
                source="shared",
                base_value="",
                value="  ",
            ),
        ],
        legacy_fields=[
            FieldSlot(
                field_id=7,
                definition_id=None,
                label="原有",
                field_type="text",
                source="legacy",
                base_value="旧",
                value="兼容值",
            )
        ],
    )
    before = fields.model_dump()
    assert format_entry_fields(fields) == (
        f"文本：起点\n追加\n单行：口令\n下拉：停用名称\n引用：当前显示名（{label}）\n原有：兼容值"
    )
    assert fields.model_dump() == before


@pytest.mark.parametrize("builder", ["chapter", "setting"])
async def test_typed_fields_are_trimmed_inside_the_existing_context_budget(app, client, builder):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        field = await db.scalar(
            select(SettingTextField)
            .join(SettingTimeline)
            .join(SettingFieldDefinition)
            .where(
                SettingTimeline.subject_kind == "character",
                SettingFieldDefinition.field_type == "text",
            )
        )
        field.base_value = "超长字段正文" * 1000
        await db.flush()
        project = await db.get(Project, data["pid"])
        chapter = await db.get(Chapter, data["chapters"][5])
        if builder == "chapter":
            text = await context_builder.build_chapter_context(db, project, chapter, 1000)
        else:
            text = await context_builder.build_setting_context(db, project, chapter, budget=1000)
        assert len(text) <= 1000
        assert "成长纪要：" in text


@pytest.mark.parametrize("builder", ["chapter", "setting", "planning", "analysis"])
async def test_context_rejects_catalog_changes_between_projection_batches(app, client, monkeypatch, builder):
    data = await _seed(app, client, planning=builder == "planning")
    resolve = setting_field_service.resolve_entry_fields
    changed = False

    async def change_after_first_batch(db, pid, entries, position=None):
        nonlocal changed
        result = await resolve(db, pid, entries, position)
        if not changed:
            changed = True
            # 屏障放在一次完整且已验证的批量读取之后，模拟下一批开始前的目录提交。
            await db.execute(
                update(Project).where(Project.id == pid).values(fields_revision=Project.fields_revision + 1)
            )
            await db.execute(
                update(SettingFieldDefinition)
                .where(
                    SettingFieldDefinition.project_id == pid,
                    SettingFieldDefinition.field_type == "text",
                )
                .values(label="重命名后的纪要")
            )
        return result

    monkeypatch.setattr(setting_field_service, "resolve_entry_fields", change_after_first_batch)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["pid"])
        chapter = await db.get(Chapter, data["chapters"][5])
        with pytest.raises(ConflictError) as error:
            if builder == "chapter":
                await context_builder.build_chapter_context(db, project, chapter, 20000)
            elif builder == "setting":
                await context_builder.build_setting_context(db, project, chapter, budget=20000)
            elif builder == "planning":
                await planning_context_service.load_planning_context(db, project, "chapter", chapter.id, budget=20000)
            else:
                await analysis_service.build_analysis_job(
                    db,
                    user_id=project.user_id,
                    chapter_id=chapter.id,
                    ai_config_id=None,
                    cipher=app.state.cipher,
                    factory=app.state.provider_factory,
                    ai_timeout=30,
                    allow_private=False,
                )
        assert error.value.extra["code"] == "stale_revision"
        assert error.value.extra["scope"] == "fields"


@pytest.mark.parametrize("builder", ["chapter", "setting"])
async def test_ordinary_context_rejects_a_different_catalog_than_its_planning_block(app, client, builder):
    data = await _seed(app, client, planning=True)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["pid"])
        chapter = await db.get(Chapter, data["chapters"][5])
        planning = await planning_context_service.load_planning_context(
            db, project, "chapter", chapter.id, budget=20000
        )
        await db.execute(
            update(Project).where(Project.id == project.id).values(fields_revision=Project.fields_revision + 1)
        )
        with pytest.raises(ConflictError) as error:
            if builder == "chapter":
                await context_builder.build_chapter_context(db, project, chapter, 20000, planning=planning)
            else:
                await context_builder.build_setting_context(db, project, chapter, budget=20000, planning=planning)
        assert error.value.extra["code"] == "stale_revision"
        assert error.value.extra["scope"] == "fields"
