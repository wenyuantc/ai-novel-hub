"""真实 FakeLLM 消息与统一场景提交：不是只测拼接器。"""

import pytest
from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import ChapterAnalysis, Task
from tests.helpers import Gate, create_ai_config, parse_sse, run_task, wait_task
from tests.test_scene_context import _policy
from tests.test_scene_planning import planning_batch, scene_setup
from tests.test_scene_progressions import _scene_change, _timeline


async def _revisions(client, url):
    page = (await client.get(url)).json()
    return {
        "expected_content_revision": page["content_revision"],
        "expected_planning_revision": page["planning_revision"],
    }


async def _contents(client, url):
    page = (await client.get(url)).json()
    return [(await client.get(f"{url}/{row['id']}")).json() for row in page["items"]]


def _prompt(fake, index=-1):
    return "\n".join(message["content"] for message in fake.calls[index]["messages"])


@pytest.mark.parametrize("mode", ["generate", "polish", "revise"])
async def test_scene_target_preserves_siblings_and_uses_filtered_pov_at_scene_start(client, app, fake_llm, mode):
    _, base, chapter, url, scenes = await scene_setup(client, content="无名原稿😀", count=3)
    await create_ai_config(client)
    old = (await client.post(f"{base}/characters", json={"name": "章默认人物", "profile": "不能混回资料"})).json()
    local = (
        await client.post(
            f"{base}/characters", json={"name": "本景角色", "profile": "隐藏秘密", "current_state": "初始学徒"}
        )
    ).json()
    never = (await client.post(f"{base}/characters", json={"name": "永不出站视角", "profile": "禁止资料"})).json()
    await _policy(client, base, "character", old["id"], ai_mode="manual_only")
    await _policy(client, base, "character", local["id"], ai_mode="manual_only", ai_hidden_builtin_fields=["profile"])
    await _policy(client, base, "character", never["id"], ai_mode="never")
    parent = await planning_batch(
        client,
        base,
        url,
        [{"kind": "chapter", "id": chapter["id"]}],
        pov={"op": "set", "character_id": old["id"]},
        references=[{"kind": "character", "op": "replace", "ids": [old["id"]]}],
    )
    assert parent.status_code == 200, parent.text
    selected = await planning_batch(
        client,
        base,
        url,
        [{"kind": "scene", "id": scenes[0]["id"]}],
        pov={"op": "set", "character_id": never["id"]},
        references=[{"kind": "character", "op": "replace", "ids": [local["id"]]}],
    )
    assert selected.status_code == 200, selected.text
    timeline = await _timeline(client, base, "character", "character_id", local["id"])
    changed = await _scene_change(
        client,
        f"{base}/setting-timelines/{timeline['id']}",
        timeline["revision"],
        scenes[1]["id"],
        field="current_state",
        value="未来神王",
    )
    assert changed.status_code == 201, changed.text
    body = await _revisions(client, url)
    if mode == "revise":
        async with app.state.sessionmaker() as db:
            analysis = ChapterAnalysis(
                project_id=chapter["project_id"],
                chapter_id=chapter["id"],
                chapter_no=chapter["chapter_no"],
                content_hash="old",
                summary="全章分析",
            )
            db.add(analysis)
            await db.commit()
            body.update(analysis_id=analysis.id, instruction="改善节奏")
    fake_llm.push("当前场景新稿")
    task, events = await run_task(client, await client.post(f"{url}/{scenes[0]['id']}/{mode}", json=body))
    assert task["status"] == "succeeded", events[-1]
    assert task["scene_id"] == scenes[0]["id"]
    result = events[-1][1]["result"]
    assert result["scene"]["content"] == "当前场景新稿"
    assert result["scene"]["id"] == scenes[0]["id"]
    contents = await _contents(client, url)
    assert [row["id"] for row in contents] == [row["id"] for row in scenes]
    assert [row["content"] for row in contents] == ["当前场景新稿", "第二段", "第二段"]
    prompt = _prompt(fake_llm)
    assert "本景角色" in prompt and "初始学徒" in prompt and "场景写作范围（锁定）" in prompt
    assert all(
        secret not in prompt
        for secret in (
            "章默认人物",
            "不能混回资料",
            "永不出站视角",
            "隐藏秘密",
            "未来神王",
        )
    )
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(Task.id).where(Task.kind == "chapter.analyze"))) == []


