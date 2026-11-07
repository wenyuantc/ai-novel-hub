import pytest
from sqlalchemy import func, select, update

from app.ai.prompts.chapter import build_chapter_messages, build_revise_messages
from app.ai.prompts.planning import PlanningTextItem
from app.ai.prompts.registry import get_template
from app.ai.prompts.rewrite import build_rewrite_messages
from app.ai.templating import PromptOverride
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import (
    Career,
    CareerStage,
    Chapter,
    ChapterCharacter,
    ChapterPlan,
    ChapterVersion,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    PromptTemplateOverride,
    RelationshipType,
    SettingProgression,
    SettingTimeline,
    WorldSetting,
)
from app.models.planning import (
    PlanningTag,
    PlanningUnit,
    PlanningUnitCareer,
    PlanningUnitOrganization,
    PlanningUnitRelationship,
    PlanningUnitTag,
    PlanningUnitWorld,
    Subplot,
    SubplotCrossing,
    SubplotCrossingParticipant,
    SubplotNode,
)
from app.services import context_builder, planning_context_service, scene_service
from tests.helpers import Gate, analysis_json, create_ai_config, create_project, parse_sse, register, run_task


@pytest.mark.parametrize("kind", ["generate", "revise", "rewrite"])
def test_planning_block_survives_legacy_overrides_once(kind):
    text = "【本章规划】\n视角人物：林远\n明确交汇：追凶线与寻亲线在本章相遇。"
    kwargs = {
        "generate": {"context": "参考", "chapter_no": 5, "title": "汇合", "summary": "", "target_words": 300},
        "revise": {
            "context": "参考",
            "chapter_no": 5,
            "title": "汇合",
            "content": "原文",
            "suggestions": [],
            "target_words": 300,
        },
        "rewrite": {"op": "rewrite", "selected": "原文", "before": "", "after": ""},
    }[kind]
    build = {"generate": build_chapter_messages, "revise": build_revise_messages, "rewrite": build_rewrite_messages}[
        kind
    ]
    template = get_template(f"chapter.{kind}")
    override = PromptOverride(system=template.system, user=template.user.replace("{{ planning_context }}", ""))
    messages = build(**kwargs, planning_context=text, override=override)
    combined = "\n".join(message["content"] for message in messages)
    assert combined.count(text) == 1
    assert any(v.name == "planning_context" and v.locked for v in template.variables)
    assert build(**kwargs, planning_context="") == build(**kwargs)


