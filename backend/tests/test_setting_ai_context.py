"""从真实入口捕获出站数据，既断言可见正例，也断言结构化旁路不泄漏。"""

import json
from contextlib import aclosing

import pytest

from app.models import (
    Career,
    Chapter,
    Character,
    Organization,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    StoryMemory,
    WorldSetting,
)
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption, SettingReferenceHandle
from app.services import (
    analysis_service,
    entity_ai_service,
    generation_service,
    kb_index_service,
    memory_service,
    outline_ai_service,
    planning_context_service,
    wizard_service,
)
from app.services.retrieval_service import Embedder
from app.services.setting_ai_context import build_scope
from tests.helpers import Gate, analysis_json, create_ai_config, create_project, register, run_task, stream_task
from tests.test_setting_fields_postgresql import (
    postgres_fields_app as postgres_fields_app,
)
from tests.test_setting_fields_postgresql import (
    postgres_fields_client as postgres_fields_client,
)


async def _settings(client, base, kind, ident, **patch):
    route = f"{base}/setting-entries/{kind}/{ident}/ai-settings"
    response = await client.get(route)
    assert response.status_code == 200, response.text
    if not patch:
        return response.json()
    current = response.json()["ai_visibility_revision"]
    response = await client.patch(route, json={**patch, "expected_ai_visibility_revision": current})
    assert response.status_code == 200, response.text
    return response.json()


def _prompt(fake_llm):
    return "\n".join(msg["content"] for msg in fake_llm.calls[-1]["messages"])