async def test_whole_chapter_stages_latest_preceding_proposals_without_old_or_future_scene(client, fake_llm):
    _, base, chapter, url, scenes = await scene_setup(client, content="旧第一景绝不能重用", count=3)
    await create_ai_config(client)
    for row, content in zip(scenes[1:], ["旧第二景绝不能重用", "第三景未来正文"], strict=True):
        response = await client.patch(
            f"{url}/{row['id']}",
            json={
                "expected_content_revision": (await _revisions(client, url))["expected_content_revision"],
                "content": content,
            },
        )
        assert response.status_code == 200
    for name in ("第一视角", "第二视角", "第三视角"):
        character = (await client.post(f"{base}/characters", json={"name": name})).json()
        await _policy(client, base, "character", character["id"], ai_mode="manual_only")
        index = ("第一视角", "第二视角", "第三视角").index(name)
        saved = await planning_batch(
            client,
            base,
            url,
            [{"kind": "scene", "id": scenes[index]["id"]}],
            pov={"op": "set", "character_id": character["id"]},
        )
        assert saved.status_code == 200
    before = await _contents(client, url)
    fake_llm.push("第一景最新提案")
    fake_llm.push("第二景最新提案")
    fake_llm.push("第三景最新提案")
    task, events = await run_task(
        client,
        await client.post(
            f"/api/v1/chapters/{chapter['id']}/generate",
            json={
                "target_words": 901,
                **await _revisions(client, url),
            },
        ),
    )
    assert task["status"] == "succeeded", events[-1]
    assert task["scene_id"] is None
    assert events[-1][1]["result"]["scene_ids"] == [row["id"] for row in scenes]
    assert "第一景最新提案" in _prompt(fake_llm, 1) and "旧第一景绝不能重用" not in _prompt(fake_llm, 1)
    assert "第三景未来正文" not in _prompt(fake_llm, 0) and "第三景未来正文" not in _prompt(fake_llm, 1)
    assert "第二景最新提案" in _prompt(fake_llm, 2) and "旧第二景绝不能重用" not in _prompt(fake_llm, 2)
    for index, name in enumerate(("第一视角", "第二视角", "第三视角")):
        assert name in _prompt(fake_llm, index)
        assert all(other not in _prompt(fake_llm, index) for other in {"第一视角", "第二视角", "第三视角"} - {name})
    after = await _contents(client, url)
    assert [[row[key] for key in ("id", "title", "sort", "summary", "planning")] for row in after] == [
        [row[key] for key in ("id", "title", "sort", "summary", "planning")] for row in before
    ]
    assert ["本场景目标字数：301", "本场景目标字数：300", "本场景目标字数：300"] == [
        next(line for line in _prompt(fake_llm, index).splitlines() if line.startswith("本场景目标字数：")).split("（")[
            0
        ]
        for index in range(3)
    ]


