"""覆盖向导删除目标时不得级联改写其他作品的损坏引用。"""

import pytest

from app.core.errors import ConflictError
from app.models import (
    Career,
    CareerStage,
    Chapter,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    PlanningUnit,
    PlanningUnitWorld,
    SettingProgression,
    SettingReferenceHandle,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
)
from app.services import wizard_service
from tests.helpers import create_project, register


@pytest.mark.parametrize("kind", ["characters", "world", "careers", "outline"])
async def test_wizard_replacement_refuses_cross_project_cascades(app, client, kind):
    await register(client)
    original_project = await create_project(client, title="本书")
    other_project = await create_project(client, title="另一书")
    pid, other_pid = original_project["id"], other_project["id"]
    async with app.state.sessionmaker() as db:
        if kind == "characters":
            target = Character(project_id=pid, name="本书人物")
            org = Organization(project_id=other_pid, name="其他书组织")
            db.add_all([target, org])
            await db.flush()
            dependent = OrganizationMember(organization_id=org.id, character_id=target.id)
            raw = '[{"name":"新角色"}]'
        elif kind == "world":
            target = WorldSetting(project_id=pid, title="本书世界")
            unit = PlanningUnit(project_id=other_pid)
            db.add_all([target, unit])
            await db.flush()
            dependent = PlanningUnitWorld(unit_id=unit.id, world_id=target.id)
            raw = '[{"title":"新世界"}]'
        elif kind == "careers":
            target = Career(project_id=pid, name="本书职业")
            foreign = Character(project_id=other_pid, name="其他书人物")
            db.add_all([target, foreign])
            await db.flush()
            stage = CareerStage(career_id=target.id, position=1, name="起步")
            db.add(stage)
            await db.flush()
            dependent = CharacterCareer(character_id=foreign.id, career_id=target.id, role="main", stage_id=stage.id)
            raw = '[{"name":"新职业","stages":[{"name":"新阶段"}]}]'
        else:
            target = Outline(project_id=pid, title="本书大纲")
            db.add(target)
            await db.flush()
            dependent = Chapter(project_id=other_pid, outline_id=target.id, title="其他书正文", content="不能改")
            raw = '[{"title":"新大纲"}]'
        db.add(dependent)
        await db.commit()
        target_id = target.id
        dependent_identity = tuple(getattr(dependent, col.name) for col in dependent.__table__.primary_key.columns)

    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await wizard_service.save_wizard_result(db, pid, kind, raw)
        # 调用方不应依赖隐式连接关闭来回滚前半次删除。
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.get(type(target), target_id) is not None
        restored = await db.get(type(dependent), dependent_identity)
        assert restored is not None
        if kind == "outline":
            assert restored.outline_id == target_id and restored.content == "不能改"


@pytest.mark.parametrize("broken", ["foreign_subject", "foreign_base", "foreign_history", "foreign_supersession"])
async def test_wizard_does_not_damage_cross_project_timeline_data(app, client, broken):
    await register(client)
    project = await create_project(client, title="本书")
    other = await create_project(client, title="另一书")
    async with app.state.sessionmaker() as db:
        target = Character(project_id=project["id"], name="本书人物")
        foreign = Character(project_id=other["id"], name="其他书人物")
        db.add_all([target, foreign])
        await db.flush()
        local_timeline = SettingTimeline(project_id=project["id"], subject_kind="character", character_id=target.id)
        foreign_timeline = SettingTimeline(project_id=other["id"], subject_kind="character", character_id=foreign.id)
        db.add_all([local_timeline, foreign_timeline])
        await db.flush()
        if broken == "foreign_subject":
            local_timeline.subject_kind = "relationship"
            local_timeline.character_id, local_timeline.related_character_id = sorted([target.id, foreign.id])
        elif broken in ("foreign_base", "foreign_history"):
            handle = SettingReferenceHandle(project_id=project["id"], kind="character", character_id=target.id)
            db.add(handle)
            await db.flush()
            value = {"reference_handle_id": handle.id}
            if broken == "foreign_base":
                db.add(SettingTextField(timeline_id=foreign_timeline.id, label="跨书引用", base_value=value))
            else:
                db.add(SettingProgression(timeline_id=foreign_timeline.id, field_key="profile", value=value,
                                          operation="replace", anchor_kind="planned", chapter_no=1, active=False))
        else:
            local_event = SettingProgression(timeline_id=local_timeline.id, field_key="profile", value="旧值",
                                             operation="replace", anchor_kind="planned", chapter_no=1, active=False)
            db.add(local_event)
            await db.flush()
            db.add(SettingProgression(timeline_id=foreign_timeline.id, field_key="profile", value="历史",
                                      operation="replace", anchor_kind="planned", chapter_no=1, active=False,
                                      superseded_by_id=local_event.id))
        await db.commit()
        target_id, local_id, foreign_id = target.id, local_timeline.id, foreign_timeline.id

    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await wizard_service.save_wizard_result(db, project["id"], "characters", '[{"name":"新角色"}]')
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.get(Character, target_id) is not None
        assert await db.get(SettingTimeline, local_id) is not None
        assert await db.get(SettingTimeline, foreign_id) is not None


async def test_append_outline_does_not_delete_or_validate_unrelated_old_outlines(app, client):
    await register(client)
    project = await create_project(client, title="本书")
    other = await create_project(client, title="另一书")
    async with app.state.sessionmaker() as db:
        target = Outline(project_id=project["id"], title="本书旧大纲")
        db.add(target)
        await db.flush()
        chapter = Chapter(project_id=other["id"], outline_id=target.id, title="其他书正文", content="不能改")
        db.add(chapter)
        await db.commit()
        target_id, chapter_id = target.id, chapter.id
    async with app.state.sessionmaker() as db:
        result = await wizard_service.save_wizard_result(
            db, project["id"], "outline", '[{"title":"新增大纲"}]', outline_mode="append"
        )
        assert result["mode"] == "append"
    async with app.state.sessionmaker() as db:
        assert await db.get(Outline, target_id) is not None
        assert (await db.get(Chapter, chapter_id)).outline_id == target_id