async def test_generation_projection_masks_builtin_and_custom_and_does_not_walk_two_hops(app, client, fake_llm):
    await register(client)
    await create_ai_config(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    async with app.state.sessionmaker() as db:
        hero = Character(project_id=pid, name="稻舟", profile="公开角色细节", background="隐藏背景哨兵")
        private = Character(
            project_id=pid, name="档案禁用者", role_type="主角", aliases=["秘密别名"], profile="隐档哨兵",
        )
        org = Organization(project_id=pid, name="渔火堂", purpose="一跳组织正文", description="隐藏组织简介")
        career = Career(project_id=pid, name="跨两跳职业", kind="main", rules="两跳职业正文")
        world = WorldSetting(project_id=pid, title="玻璃港", category="place", content="隐藏地点原文")
        detected = WorldSetting(project_id=pid, title="观测塔", category="item", content="检测地点可见")
        manual = WorldSetting(project_id=pid, title="手选小岛", category="place", content="不因名称注入的标记")
        chapters = [Chapter(project_id=pid, title=f"测试第{n}章", chapter_no=n) for n in (5, 20, 21)]
        db.add_all([hero, private, org, career, world, detected, manual, *chapters])
        await db.flush()
        from app.services.scene_service import initialize_chapter

        for chapter in chapters:
            await initialize_chapter(db, chapter)
        timeline = SettingTimeline(
            project_id=pid, subject_kind="character", character_id=hero.id,
            baseline={
                "profile": hero.profile, "appearance": "", "personality": "", "background": hero.background,
                "current_state": "", "age": "", "gender": "", "life_status": "alive",
            },
        )
        handle = SettingReferenceHandle(project_id=pid, kind="character", character_id=private.id)
        defs = [
            SettingFieldDefinition(
                project_id=pid, label=f"隐藏{kind}标签", field_type=kind, applicable_types=["character"],
                reference_types=["character"] if kind == "reference" else [], ai_hidden=True,
            )
            for kind in ("text", "line", "dropdown", "reference")
        ]
        db.add_all([timeline, handle, *defs])
        await db.flush()
        option = SettingFieldOption(definition_id=defs[2].id, label="隐藏下拉选项")
        visible_ref = SettingFieldDefinition(
            project_id=pid, label="公开字段指向禁用条目", field_type="reference",
            applicable_types=["character"], reference_types=["character"],
        )
        db.add_all([option, visible_ref])
        await db.flush()
        hidden = [
            SettingTextField(timeline_id=timeline.id, definition_id=d.id, base_value=value)
            for d, value in zip(defs, [
                "隐藏多行内容", "隐藏单行内容", {"option_id": option.id}, {"reference_handle_id": handle.id},
            ], strict=True)
        ]
        db.add_all([
            *hidden,
            SettingTextField(timeline_id=timeline.id, definition_id=visible_ref.id,
                             base_value={"reference_handle_id": handle.id}),
            SettingTextField(
                timeline_id=timeline.id, label="隐藏旧字段标签", base_value="隐藏旧字段原值", ai_hidden=True,
            ),
            SettingTextField(timeline_id=timeline.id, label="公开旧字段", base_value="公开旧值"),
        ])
        await db.flush()
        db.add(SettingProgression(
            timeline_id=timeline.id, custom_field_id=hidden[0].id, operation="append", value="隐藏后期事件",
            anchor_kind="chapter", chapter_id=chapters[1].id, boundary="end", source="manual",
        ))
        await db.commit()

    assert (await _settings(client, base, "world", detected.id))["ai_mode"] == "detected"
    await _settings(client, base, "character", hero.id, ai_mode="always", mention_enabled=False,
                    ai_hidden_builtin_fields=["background"], links=[{"kind": "organization", "id": org.id}])
    await _settings(client, base, "character", private.id, ai_mode="never")
    await _settings(client, base, "organization", org.id, ai_mode="manual_only",
                    ai_hidden_builtin_fields=["description"], links=[{"kind": "career", "id": career.id}])
    await _settings(client, base, "career", career.id, ai_mode="manual_only")
    await _settings(client, base, "world", world.id, ai_mode="always", ai_hidden_builtin_fields=["content"])
    await _settings(client, base, "world", manual.id, ai_mode="manual_only")
    cid = chapters[-1].id
    cast = await client.put(f"{base}/chapters/{cid}/cast", json={"character_ids": [private.id]})
    assert cast.status_code == 200, cast.text

    private_values = (
        "隐藏背景哨兵", "档案禁用者", "秘密别名", "隐档哨兵", "隐藏地点原文",
        "隐藏组织简介", "跨两跳职业", "不因名称注入的标记", "隐藏下拉选项",
        "隐藏多行内容", "隐藏单行内容", "隐藏旧字段", "隐藏后期事件",
        "隐藏text标签", "隐藏line标签", "隐藏dropdown标签", "隐藏reference标签",
        "公开字段指向禁用条目",
    )
    for chapter in chapters:
        fake_llm.push("这是本章正文。")
        task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter.id}/generate", json={
            "instruction": "请写观测塔和手选小岛。",
        }))
        assert task["status"] == "succeeded", task
        prompt = _prompt(fake_llm)
        for value in ("公开角色细节", "公开旧值", "玻璃港", "一跳组织正文", "检测地点可见"):
            assert value in prompt
        for value in private_values:
            assert value not in prompt, (chapter.chapter_no, value)
        preview = await client.get(f"{base}/chapters/{chapter.id}/setting-context")
        assert preview.status_code == 200, preview.text
        assert "公开角色细节" in preview.json()["text"]
        assert all(value not in preview.json()["text"] for value in private_values)

    detail = await client.get(f"{base}/setting-entries/character/{hero.id}/fields", params={"chapter_id": cid})
    assert "隐藏后期事件" in detail.text
    await _settings(client, base, "character", hero.id, ai_mode="manual_only")
    async with app.state.sessionmaker() as db:
        scope = await build_scope(db, pid, explicit=[("organization", org.id)])
        assert scope.includes("career", career.id)
        assert ("career", career.id) not in scope.direct
        assert not scope.includes("character", hero.id)  # 不反向带入来源
    await _settings(client, base, "organization", org.id, ai_mode="never")
    async with app.state.sessionmaker() as db:
        scope = await build_scope(db, pid, explicit=[("character", hero.id), ("organization", org.id)])
        assert scope.includes("character", hero.id)
        assert not scope.includes("organization", org.id) and not scope.includes("career", career.id)
    await _settings(client, base, "organization", org.id, ai_mode="manual_only")
    await _settings(client, base, "character", hero.id, ai_mode="never")
    async with app.state.sessionmaker() as db:
        scope = await build_scope(db, pid, explicit=[("character", hero.id)])
        assert not scope.includes("character", hero.id) and not scope.includes("organization", org.id)


async def test_analysis_uses_author_planned_pov_without_bypassing_never(client, fake_llm):
    await register(client)
    await create_ai_config(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "雨夜", "content": "风雨初歇。"})).json()
    character = (await client.post(f"{base}/characters", json={
        "name": "远航者", "current_state": "规划指定才读取的状态",
    })).json()
    await _settings(client, base, "character", character["id"], ai_mode="manual_only")
    revision = (await client.get(f"{base}/planning/matrix")).json()["revision"]
    saved = await client.patch(f"{base}/planning/units/batch", json={
        "expected_revision": revision,
        "targets": [{"kind": "chapter", "id": chapter["id"]}],
        "pov": {"op": "set", "character_id": character["id"]},
    })
    assert saved.status_code == 200, saved.text

    for mode in ("manual_only", "never"):
        await _settings(client, base, "character", character["id"], ai_mode=mode)
        fake_llm.push(analysis_json())
        await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/analyze"))
        for marker in ("远航者", "规划指定才读取的状态"):
            assert (marker in _prompt(fake_llm)) == (mode == "manual_only")