@pytest.mark.parametrize("failure", ["error", "cancel", "planning", "timeline"])
async def test_whole_chapter_failure_or_stale_context_never_commits_partial_scenes(client, fake_llm, failure):
    _, base, chapter, url, scenes = await scene_setup(client, count=3)
    await create_ai_config(client)
    world = (await client.post(f"{base}/world", json={"title": "山门", "content": "初始"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    before = await _contents(client, url)
    gate = Gate()
    fake_llm.push("第一景已生成但未提交")
    fake_llm.push(
        ["第二景部分", gate, AIProviderError("主调用失败")] if failure == "error" else ["第二景部分", gate, "完整"]
    )
    fake_llm.push("第三景提案")
    response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={"target_words": 900})
    assert response.status_code == 202, response.text
    await gate.wait_reached()
    if failure == "cancel":
        assert (await client.post(f"/api/v1/tasks/{response.json()['id']}/cancel")).status_code == 200
    elif failure == "planning":
        changed = await planning_batch(
            client, base, url, [{"kind": "chapter", "id": chapter["id"]}], pov={"op": "clear"}
        )
        assert changed.status_code == 200, changed.text
    elif failure == "timeline":
        changed = await _scene_change(
            client,
            f"{base}/setting-timelines/{timeline['id']}",
            timeline["revision"],
            scenes[1]["id"],
            value="同时改动设定",
        )
        assert changed.status_code == 201, changed.text
    gate.release()
    task = await wait_task(client, response.json()["id"])
    assert task["status"] == ("cancelled" if failure == "cancel" else "failed"), task
    assert [row["content"] for row in await _contents(client, url)] == [row["content"] for row in before]


@pytest.mark.parametrize("op", ["rewrite", "expand", "condense", "deslop", "continue"])
async def test_five_scene_local_operations_bind_unicode_versions_and_real_log(client, fake_llm, op):
    _, _, _, url, scenes = await scene_setup(client, content="雨😀𠀀落下。", count=2)
    await create_ai_config(client)
    revision = await _revisions(client, url)
    start, end = (3, 3) if op == "continue" else (1, 3)
    fake_llm.push("替换稿")
    response = await client.post(
        f"{url}/{scenes[0]['id']}/rewrite",
        json={
            "op": op,
            "start": start,
            "end": end,
            **revision,
        },
    )
    assert response.status_code == 200, response.text
    event, done = parse_sse(response.text)[-1]
    assert event == "done", done
    assert done["scene_id"] == scenes[0]["id"]
    assert done["content_revision"] == revision["expected_content_revision"]
    assert "第二段" not in _prompt(fake_llm)
    apply = {key: done[key] for key in ("start", "end", "original", "before", "log_id")}
    apply.update(replacement=done["text"], **revision)
    saved = await client.post(f"{url}/{scenes[0]['id']}/rewrite/apply", json=apply)
    assert saved.status_code == 200, saved.text
    assert saved.json()["content"] == "雨😀𠀀落下。"[:start] + "替换稿" + "雨😀𠀀落下。"[end:]
    assert (await _contents(client, url))[1]["content"] == "第二段"
    stale = await client.post(f"{url}/{scenes[0]['id']}/rewrite/apply", json=apply)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision"


async def test_old_cross_scene_preview_rejected_and_new_wrappers_forbid_ambiguous_or_stale_target(client, fake_llm):
    _, _, chapter, url, scenes = await scene_setup(client)
    await create_ai_config(client)
    whole = await client.post(f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 7})
    assert whole.status_code == 409 and whole.json()["code"] == "scene_target_required"
    for body in (
        {},
        {"expected_content_revision": True, "expected_planning_revision": 0},
        {**await _revisions(client, url), "scene_id": scenes[1]["id"]},
    ):
        invalid = await client.post(f"{url}/{scenes[0]['id']}/generate", json=body)
        assert invalid.status_code == 422, invalid.text
    assert not fake_llm.calls


@pytest.mark.parametrize("mode", ["polish", "revise"])
@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_whole_edit_keeps_empty_scene_and_enqueues_exactly_one_full_analysis(client, app, fake_llm, mode):
    from tests.helpers import analysis_json

    _, _, chapter, url, scenes = await scene_setup(client, count=3)
    await create_ai_config(client)
    empty = await client.patch(
        f"{url}/{scenes[1]['id']}",
        json={
            "expected_content_revision": (await _revisions(client, url))["expected_content_revision"],
            "content": "",
        },
    )
    assert empty.status_code == 200
    # 开启自动分析时，独立场景成功仍不能排队伪全章分析。
    fake_llm.push("独立场景修改")
    task, _ = await run_task(
        client, await client.post(f"{url}/{scenes[0]['id']}/polish", json=await _revisions(client, url))
    )
    assert task["status"] == "succeeded"
    assert (await client.get("/api/v1/tasks", params={"kind": "chapter.analyze"})).json() == []
    body = await _revisions(client, url)
    if mode == "revise":
        async with app.state.sessionmaker() as db:
            analysis = ChapterAnalysis(
                project_id=chapter["project_id"],
                chapter_id=chapter["id"],
                chapter_no=1,
                content_hash="old",
                summary="全章建议",
            )
            db.add(analysis)
            await db.commit()
            body.update(analysis_id=analysis.id, instruction="只修改有正文场景")
    fake_llm.push("首景整章修改")
    fake_llm.push("末景整章修改")
    fake_llm.push(analysis_json())
    task, events = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/{mode}", json=body))
    assert task["status"] == "succeeded", events[-1]
    assert [row["content"] for row in await _contents(client, url)] == ["首景整章修改", "", "末景整章修改"]
    analyses = (await client.get("/api/v1/tasks", params={"kind": "chapter.analyze"})).json()
    assert len(analyses) == 1
    assert (await wait_task(client, analyses[0]["id"]))["status"] == "succeeded"


async def test_scene_continuation_warning_only_keeps_current_primary_and_never_touches_next_scene(client, fake_llm):
    _, _, _, url, scenes = await scene_setup(client, count=3)
    await create_ai_config(client)
    fake_llm.push("主稿" * 100)
    fake_llm.push(["应丢弃的续写半稿", AIProviderError("续写网络失败")])
    task, events = await run_task(
        client,
        await client.post(
            f"{url}/{scenes[1]['id']}/generate",
            json={
                **await _revisions(client, url),
                "target_words": 300,
                "auto_continue": True,
            },
        ),
    )
    assert task["status"] == "succeeded" and "自动续写失败" in events[-1][1]["result"]["warning"]
    assert [row["content"] for row in await _contents(client, url)] == ["第一段", "主稿" * 100, "第二段"]
    assert len(fake_llm.calls) == 2
    assert "场景写作范围（锁定）" in _prompt(fake_llm, 1)
    assert f"目标：场景 {scenes[1]['id']}" in _prompt(fake_llm, 1)


@pytest.mark.parametrize("mode", ["generate", "polish", "revise", "rewrite"])
async def test_real_legacy_templates_cannot_drop_scene_scope_or_filtered_planning(client, app, fake_llm, mode):
    from app.models import PromptTemplateOverride

    _, base, chapter, url, scenes = await scene_setup(client, content="原稿😀", count=2)
    await create_ai_config(client)
    character = (await client.post(f"{base}/characters", json={"name": "唯一正确视角"})).json()
    saved = await planning_batch(
        client,
        base,
        url,
        [{"kind": "scene", "id": scenes[0]["id"]}],
        pov={"op": "set", "character_id": character["id"]},
    )
    assert saved.status_code == 200
    user = (await client.get("/api/v1/auth/me")).json()
    body = await _revisions(client, url)
    async with app.state.sessionmaker() as db:
        db.add(
            PromptTemplateOverride(user_id=user["id"], key=f"chapter.{mode}", system="旧模板系统", user="旧模板标记")
        )
        if mode == "revise":
            analysis = ChapterAnalysis(
                project_id=chapter["project_id"],
                chapter_id=chapter["id"],
                chapter_no=1,
                content_hash="old",
                summary="建议",
            )
            db.add(analysis)
            await db.flush()
            body.update(analysis_id=analysis.id, instruction="保留事件")
        await db.commit()
    fake_llm.push("限定场景新稿")
    if mode == "rewrite":
        response = await client.post(
            f"{url}/{scenes[0]['id']}/rewrite", json={**body, "op": "rewrite", "start": 0, "end": 2}
        )
        assert parse_sse(response.text)[-1][0] == "done", response.text
    else:
        task, events = await run_task(client, await client.post(f"{url}/{scenes[0]['id']}/{mode}", json=body))
        assert task["status"] == "succeeded", events[-1]
    prompt = _prompt(fake_llm)
    assert "旧模板标记" in prompt and prompt.count("【场景写作范围（锁定）】") == 1
    assert "唯一正确视角" in prompt and "第二段" not in prompt


async def test_explicit_retry_rechecks_stable_scene_and_new_revisions_but_keeps_skill_snapshot(client, fake_llm):
    _, _, _, url, scenes = await scene_setup(client)
    await create_ai_config(client)
    skill = (
        await client.post(
            "/api/v1/skills",
            json={
                "slug": "scene-flow",
                "name": "场景工作流",
                "body": "原工作流必须保留",
            },
        )
    ).json()
    fake_llm.push(AIProviderError("首次主调用失败"))
    task, _ = await run_task(
        client,
        await client.post(
            f"{url}/{scenes[0]['id']}/generate",
            json={
                **await _revisions(client, url),
                "skill_id": skill["id"],
            },
        ),
    )
    assert task["status"] == "failed"
    changed = await client.patch(
        f"{url}/{scenes[0]['id']}",
        json={
            "expected_content_revision": (await _revisions(client, url))["expected_content_revision"],
            "content": "作者新稿",
        },
    )
    assert changed.status_code == 200
    assert (await client.patch(f"/api/v1/skills/{skill['id']}", json={"body": "不能偷换的新工作流"})).status_code == 200
    fake_llm.push("显式重试成功")
    retried, events = await run_task(client, await client.post(f"/api/v1/tasks/{task['id']}/retry"))
    assert retried["status"] == "succeeded", events[-1]
    assert retried["scene_id"] == scenes[0]["id"] and retried["retry_of_id"] == task["id"]
    assert "原工作流必须保留" in _prompt(fake_llm) and "不能偷换的新工作流" not in _prompt(fake_llm)
    # 再次失败后的原身份退场，不能按相同排序改认另一个Scene。
    fake_llm.push(AIProviderError("再次失败"))
    failed, _ = await run_task(
        client, await client.post(f"{url}/{scenes[0]['id']}/generate", json=await _revisions(client, url))
    )
    deleted = await client.delete(
        f"{url}/{scenes[0]['id']}",
        params={
            "expected_content_revision": (await _revisions(client, url))["expected_content_revision"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    stale = await client.post(f"/api/v1/tasks/{failed['id']}/retry")
    assert stale.status_code == 404 and stale.json()["detail"] == "场景不存在"


async def test_batch_skips_future_chapter_deleted_after_prior_commit_before_next_snapshot(
    client, app, fake_llm, monkeypatch
):
    from app.services import batch_service

    _, base, first, _, _ = await scene_setup(client, content="", count=1)
    await create_ai_config(client)
    second = (await client.post(f"{base}/chapters", json={"content": ""})).json()
    third = (await client.post(f"{base}/chapters", json={"content": ""})).json()
    gate = Gate()
    original = batch_service._analyze_chapter

    async def stop_after_commit(ctx, job, index, item):
        if index == 0:
            gate.reached.set()
            await gate.released.wait()
        async for event in original(ctx, job, index, item):
            yield event

    monkeypatch.setattr(batch_service, "_analyze_chapter", stop_after_commit)
    fake_llm.push("已提交的第一章")
    fake_llm.push("第三章新稿")
    response = await client.post(
        f"{base}/batch-generate",
        json={
            "start_chapter_id": first["id"],
            "end_chapter_id": third["id"],
            "auto_continue": False,
        },
    )
    assert response.status_code == 202, response.text
    await gate.wait_reached()
    persisted = (await client.get(f"{base}/chapters/{first['id']}")).json()
    assert persisted["content"] == "已提交的第一章" and persisted["status"] == "done"
    assert (await client.delete(f"{base}/chapters/{second['id']}")).status_code == 204
    gate.release()
    task = await wait_task(client, response.json()["id"])
    assert task["status"] == "succeeded", task
    assert [item["state"] for item in task["checkpoint"]["items"]] == ["done", "skipped_missing", "done"]
    assert (await client.get(f"{base}/chapters/{first['id']}")).json()["content"] == "已提交的第一章"
    assert (await client.get(f"{base}/chapters/{third['id']}")).json()["content"] == "第三章新稿"


async def test_batch_multiscene_main_failure_keeps_previous_chapter_checkpoint(client, fake_llm, monkeypatch):
    from app.services import batch_service

    _, base, first, _, _ = await scene_setup(client, content="", count=2)
    await create_ai_config(client)
    second = (await client.post(f"{base}/chapters", json={"content": ""})).json()
    url = f"{base}/chapters/{second['id']}/scenes"
    added = await client.post(url, json={"expected_content_revision": 0, "content": ""})
    assert added.status_code == 201, added.text
    before = [row["content"] for row in await _contents(client, url)]
    monkeypatch.setattr(batch_service, "MAX_RETRIES", 0)
    fake_llm.push("第一章第一景提交")
    fake_llm.push("第一章第二景提交")
    fake_llm.push("第二章第一景不得半写")
    fake_llm.push(AIProviderError("第二章第二景主失败"))
    task, events = await run_task(
        client,
        await client.post(
            f"{base}/batch-generate",
            json={
                "start_chapter_id": first["id"],
                "end_chapter_id": second["id"],
                "auto_continue": False,
                "include_written": True,
            },
        ),
    )
    assert task["status"] == "failed", events[-1]
    assert task["step_cursor"] == 1
    assert [item["state"] for item in task["checkpoint"]["items"]] == ["done", "failed"]
    assert (await client.get(f"{base}/chapters/{first['id']}")).json()[
        "content"
    ] == "第一章第一景提交\n\n第一章第二景提交"
    assert [row["content"] for row in await _contents(client, url)] == before


@pytest.mark.parametrize("scene_id", [0, -1, 2_147_483_648, 10**50])
@pytest.mark.parametrize("mode", ["generate", "polish", "revise", "rewrite", "rewrite/apply"])
async def test_scoped_ai_invalid_path_identity_is_validation_error_not_internal_failure(client, scene_id, mode):
    _, _, _, url, _ = await scene_setup(client, count=1)
    body = {"expected_content_revision": 0, "expected_planning_revision": 0}
    if mode == "revise":
        body.update(analysis_id=1, instruction="改写")
    elif mode == "rewrite":
        body.update(op="rewrite", start=0, end=1)
    elif mode == "rewrite/apply":
        body.update(start=0, end=1, original="第", replacement="新")
    response = await client.post(f"{url}/{scene_id}/{mode}", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("请求参数校验失败")


async def test_scoped_routes_isolate_owner_project_chapter_and_scene_before_private_ai(client, make_client):
    from tests.helpers import register

    _, base, chapter, url, scenes = await scene_setup(client, count=1)
    body = await _revisions(client, url)
    other = make_client()
    await register(other, "different_scene_owner")
    second = (await client.post(f"{base}/chapters", json={"content": "另章"})).json()
    paths = [f"{url}/{scenes[0]['id']}", f"{base}/chapters/{second['id']}/scenes/{scenes[0]['id']}"]
    for mode in ("generate", "polish", "revise", "rewrite", "rewrite/apply"):
        payload = dict(body)
        if mode == "revise":
            payload.update(analysis_id=1, instruction="改写")
        elif mode == "rewrite":
            payload.update(op="rewrite", start=0, end=1)
        elif mode == "rewrite/apply":
            payload.update(start=0, end=1, original="第", replacement="新")
        forbidden = await other.post(f"{paths[0]}/{mode}", json=payload)
        assert forbidden.status_code == 404, forbidden.text
        wrong_chapter = await client.post(f"{paths[1]}/{mode}", json=payload)
        assert wrong_chapter.status_code == 404, wrong_chapter.text
    assert (await client.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == "第一段"


async def test_scene_apply_requires_preview_log_and_rechecks_timeline_without_relabeling_revision(client, fake_llm):
    _, base, _, url, scenes = await scene_setup(client)
    await create_ai_config(client)
    world = (await client.post(f"{base}/world", json={"title": "变化资料", "content": "初始"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    revision = await _revisions(client, url)
    fake_llm.push("预览修改")
    response = await client.post(
        f"{url}/{scenes[0]['id']}/rewrite",
        json={
            **revision,
            "op": "rewrite",
            "start": 0,
            "end": 1,
        },
    )
    event, done = parse_sse(response.text)[-1]
    assert event == "done", done
    payload = {
        **revision,
        "start": done["start"],
        "end": done["end"],
        "original": done["original"],
        "before": done["before"],
        "replacement": done["text"],
    }
    missing = await client.post(f"{url}/{scenes[0]['id']}/rewrite/apply", json=payload)
    assert missing.status_code == 409 and missing.json()["code"] == "scene_context_changed"
    changed = await _scene_change(
        client,
        f"{base}/setting-timelines/{timeline['id']}",
        timeline["revision"],
        scenes[1]["id"],
        value="预览后修改的资料",
    )
    assert changed.status_code == 201, changed.text
    assert await _revisions(client, url) == revision
    stale = await client.post(f"{url}/{scenes[0]['id']}/rewrite/apply", json={**payload, "log_id": done["log_id"]})
    assert stale.status_code == 409 and stale.json()["code"] == "scene_context_changed"
    assert [row["content"] for row in await _contents(client, url)] == ["第一段", "第二段"]


async def test_long_chapter_title_keeps_scene_identity_and_style_id_stays_within_database_range(client, fake_llm):
    _, base, chapter, url, scenes = await scene_setup(client, count=1)
    await create_ai_config(client)
    renamed = await client.patch(f"{base}/chapters/{chapter['id']}", json={"title": "章" * 200})
    assert renamed.status_code == 200
    body = await _revisions(client, url)
    invalid = await client.post(f"{url}/{scenes[0]['id']}/generate", json={**body, "style": 10**50})
    assert invalid.status_code == 422
    assert not fake_llm.calls
    fake_llm.push("标题边界的新稿")
    task, events = await run_task(client, await client.post(f"{url}/{scenes[0]['id']}/generate", json=body))
    assert task["status"] == "succeeded", events[-1]
    assert task["title"].endswith(f" · 场景 {scenes[0]['id']}") and len(task["title"]) <= 200