async def _seed(app, client, *, content="需要改写的原文。"):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    pid = project["id"]
    async with app.state.sessionmaker() as db:
        hero = Character(project_id=pid, name="林远", role_type="配角", current_state="如今是圣者")
        partner = Character(project_id=pid, name="苏晚", role_type="配角", current_state="寻访旧友")
        world = WorldSetting(project_id=pid, title="旧城", category="place", content="如今城门已毁")
        org = Organization(project_id=pid, name="观星阁", purpose="观测星象", description="静态组织资料标记")
        career = Career(
            project_id=pid, name="符师", kind="main", description="静态职业资料标记", rules="符术不能复活死者"
        )
        chapters = {
            n: Chapter(project_id=pid, chapter_no=n, title=f"第{n}章", content=content if n == 5 else "")
            for n in (5, 15, 25)
        }
        outline = Outline(project_id=pid, title="未建章大纲", chapter_no=26, volume_no=1)
        subplots = [
            Subplot(project_id=pid, name=name, description="作者结局概览禁止注入")
            for name in ("追凶线", "寻亲线", "破局线")
        ]
        db.add_all([hero, partner, world, org, career, outline, *chapters.values(), *subplots])
        await db.flush()
        for chapter in chapters.values():
            await scene_service.initialize_chapter(db, chapter)
        db.add(CareerStage(career_id=career.id, name="入门", position=1, description="能读基础符文"))
        plan = ChapterPlan(project_id=pid, outline_id=outline.id, seq=1, title="将来细纲", character_ids=[])
        db.add(plan)
        await db.flush()
        friendship = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "朋友"))
        hero_base = {
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
        hero_base["current_state"] = "还是学徒"
        hero_line = SettingTimeline(project_id=pid, subject_kind="character", character_id=hero.id, baseline=hero_base)
        world_line = SettingTimeline(
            project_id=pid, subject_kind="world", world_setting_id=world.id, baseline={"content": "旧城门完好"}
        )
        relation = SettingTimeline(
            project_id=pid,
            subject_kind="relationship",
            character_id=hero.id,
            related_character_id=partner.id,
            baseline={"exists": False},
        )
        db.add_all([hero_line, world_line, relation])
        await db.flush()

        def event(timeline, field, value, no=20):
            return SettingProgression(
                timeline_id=timeline.id,
                field_key=field,
                operation="replace",
                value=value,
                anchor_kind="planned",
                chapter_no=no,
                boundary="start",
                source="manual",
            )

        db.add_all(
            [
                event(hero_line, "current_state", "如今是圣者"),
                event(world_line, "content", "如今城门已毁"),
                event(
                    relation,
                    "presence",
                    {
                        "exists": True,
                        "state": {
                            "identity": {"a_id": hero.id, "b_id": partner.id, "type_id": friendship.id},
                            "intimacy": 70,
                            "status": "normal",
                            "description": "两人曾于星桥盟誓",
                        },
                    },
                    10,
                ),
                event(relation, "presence", {"exists": False}, 20),
            ]
        )
        units = {
            n: PlanningUnit(project_id=pid, chapter_id=ch.id, pov_character_id=hero.id if n == 5 else None)
            for n, ch in chapters.items()
        }
        planned = PlanningUnit(project_id=pid, plan_id=plan.id, pov_character_id=hero.id)
        detached = PlanningUnit(project_id=pid, last_source_label="已删旧章")
        db.add_all([*units.values(), planned, detached])
        await db.flush()
        db.add_all(
            [
                PlanningUnitWorld(unit_id=units[5].id, world_id=world.id),
                PlanningUnitOrganization(unit_id=units[5].id, organization_id=org.id),
                PlanningUnitCareer(unit_id=units[5].id, career_id=career.id),
                *(PlanningUnitRelationship(unit_id=unit.id, timeline_id=relation.id) for unit in units.values()),
            ]
        )
        current_node = SubplotNode(unit_id=units[5].id, subplot_id=subplots[0].id, content="本章先找到密钥")
        future_node = SubplotNode(unit_id=units[25].id, subplot_id=subplots[0].id, content="后续节点专属禁入标记")
        db.add_all(
            [
                current_node,
                future_node,
                SubplotNode(unit_id=planned.id, subplot_id=subplots[0].id, content="未建章节点专属禁入标记"),
                SubplotNode(unit_id=detached.id, subplot_id=subplots[1].id, content="失联节点专属禁入标记"),
            ]
        )
        crossing = SubplotCrossing(
            project_id=pid, unit_id=units[5].id, title="渡口汇合", description="三方共同发现旧信"
        )
        later = SubplotCrossing(
            project_id=pid, unit_id=units[25].id, title="后续交汇专属禁入标记", description="以后再说"
        )
        db.add_all([crossing, later])
        await db.flush()
        db.add_all([SubplotCrossingParticipant(crossing_id=crossing.id, subplot_id=s.id) for s in subplots])
        db.add_all([SubplotCrossingParticipant(crossing_id=later.id, subplot_id=s.id) for s in subplots[:2]])
        await db.commit()
        return {
            "project_id": pid,
            "chapters": {n: ch.id for n, ch in chapters.items()},
            "plan_id": plan.id,
            "units": {n: unit.id for n, unit in units.items()},
            "planned_unit": planned.id,
            "detached_unit": detached.id,
            "hero": hero.id,
            "partner": partner.id,
            "relation": relation.id,
            "node": current_node.id,
            "crossing": crossing.id,
            "subplot_ids": [s.id for s in subplots],
            "content": content,
        }


def _prompt(call):
    return "\n".join(message["content"] for message in call["messages"])


def _assert_current_planning(prompt):
    assert "本章先找到密钥" in prompt
    assert prompt.count("三方共同发现旧信") == 1
    assert "追凶线、寻亲线、破局线" in prompt
    assert "视角人物" in prompt and "林远" in prompt
    assert "当前状态：还是学徒" in prompt
    assert "旧城门完好" in prompt
    for forbidden in (
        "后续节点专属禁入标记",
        "后续交汇专属禁入标记",
        "未建章节点专属禁入标记",
        "失联节点专属禁入标记",
        "作者结局概览禁止注入",
        "如今是圣者",
        "如今城门已毁",
        "两人曾于星桥盟誓",
    ):
        assert forbidden not in prompt


async def test_generation_actual_messages_include_only_current_planning_and_all_references(app, client, fake_llm):
    data = await _seed(app, client)
    fake_llm.push("生成的正文。")
    task, _ = await run_task(
        client, await client.post(f"/api/v1/chapters/{data['chapters'][5]}/generate", json={"target_words": 300})
    )
    assert task["status"] == "succeeded", task
    prompt = _prompt(fake_llm.calls[-1])
    _assert_current_planning(prompt)
    assert prompt.count("当前状态：还是学徒") == 1
    assert prompt.count("旧城门完好") == 1
    assert prompt.count("静态组织资料标记") == 1
    assert prompt.count("静态职业资料标记") == 1
    assert "符术不能复活死者" in prompt
    base = f"/api/v1/projects/{data['project_id']}/chapters/{data['chapters'][5]}"
    logs = (await client.get(base + "/generation-logs")).json()
    snapshot = logs[0]["params"]["planning"]
    assert snapshot["node_ids"] == [data["node"]]
    assert snapshot["crossing_participants"][str(data["crossing"])] == data["subplot_ids"]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(ChapterCharacter)) == 0
        assert await db.scalar(select(func.count()).select_from(CharacterCareer)) == 0
        assert await db.scalar(select(func.count()).select_from(OrganizationMember)) == 0