async def test_analysis_never_sends_a_mixed_planning_revision(app, client, fake_llm, monkeypatch):
    await register(client)
    await create_ai_config(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=pid, chapter_no=1, title="测试章", content="风雨初歇。")
        actors = [Character(project_id=pid, name=f"计划角色{n}", ai_mode="manual_only") for n in "甲乙"]
        organizations = [Organization(project_id=pid, name=f"计划组织{n}", ai_mode="manual_only") for n in "甲乙"]
        db.add_all([chapter, *actors, *organizations])
        await db.commit()

    async def save_plan(index):
        revision = (await client.get(f"{base}/planning/matrix")).json()["revision"]
        saved = await client.patch(f"{base}/planning/units/batch", json={
            "expected_revision": revision,
            "targets": [{"kind": "chapter", "id": chapter.id}],
            "pov": {"op": "set", "character_id": actors[index].id},
            "references": [{"kind": "organization", "op": "replace", "ids": [organizations[index].id]}],
        })
        assert saved.status_code == 200, saved.text

    await save_plan(0)
    load_unit = planning_context_service._chapter_unit

    async def load_then_change(*args):
        unit = await load_unit(*args)
        assert unit.pov_character_id == actors[0].id
        await save_plan(1)
        return unit

    fake_llm.push(analysis_json())
    with monkeypatch.context() as patch:
        patch.setattr(planning_context_service, "_chapter_unit", load_then_change)
        task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter.id}/analyze"))
    assert task["status"] == "failed" and "规划在读取期间发生变化" in task["error"]
    assert not fake_llm.calls

    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter.id}/analyze"))
    assert task["status"] == "succeeded"
    prompt = _prompt(fake_llm)
    assert "计划角色乙" in prompt and "计划组织乙" in prompt
    assert "计划角色甲" not in prompt and "计划组织甲" not in prompt


async def test_tracking_configuration_reuses_nine_categories_without_turning_legacy_entries_on(app, client):
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    async with app.state.sessionmaker() as db:
        old = Organization(project_id=pid, name="旧团体", ai_mode=None)
        org = Organization(project_id=pid, name="追踪组织")
        career = Career(project_id=pid, name="追踪职业", kind="main")
        hero = Character(project_id=pid, name="Ann", aliases=["Annie"])
        categories = ("place", "faction", "item", "rule", "history", "other")
        worlds = [WorldSetting(project_id=pid, title=f"词条{cat}", category=cat) for cat in categories]
        text = "🙂 ANN Board，ann，Annie；旧团体，追踪组织，追踪职业；" + "，".join(w.title for w in worlds)
        chapter = Chapter(project_id=pid, title="第一章", chapter_no=1, content=text)
        db.add_all([old, org, career, hero, *worlds, chapter])
        await db.commit()
    await _settings(client, base, "character", hero.id, mention_case_sensitive=False, mention_exclusions=["Ann Board"])
    assert not (await _settings(client, base, "organization", old.id))["mention_enabled"]
    response = await client.post(f"{base}/mentions/preview", json={"text": text})
    body = response.json()
    assert {e["category"] for span in body["spans"] for e in span["entities"]} == {
        *categories, "organization", "career", "character",
    }
    assert [span["text"] for span in body["spans"][:2]] == ["ann", "Annie"]
    assert "旧团体" not in [s["text"] for s in body["spans"]]
    await _settings(client, base, "organization", old.id, mention_enabled=True)
    summary = (await client.get(f"{base}/mentions/summary")).json()
    assert next(row for row in summary["entries"] if row["entity"]["name"] == "旧团体")["mention_count"] == 1
    await _settings(client, base, "character", hero.id, mention_enabled=False)
    refreshed = (await client.post(f"{base}/mentions/preview", json={"text": text})).json()
    assert refreshed["dictionary_hash"] != body["dictionary_hash"]
    assert all(e["kind"] != "character" for span in refreshed["spans"] for e in span["entities"])


