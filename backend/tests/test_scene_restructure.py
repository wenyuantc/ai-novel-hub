"""从选定的保存稿生成结构建议，显式确认才写正文结构。"""

import asyncio
import json

import pytest
from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import ProjectCollaborator, Task
from tests.helpers import Gate, create_ai_config, register, run_task, task_params, wait_task
from tests.test_rich_document_api import KEY, add_section, save_payload
from tests.test_scene_planning import planning_batch, scene_setup
from tests.test_scene_progressions import _scene_change, _timeline


def _body(scenes, operation="split"):
    return {
        "operation": operation,
        "scene_ids": [scene["id"] for scene in scenes],
        "expected_content_revision": scenes[0]["content_revision"],
    }


async def _preview(client, url, body):
    response = await client.post(f"{url}/preview", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _start(client, url, body, preview):
    return await client.post(f"{url}/suggestions", json={**body, "signature": preview["signature"]})


async def test_split_uses_only_confirmed_saved_body_and_applies_once(client, fake_llm):
    story = "我走到月夜里🙂。\r\n\r\n他回到了城门。"
    _, project_url, chapter, scenes_url, scenes = await scene_setup(client, content=story)
    await create_ai_config(client)
    chapter_url = f"{project_url}/chapters/{chapter['id']}"
    url = f"{chapter_url}/scene-restructure"
    document_url = f"{scenes_url}/{scenes[0]['id']}/document"
    payload = save_payload((await client.get(document_url)).json())
    add_section(payload, enabled=True)
    saved = await client.put(document_url, json=payload)
    assert saved.status_code == 200, saved.text
    response = await client.post(f"{project_url}/chapters", json={
        "chapter_no": 2, "title": "第三幕", "content": "这段在无关章节",
    })
    assert response.status_code == 201, response.text
    before = (await client.get(chapter_url)).json()
    versions_before = (await client.get(f"{chapter_url}/versions")).json()
    original_scene = {**scenes[0], "content_revision": saved.json()["content_revision"]}
    body = _body([original_scene])
    preview = await _preview(client, url, body)
    assert preview["scenes"][0]["content"] == story
    offset = story.index("他")
    assert preview["candidates"] == [{"candidate_id": 1, "offset": offset}]
    assert not preview["blockers"]
    fake_llm.push('{"proposals":[{"candidate_id":1,"reason":"人物和地点在此发生切换"}]}')
    task, events = await run_task(client, await _start(client, url, body, preview))
    assert task["status"] == "succeeded" and task["kind"] == "scene.restructure", events[-1]
    assert task["scene_id"] == scenes[0]["id"]
    messages = fake_llm.calls[0]["messages"]
    prompt = "\n".join(message["content"] for message in messages)
    assert messages[-1]["content"] == f"给出拆分位置建议。候选编号与对应位置：\n1：第 3 行开头\n\n保存稿：\n{story}"
    assert all(word not in prompt for word in ("作者🙂", "这段在无关章节", "第二段"))
    assert (await client.get(chapter_url)).json() == before
    assert (await client.get(f"{chapter_url}/versions")).json() == versions_before

    result_url = f"{url}/suggestions/{task['task_key']}"
    response = await client.get(result_url)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["proposals"] == [{
        "candidate_id": 1, "reason": "人物和地点在此发生切换", "offset": offset,
        "before": story[:offset], "after": story[offset:],
    }]
    args = {"signature": result["signature"], "candidate_id": 1, "right_title": "城门"}
    invalid = await client.post(f"{result_url}/apply", json={**args, "candidate_id": 2})
    assert invalid.status_code == 409, invalid.text
    attempts = await asyncio.gather(*(client.post(f"{result_url}/apply", json=args) for _ in range(2)))
    assert sorted(attempt.status_code for attempt in attempts) == [200, 409]
    applied = next(attempt for attempt in attempts if attempt.is_success)
    assert applied.status_code == 200, applied.text
    left, right = applied.json()["items"]
    assert (left["id"], left["content"], right["title"], right["content"]) == (
        scenes[0]["id"], story[:offset], "城门", story[offset:],
    )
    assert (await client.get(document_url)).json()["document"]["sections"][0]["key"] == KEY
    assert (await client.get(chapter_url)).json()["content"] == before["content"]
    assert (await client.get(scenes_url)).json()["total"] == 3


async def test_merge_requires_explicit_metadata_choice_and_preserves_original_seam(client, fake_llm):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="🚪 夜深。\r\n")
    await create_ai_config(client)
    chapter_url = f"{base}/chapters/{chapter['id']}"
    url = f"{chapter_url}/scene-restructure"
    before = (await client.get(chapter_url)).json()["content"]
    body = _body(scenes, "merge")
    preview = await _preview(client, url, body)
    assert preview["candidates"] == [{"candidate_id": 1, "offset": None}]
    assert any(conflict["field"] == "title" for conflict in preview["conflicts"])
    fake_llm.push('{"proposals":[{"candidate_id":1,"reason":"两段都在同一个晚上"}]}')
    task, _ = await run_task(client, await _start(client, url, body, preview))
    assert task["status"] == "succeeded", task
    assert fake_llm.calls[0]["messages"][-1]["content"] == (
        "判断是否适合合并为一个场景。候选编号 1。\n\n第一景保存稿：\n🚪 夜深。\r\n\n\n第二景保存稿：\n第二段"
    )
    result_url = f"{url}/suggestions/{task['task_key']}"
    result = (await client.get(result_url)).json()
    suggestion = result["proposals"][0]
    assert suggestion["before"] + suggestion["after"] == before
    args = {"signature": result["signature"], "candidate_id": 1}
    assert (await client.post(f"{result_url}/apply", json=args)).status_code == 422
    applied = await client.post(f"{result_url}/apply", json={**args, "metadata_source": "right"})
    assert applied.status_code == 200, applied.text
    assert [row["title"] for row in applied.json()["items"]] == ["场景2"]
    assert (await client.get(chapter_url)).json()["content"] == before