@pytest.mark.parametrize("op", ["rewrite", "expand", "condense", "deslop", "continue"])
async def test_local_operations_receive_planning_as_background_only(app, client, fake_llm, op):
    data = await _seed(app, client)
    end = len(data["content"])
    fake_llm.push("局部输出。")
    response = await client.post(
        f"/api/v1/chapters/{data['chapters'][5]}/rewrite",
        json={
            "op": op,
            "start": end if op == "continue" else 0,
            "end": end,
        },
    )
    assert response.status_code == 200, response.text
    assert parse_sse(response.text)[-1][0] == "done", response.text
    prompt = _prompt(fake_llm.calls[-1])
    _assert_current_planning(prompt)
    assert "不把全部节点或整场交汇强塞进本段" in prompt
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, data["chapters"][5])).content == data["content"]


async def test_revise_and_automatic_continuation_use_the_actual_planning_snapshot(app, client, fake_llm):
    data = await _seed(app, client)
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
    _assert_current_planning(_prompt(fake_llm.calls[-1]))
    fake_llm.push("开" * 50)
    fake_llm.push("续" * 300)
    start = len(fake_llm.calls)
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
        _assert_current_planning(_prompt(call))


async def test_relationship_reference_is_effective_at_target_chapter_even_without_current_carrier(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["project_id"])
        previews = {
            n: await planning_context_service.load_planning_context(db, project, "chapter", cid, budget=10000)
            for n, cid in data["chapters"].items()
        }
        assert "两人曾于星桥盟誓" not in previews[5].text
        assert "两人曾于星桥盟誓" in previews[15].text
        assert "两人曾于星桥盟誓" not in previews[25].text
        assert "本章开始时没有有效关系" in previews[5].text
        assert "本章开始时没有有效关系" in previews[25].text
        assert "关系参考人物（不额外要求出场）" in previews[15].text
        normal = await context_builder.build_chapter_context(
            db, project, await db.get(Chapter, data["chapters"][15]), 10000, planning=previews[15]
        )
        assert "两人曾于星桥盟誓" not in normal  # 已在锁定的显式关系块中，只出现一次。