async def test_continuation_does_not_send_first_round_context_after_tightening(app, client, fake_llm):
    await register(client)
    await create_ai_config(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    hero = (await client.post(
        f"{base}/characters", json={"name": "桥边旅人", "profile": "只允许在首轮发出的哨兵"},
    )).json()
    await _settings(client, base, "character", hero["id"], ai_mode="always")
    chapter = (await client.post(f"{base}/chapters", json={"title": "测试续写"})).json()
    chapter_url = f"{base}/chapters/{chapter['id']}"
    scene = (await client.get(f"{chapter_url}/scenes")).json()["items"][0]
    assert (await client.get(f"{chapter_url}/versions")).json() == []
    gate = Gate()
    fake_llm.push(["已生成首句。", gate])
    response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={
        "target_words": 300, "auto_continue": True,
    })
    assert response.status_code == 202, response.text
    await gate.wait_reached()
    try:
        assert "只允许在首轮发出的哨兵" in _prompt(fake_llm)
        await _settings(client, base, "character", hero["id"], ai_mode="never")
    finally:
        gate.release()
    events = await stream_task(client, response.json()["id"])
    assert len(fake_llm.calls) == 1
    event, result = events[-1]
    # 可见性收紧撤回了整个上下文快照；最终保存必须拒绝，而非把旧首轮当作成功稿。
    # 这不同于普通续写网络失败，后者仍保留主结果并返回warning。
    assert event == "error" and result["task"]["status"] == "failed", events[-1]
    assert "AI 可见设置已变更" in json.dumps(result, ensure_ascii=False)
    saved = (await client.get(chapter_url)).json()
    saved_scene = (await client.get(f"{chapter_url}/scenes/{scene['id']}")).json()
    assert saved["content"] == saved_scene["content"] == chapter["content"] == ""
    assert saved["content_revision"] == saved_scene["content_revision"] == chapter["content_revision"]
    assert (await client.get(f"{chapter_url}/versions")).json() == []