@pytest.mark.parametrize(("proposals", "status"), [
    ([], "succeeded"),
    ([{"candidate_id": 9, "reason": "超出预览候选"}], "failed"),
    ([{"candidate_id": True, "reason": "原始类型错误"}], "failed"),
    ([{"candidate_id": 1, "reason": "甲"}, {"candidate_id": 1, "reason": "重复"}], "failed"),
    ([{"candidate_id": 1, "reason": "甲", "content": "无关改稿"}], "failed"),
    (None, "failed"),
])
async def test_strict_model_candidates_never_write_scene(client, fake_llm, proposals, status):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="第一段。\n第二段。", count=1)
    await create_ai_config(client)
    url = f"{base}/chapters/{chapter['id']}/scene-restructure"
    body = _body(scenes)
    preview = await _preview(client, url, body)
    fake_llm.push(json.dumps({"proposals": proposals}) if proposals is not None else AIProviderError("模型错误"))
    task, _ = await run_task(client, await _start(client, url, body, preview))
    assert task["status"] == status, task
    if status == "succeeded":
        assert (await client.get(f"{url}/suggestions/{task['task_key']}")).json()["proposals"] == []
    assert (await client.get(scenes_url)).json()["items"] == scenes


async def test_split_reference_blocker_stops_before_model(client, fake_llm):
    _, base, chapter, _, scenes = await scene_setup(client, content="第一段。\n第二段。", count=1)
    await create_ai_config(client)
    world = (await client.post(f"{base}/world", json={"title": "山门", "content": "未毁"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    changed = await _scene_change(
        client, f"{base}/setting-timelines/{timeline['id']}", timeline["revision"], scenes[0]["id"], value="毁坏",
    )
    assert changed.status_code == 201, changed.text
    url = f"{base}/chapters/{chapter['id']}/scene-restructure"
    body = _body(scenes)
    preview = await _preview(client, url, body)
    assert preview["candidates"] == [] and preview["blockers"][0]["scene_id"] == scenes[0]["id"]
    response = await _start(client, url, body, preview)
    assert response.status_code == 409, response.text
    assert fake_llm.calls == []


@pytest.mark.parametrize("changed", ["body", "planning"])
async def test_saved_edit_locks_out_result_and_apply(client, fake_llm, changed):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="第一段。\n第二段。", count=1)
    await create_ai_config(client)
    url = f"{base}/chapters/{chapter['id']}/scene-restructure"
    body = _body(scenes)
    preview = await _preview(client, url, body)
    fake_llm.push('{"proposals":[{"candidate_id":1,"reason":"剧情过渡"}]}')
    task, _ = await run_task(client, await _start(client, url, body, preview))
    assert task["status"] == "succeeded", task
    result_url = f"{url}/suggestions/{task['task_key']}"
    result = (await client.get(result_url)).json()
    if changed == "planning":
        character = await client.post(f"{base}/characters", json={"name": "新视角"})
        assert character.status_code == 201, character.text
        edited = await planning_batch(
            client, base, scenes_url, [{"kind": "chapter", "id": chapter["id"]}],
            pov={"op": "set", "character_id": character.json()["id"]},
        )
    else:
        edited = await client.patch(f"{scenes_url}/{scenes[0]['id']}", json={
            "content": "保存后正文。", "expected_content_revision": body["expected_content_revision"],
        })
    assert edited.status_code == 200, edited.text
    assert (await client.get(result_url)).status_code == 409
    assert (await client.post(f"{result_url}/apply", json={
        "candidate_id": 1, "signature": result["signature"],
    })).status_code == 409
    assert (await client.get(scenes_url)).json()["total"] == 1


async def test_cancelled_advice_preserves_the_original(client, fake_llm):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="第一段。\n第二段。", count=1)
    await create_ai_config(client)
    url = f"{base}/chapters/{chapter['id']}/scene-restructure"
    body = _body(scenes)
    preview = await _preview(client, url, body)
    gate = Gate()
    fake_llm.push([gate, '{"proposals":[]}'])
    started = await _start(client, url, body, preview)
    assert started.status_code == 202, started.text
    task = started.json()
    try:
        await gate.wait_reached()
        cancelled = await asyncio.wait_for(
            client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task)), 5,
        )
        assert cancelled.status_code == 200, cancelled.text
    finally:
        gate.release()
    assert (await wait_task(client, task))["status"] == "cancelled"
    assert (await client.get(scenes_url)).json()["items"] == scenes