async def test_preview_unbuilt_and_detached_is_read_only_and_has_no_estimated_history(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["project_id"])
        count = await db.scalar(select(func.count()).select_from(PlanningUnit))
        project.title = "尚未保存的标题"
        planned = await planning_context_service.context_preview(db, project, "plan", data["plan_id"])
        detached = await planning_context_service.context_preview(db, project, "detached", data["detached_unit"])
        assert planned.chapter_id is None and planned.plan_id == data["plan_id"]
        assert "未建章节点专属禁入标记" in planned.text
        assert "还是学徒" not in planned.text and "如今是圣者" not in planned.text
        assert planned.warnings and detached.warnings
        assert "失联节点专属禁入标记" in detached.text
        assert project in db.dirty
        with db.no_autoflush:
            assert await db.scalar(select(func.count()).select_from(PlanningUnit)) == count
            assert await db.scalar(select(Project.title).where(Project.id == project.id)) != project.title
        unit = await db.get(PlanningUnit, data["detached_unit"])
        unit.retired = True
        await db.flush()
        with pytest.raises(NotFoundError):
            await planning_context_service.context_preview(db, project, "detached", unit.id)


def test_planning_budget_preserves_each_identity_and_never_silently_drops_a_goal():
    items = [PlanningTextItem(f"支线节点 #{i}：目标{i}", str(i) * 2000) for i in range(1, 5)]
    text, truncated = planning_context_service.fit_planning_context(items, scope="chapter", budget=1000)
    assert truncated and len(text) <= 1000
    for item in items:
        assert item.title in text
        assert item.body[:10] in text
    assert "已截断" in text
    with pytest.raises(BadRequestError, match="预算不足"):
        planning_context_service.fit_planning_context(items, scope="selection", budget=20)


async def test_only_tags_preserves_old_context_and_only_crossing_still_has_planning(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["project_id"])
        chapter = Chapter(project_id=project.id, chapter_no=30, title="仅标签")
        tag = PlanningTag(project_id=project.id, name="待修订")
        db.add_all([chapter, tag])
        await db.flush()
        unit = PlanningUnit(project_id=project.id, chapter_id=chapter.id)
        db.add(unit)
        await db.flush()
        db.add(PlanningUnitTag(unit_id=unit.id, tag_id=tag.id))
        await db.flush()
        baseline = await context_builder.build_chapter_context(db, project, chapter, 4000)
        planning = await planning_context_service.load_planning_context(db, project, "chapter", chapter.id)
        assert planning.text == "" and planning.reserved_chars == 0
        assert await context_builder.build_chapter_context(db, project, chapter, 4000, planning=planning) == baseline
        event = SubplotCrossing(
            project_id=project.id, unit_id=unit.id, title="单独事件", description="没有普通节点仍是交汇"
        )
        db.add(event)
        await db.flush()
        db.add_all([SubplotCrossingParticipant(crossing_id=event.id, subplot_id=sid) for sid in data["subplot_ids"]])
        await db.flush()
        planning = await planning_context_service.load_planning_context(db, project, "chapter", chapter.id)
        assert planning.node_ids == []
        assert planning.crossing_ids == [event.id]
        assert planning.text.count("没有普通节点仍是交汇") == 1


async def test_batch_reads_each_chapters_own_planning(app, client, fake_llm):
    data = await _seed(app, client, content="")
    for text in ("第一段正文", "第二段正文", "第三段正文"):
        fake_llm.push(text)
    task, _ = await run_task(
        client,
        await client.post(
            f"/api/v1/projects/{data['project_id']}/batch-generate",
            json={
                "start_chapter_id": data["chapters"][5],
                "end_chapter_id": data["chapters"][25],
                "auto_continue": False,
            },
        ),
    )
    assert task["status"] == "succeeded", task
    assert len(fake_llm.calls) == 3
    first, middle, last = map(_prompt, fake_llm.calls)
    _assert_current_planning(first)
    assert "本章先找到密钥" not in middle
    assert "两人曾于星桥盟誓" in middle
    assert "后续节点专属禁入标记" in last and "后续交汇专属禁入标记" in last
    assert "三方共同发现旧信" not in last


async def test_real_legacy_override_cannot_drop_explicit_reference_or_planning(app, client, fake_llm):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["project_id"])
        # 模拟升级前合法保存的覆盖：仅取普通 context 首字，缺少新锁定变量。
        db.add(
            PromptTemplateOverride(
                user_id=project.user_id,
                key="chapter.generate",
                system="请创作。",
                user="旧模板标记：{{ context|first }} {{ summary }}",
            )
        )
        await db.commit()
    fake_llm.push("旧模板生成正文")
    task, _ = await run_task(
        client,
        await client.post(
            f"/api/v1/chapters/{data['chapters'][5]}/generate",
            json={"target_words": 300},
        ),
    )
    assert task["status"] == "succeeded", task
    prompt = _prompt(fake_llm.calls[-1])
    _assert_current_planning(prompt)
    assert "旧模板标记" in prompt
    assert "静态组织资料标记" in prompt and "静态职业资料标记" in prompt