async def test_initial_job_can_rebuild_before_any_generation_request(app, client, fake_llm, monkeypatch):
    await register(client)
    await create_ai_config(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    hero = (await client.post(f"{base}/characters", json={"name": "堤边画师", "profile": "首轮重建哨兵"})).json()
    chapter = (await client.post(f"{base}/chapters", json={"title": "重建测试"})).json()
    await _settings(client, base, "character", hero["id"], ai_mode="always")
    builds = 0
    original = generation_service.build_chapter_context

    async def change_after_first_build(*args, **kwargs):
        nonlocal builds
        text = await original(*args, **kwargs)
        builds += 1
        if builds == 1:
            assert "首轮重建哨兵" in text
            await _settings(client, base, "character", hero["id"], ai_mode="never")
        return text

    monkeypatch.setattr(generation_service, "build_chapter_context", change_after_first_build)
    fake_llm.push("安全的新正文。")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/generate"))
    assert task["status"] == "succeeded", task
    assert builds == 2 and len(fake_llm.calls) == 1
    assert "首轮重建哨兵" not in _prompt(fake_llm)


async def test_memory_embedding_does_not_send_canonical_names_from_old_internal_index(app, client, fake_llm):
    await register(client)
    pid = (await create_project(client))["id"]
    async with app.state.sessionmaker() as db:
        memory = StoryMemory(
            project_id=pid, chapter_no=1, kind="summary", origin="auto",
            content="原文保留", characters=["从旧设定解析的内部名字"], content_key="a" * 64,
        )
        db.add(memory)
        await db.flush()
        await memory_service.index_memories(db, pid, [memory])
        await db.commit()
    embedder = Embedder(fake_llm.factory("openai_compat", "test", None, 10), "openai_compat", "test")
    job = kb_index_service.IndexJob("memory", pid, None, embedder)
    events = [event async for event in kb_index_service._embed_stale(
        app.state.sessionmaker, job, source="memory", ref_ids=None, total=1,
    )]
    assert len(events) == 1
    assert fake_llm.embed_calls[0]["texts"] == ["原文保留"]


async def test_generation_retrieval_embedding_uses_projected_cast(client, fake_llm):
    await register(client)
    await create_ai_config(client)
    await create_ai_config(client, name="向量测试", model_type="embedding", model="embedding-test")
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    allowed = (await client.post(f"{base}/characters", json={"name": "允许进入检索词"})).json()
    private = (await client.post(f"{base}/characters", json={"name": "不得进入检索词"})).json()
    await _settings(client, base, "character", allowed["id"], ai_mode="always")
    await _settings(client, base, "character", private["id"], ai_mode="never")
    index = await client.post(f"{base}/kb/documents", files={
        "file": ("测试资料.txt", "只为触发混合检索的资料。".encode(), "text/plain"),
    })
    assert index.status_code == 202, index.text
    events = await stream_task(client, index.json()["task"]["id"])
    assert events[-1][0] == "done"
    fake_llm.embed_calls.clear()
    chapter = (await client.post(f"{base}/chapters", json={"title": "向量验证"})).json()
    cast = await client.put(f"{base}/chapters/{chapter['id']}/cast", json={"character_ids": [private["id"]]})
    assert cast.status_code == 200, cast.text
    fake_llm.push("正文")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/generate"))
    assert task["status"] == "succeeded", task
    query = "\n".join(text for call in fake_llm.embed_calls for text in call["texts"])
    assert "允许进入检索词" in query and "不得进入检索词" not in query


@pytest.mark.parametrize("consumer", ["analysis", "outline", "wizard", "entity"])
async def test_other_model_consumers_use_same_hard_projection(app, client, fake_llm, consumer):
    user = await register(client)
    await create_ai_config(client)
    pid = (await create_project(client))["id"]
    async with app.state.sessionmaker() as db:
        first = Character(project_id=pid, name="可读人物", profile="人物简介可见", current_state="隐藏当前状态",
                          background="隐藏人物背景", role_type="隐藏人物定位", ai_mode="always",
                          ai_hidden_builtin_fields=["current_state", "background", "role_type"])
        private = Character(project_id=pid, name="不可读人物", aliases=["隐私别名"], ai_mode="never")
        visible = WorldSetting(
            project_id=pid, title="公开条目", category="rule", content="公开世界观", ai_mode="always",
        )
        world = WorldSetting(
            project_id=pid, title="隐藏世界观", category="rule", content="禁用世界观正文", ai_mode="never",
        )
        org = Organization(project_id=pid, name="公开组织", description="不可读组织描述", ai_mode="always",
                           ai_hidden_builtin_fields=["description"])
        chapter = Chapter(project_id=pid, title="测试章", chapter_no=1, content="作者输入中的原文。")
        db.add_all([first, private, visible, world, org, chapter])
        await db.commit()
        common = dict(user_id=user["id"], cipher=app.state.cipher, factory=fake_llm.factory,
                      ai_timeout=10, allow_private=False)
        if consumer == "analysis":
            job = await analysis_service.build_analysis_job(db, chapter_id=chapter.id, ai_config_id=None, **common)
            expected = "可读人物"
        elif consumer == "outline":
            job = await outline_ai_service.build_outline_job(
                db, kind="outline.continue", project_id=pid, params={}, **common,
            )
            expected = "公开世界观"
        elif consumer == "wizard":
            from app.schemas.chapter import WizardRequest
            job = await wizard_service.build_wizard_job(
                db, kind="outline", project_id=pid, req=WizardRequest(), ai_config_id=None, **common,
            )
            expected = "人物简介可见"
        else:
            job = await entity_ai_service.build_entity_job(
                db, kind="organization.generate", project_id=pid, params={}, **common,
            )
            expected = "公开组织"
    async with aclosing(job.provider.generate_stream(job.messages, job.model)) as stream:
        assert await anext(stream) == "ok"
    prompt = _prompt(fake_llm)
    assert expected in prompt
    for marker in ("隐藏当前状态", "隐藏人物背景", "隐藏人物定位", "不可读人物",
                   "隐私别名", "隐藏世界观", "禁用世界观正文", "不可读组织描述"):
        assert marker not in prompt


async def test_postgresql_identity_union_tracks_all_types_and_loads_directional_links(
    postgres_fields_app, postgres_fields_client,
):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    async with app.state.sessionmaker() as db:
        hero = Character(project_id=pid, name="当前角色", aliases=["当前别名"])
        org = Organization(project_id=pid, name="当前组织")
        career = Career(project_id=pid, name="当前职业", kind="main")
        world = WorldSetting(project_id=pid, title="当前物品", category="item")
        db.add_all([hero, org, career, world])
        await db.commit()
    await _settings(client, base, "character", hero.id, links=[{"kind": "organization", "id": org.id}])
    await _settings(client, base, "organization", org.id, ai_mode="manual_only")
    response = await client.post(f"{base}/mentions/preview", json={
        "text": "当前别名、当前组织、当前职业、当前物品",
    })
    assert response.status_code == 200, response.text
    assert [s["entities"][0]["kind"] for s in response.json()["spans"]] == [
        "character", "organization", "career", "world",
    ]
    async with app.state.sessionmaker() as db:
        scope = await build_scope(db, pid, texts=("当前别名",))
        assert scope.direct == {("character", hero.id)}
        assert scope.selected == {("character", hero.id), ("organization", org.id)}