async def test_move_after_model_result_makes_the_original_suggestion_stale(client, fake_llm):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="第一段。\n第二段。", count=1)
    await create_ai_config(client)
    chapter_url = f"{base}/chapters/{chapter['id']}"
    url = f"{chapter_url}/scene-restructure"
    body = _body(scenes)
    preview = await _preview(client, url, body)
    fake_llm.push('{"proposals":[{"candidate_id":1,"reason":"更换地点"}]}')
    task, _ = await run_task(client, await _start(client, url, body, preview))
    assert task["status"] == "succeeded", task
    old_review_url = f"{url}/suggestions/{task['task_key']}"
    old_review = (await client.get(old_review_url)).json()
    target = await client.post(f"{base}/chapters", json={"title": "目标章", "chapter_no": 2, "content": "续写"})
    assert target.status_code == 201, target.text
    move_body = {"target_chapter_id": target.json()["id"]}
    scene_url = f"{scenes_url}/{scenes[0]['id']}"
    move_preview = await client.post(f"{scene_url}/move-preview", json=move_body)
    assert move_preview.status_code == 200, move_preview.text
    moved = await client.post(f"{scene_url}/move", json={**move_body, "signature": move_preview.json()["signature"]})
    assert moved.status_code == 200, moved.text
    assert (await client.get(old_review_url)).status_code == 409
    assert (await client.post(f"{old_review_url}/apply", json={
        "signature": old_review["signature"], "candidate_id": 1,
    })).status_code == 409
    assert (await client.get(chapter_url)).json()["content"] == ""
    target_scene = await client.get(f"{base}/chapters/{target.json()['id']}/scenes/{scenes[0]['id']}")
    assert target_scene.json()["content"] == "第一段。\n第二段。"


async def test_running_advice_fails_closed_when_selected_body_is_saved_again(client, app, fake_llm):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="甲。\n乙。", count=1)
    await create_ai_config(client)
    url = f"{base}/chapters/{chapter['id']}/scene-restructure"
    body = _body(scenes)
    preview = await _preview(client, url, body)
    gate = Gate()
    old_reason = "模型反馈基于旧正文"
    fake_llm.push([gate, json.dumps({"proposals": [{"candidate_id": 1, "reason": old_reason}]}, ensure_ascii=False)])
    start = await _start(client, url, body, preview)
    assert start.status_code == 202, start.text
    task = start.json()
    try:
        await gate.wait_reached()
        edited = await asyncio.wait_for(client.patch(f"{scenes_url}/{scenes[0]['id']}", json={
            "content": "编辑器在建议期间已保存", "expected_content_revision": body["expected_content_revision"],
        }), 5)
        assert edited.status_code == 200, edited.text
    finally:
        gate.release()
    assert (await wait_task(client, task))["status"] == "failed"
    async with app.state.sessionmaker() as db:
        persisted = await db.get(Task, task["id"])
        assert persisted.result is None and old_reason not in persisted.output_text
    assert (await client.get(f"{scenes_url}/{scenes[0]['id']}")).json()["content"] == "编辑器在建议期间已保存"


async def test_collaborator_reopens_only_own_result_while_authorized(client, make_client, app, fake_llm):
    project, base, chapter, scenes_url, scenes = await scene_setup(client, content="转身。\n进入庭院。", count=1)
    guest = make_client()
    username = "scene-editor"
    member = await register(guest, username)
    await create_ai_config(guest, model="editor-model")
    invite = await client.post(f"{base}/collaborators", json={"username": username, "role": "editor"})
    assert invite.status_code == 201, invite.text
    invitation = invite.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    url = f"{base}/chapters/{chapter['id']}/scene-restructure"
    body = _body(scenes)
    preview = await _preview(guest, url, body)
    fake_llm.push('{"proposals":[{"candidate_id":1,"reason":"换了取景位置"}]}')
    start = await _start(guest, url, body, preview)
    task, events = await run_task(guest, start)
    assert task["status"] == "succeeded", events[-1]
    assert [call["model"] for call in fake_llm.calls] == ["editor-model"]
    result_url = f"{url}/suggestions/{task['task_key']}"
    assert (await client.get(result_url)).status_code == 404
    reviewed = await guest.get(result_url)
    assert reviewed.status_code == 200, reviewed.text
    async with app.state.sessionmaker() as db:
        grant = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == member["id"],
        ))
    revoked = await client.delete(f"{base}/collaborators/{member['id']}", params={"expected_revision": grant.revision})
    assert revoked.status_code == 204, revoked.text
    assert (await guest.get(result_url)).status_code == 404
    failed = await guest.post(f"{result_url}/apply", json={
        "signature": reviewed.json()["signature"], "candidate_id": 1,
    })
    assert failed.status_code == 404, failed.text
    assert (await client.get(scenes_url)).json()["items"] == scenes