async def test_planning_preview_is_locked_and_new_override_must_keep_it(client):
    await register(client)
    for kind in ("generate", "revise", "rewrite"):
        template = get_template(f"chapter.{kind}")
        url = f"/api/v1/prompt-templates/chapter.{kind}"
        response = await client.post(
            url + "/preview",
            json={
                "system": template.system,
                "user": template.user,
                "variables": {"planning_context": "客户端伪造规划"},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["errors"] == []
        assert "客户端伪造规划" not in _prompt(response.json())
        assert "【本章规划】" in _prompt(response.json())
        response = await client.put(
            url,
            json={
                "system": template.system,
                "user": template.user.replace("{{ planning_context }}", ""),
            },
        )
        assert response.status_code == 400 and "planning_context" in response.text


async def test_auto_continue_rejects_changed_planning_snapshot_before_second_send(app, client, fake_llm):
    data = await _seed(app, client)
    gate = Gate()
    fake_llm.push(["首" * 50, gate])
    fake_llm.push("续" * 300)
    response = await client.post(
        f"/api/v1/chapters/{data['chapters'][5]}/generate",
        json={
            "target_words": 300,
            "auto_continue": True,
        },
    )
    await gate.wait_reached()
    try:
        async with app.state.sessionmaker() as db:
            node = await db.get(SubplotNode, data["node"])
            node.content = "任务中途改成另一规划"
            await db.execute(
                update(Project)
                .where(Project.id == data["project_id"])
                .values(planning_revision=Project.planning_revision + 1)
            )
            await db.commit()
    finally:
        gate.release()
    task, _ = await run_task(client, response)
    # 场景设计 §8 同时保护后续真实发送及最终采纳，不能给旧规划续签。
    assert task["status"] == "failed", task
    assert len(fake_llm.calls) == 1
    for call in fake_llm.calls:
        _assert_current_planning(_prompt(call))
        assert "任务中途改成另一规划" not in _prompt(call)
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, data["chapters"][5])
        assert chapter.content == "需要改写的原文。"
        assert [scene.content for scene in await scene_service.list_active_scenes(db, chapter.id)] == [
            "需要改写的原文。",
        ]
        assert list(await db.scalars(select(ChapterVersion.id).where(ChapterVersion.chapter_id == chapter.id))) == []


async def test_budget_accounts_for_protected_block_and_reports_truncation(app, client):
    data = await _seed(app, client)
    async with app.state.sessionmaker() as db:
        node = await db.get(SubplotNode, data["node"])
        node.content = "长节点内容" * 400
        crossing = await db.get(SubplotCrossing, data["crossing"])
        crossing.description = "长交汇内容" * 400
        await db.flush()
        project = await db.get(Project, data["project_id"])
        chapter = await db.get(Chapter, data["chapters"][5])
        planning = await planning_context_service.load_planning_context(db, project, "chapter", chapter.id, budget=1600)
        ordinary = await context_builder.build_chapter_context(
            db,
            project,
            chapter,
            1600 - planning.reserved_chars,
            planning=planning,
        )
        assert len(ordinary) + planning.reserved_chars <= 1600
        assert planning.truncated and planning.warnings
        assert "视角人物" in planning.text and "明确交汇" in planning.text and "支线目标" in planning.text


async def test_context_does_not_pair_stale_data_with_new_revision(app, client, monkeypatch):
    data = await _seed(app, client)
    original = planning_context_service._story_items

    async def changed(db, project, unit, out):
        items = await original(db, project, unit, out)
        await db.execute(
            update(Project).where(Project.id == project.id).values(planning_revision=Project.planning_revision + 1)
        )
        return items

    monkeypatch.setattr(planning_context_service, "_story_items", changed)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError, match="读取期间发生变化"):
            await planning_context_service.load_planning_context(
                db, await db.get(Project, data["project_id"]), "chapter", data["chapters"][5]
            )
