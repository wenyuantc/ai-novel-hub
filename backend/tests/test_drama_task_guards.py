"""通过真实插件入口验证协作模型、同集互斥、并发草稿和分批重试。"""

import asyncio
import json

import pytest
from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import AiUsageCall, ProjectCollaborator, Task
from tests.helpers import (
    Gate,
    create_ai_config,
    create_project,
    parse_sse,
    register,
    stream_with_gate,
    task_params,
    wait_task,
)

BASE = "/api/v1/plugins/drama"
RESULT = "## S1 | 外景 · 山门 | 日\n山门被风推开。"


@pytest.fixture
async def drama(client, system_admin):
    owner = await register(client, "drama-guard-author")
    enabled = await system_admin.patch("/api/v1/admin/plugins/drama", json={"enabled": True, "revision": 0})
    assert enabled.status_code == 200, enabled.text
    pid = (await create_project(client))["id"]
    await create_ai_config(client, name="作者模型", model="guard-owner-text")
    series = await client.post(f"{BASE}/projects/{pid}/series", json={"title": "山门"})
    assert series.status_code == 201, series.text
    episode = await client.post(f"{BASE}/series/{series.json()['id']}/episodes", json={
        "title": "山门初开", "source_text": "山门被风推开了。",
    })
    assert episode.status_code == 201, episode.text
    return owner["id"], pid, f"{BASE}/episodes/{episode.json()['id']}"


async def begin(client, url, kind="drama.rewrite"):
    response = await client.post(url + "/tasks", json={"kind": kind})
    assert response.status_code == 202, response.text
    return response.json()


async def terminal_row(app, task):
    async with asyncio.timeout(20):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task["id"])
                if row.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return row
            await asyncio.sleep(0.03)


async def grant(owner, app, pid, user_id, allowed):
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == pid, ProjectCollaborator.user_id == user_id,
        ))
    response = await owner.patch(f"/api/v1/projects/{pid}/collaborators/{user_id}", json={
        "expected_revision": member.revision, "allow_owner_ai": allowed,
    })
    assert response.status_code == 200, response.text


async def invite_editor(client, make_client, pid, *, allow_owner_ai):
    editor = make_client()
    user = await register(editor, "drama-guard-editor")
    invited = await client.post(f"/api/v1/projects/{pid}/collaborators", json={
        "username": user["username"], "role": "editor", "allow_owner_ai": allow_owner_ai,
    })
    assert invited.status_code == 201, invited.text
    invite = invited.json()
    accepted = await editor.post(f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
                                 json={"expected_revision": invite["revision"]})
    assert accepted.status_code == 200, accepted.text
    return editor, user


async def test_editor_uses_author_model_only_while_granted(app, client, make_client, drama, fake_llm):
    owner_id, pid, url = drama
    editor, user = await invite_editor(client, make_client, pid, allow_owner_ai=False)
    denied = await editor.post(url + "/tasks", json={"kind": "drama.rewrite"})
    assert 400 <= denied.status_code < 500 and not fake_llm.calls
    await grant(client, app, pid, user["id"], True)
    fake_llm.push([RESULT])
    original = await begin(editor, url)
    completed = await wait_task(editor, original, within=20)
    assert completed["status"] == "succeeded", completed
    assert (await client.get(url)).json()["script"] == RESULT
    replay = await editor.get(f"/api/v1/tasks/{original['id']}/stream", params=task_params(original))
    frames = parse_sse(replay.text)
    assert frames[-1][0] == "done" and frames[-1][1]["result"]["applied"] is True, frames
    history = await client.get(f"/api/v1/projects/{pid}/ai-usage", params={"kind": "drama.rewrite"})
    assert history.status_code == 200 and history.json()["total"] == 1
    async with app.state.sessionmaker() as db:
        usage = (await db.scalars(select(AiUsageCall).where(AiUsageCall.task_id == original["id"]))).one()
        assert (usage.kind, usage.actor_id, usage.payer_id) == ("drama.rewrite", user["id"], owner_id)
    gate = Gate()
    fake_llm.push([gate, RESULT.replace("山门", "新门")])
    running = await begin(editor, url)
    try:
        await gate.wait_reached(15)
        await grant(client, app, pid, user["id"], False)
    finally:
        gate.release()
    stopped = await terminal_row(app, running)
    assert stopped.status in {"cancelled", "failed"} and stopped.result is None
    assert (await client.get(url)).json()["script"] == RESULT


@pytest.mark.parametrize("action", ["edit", "cancel", "disable"])
async def test_running_rewrite_protects_saved_script(client, system_admin, drama, fake_llm, action):
    _, _, url = drama
    gate = Gate()
    fake_llm.push(["## S1", gate, RESULT[len("## S1"):]])
    task = await begin(client, url)
    try:
        await gate.wait_reached(15)
        busy = await client.post(url + "/tasks", json={"kind": "drama.rewrite"})
        assert busy.status_code == 409, busy.text
        if action == "edit":
            latest = (await client.get(url)).json()
            edit = await client.patch(url, json={"revision": latest["revision"], "script": "人工定稿"})
            assert edit.status_code == 200, edit.text
        elif action == "cancel":
            cancelled = await client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task))
            assert cancelled.is_success, cancelled.text
        else:
            disabled = await system_admin.patch("/api/v1/admin/plugins/drama", json={"revision": 1, "enabled": False})
            assert disabled.is_success
            assert (await client.post(url + "/tasks", json={"kind": "drama.rewrite"})).status_code == 403
            enabled = await system_admin.patch("/api/v1/admin/plugins/drama", json={"revision": 2, "enabled": True})
            assert enabled.is_success
    finally:
        gate.release()
    finished = await wait_task(client, task, within=20)
    if action == "edit":
        assert finished["status"] == "succeeded", finished
        stream = await client.get(f"/api/v1/tasks/{task['id']}/stream", params=task_params(task))
        frames = parse_sse(stream.text)
        assert frames[-1][0] == "done", frames
        result = frames[-1][1]["result"]
        assert not result["applied"] and result["draft"]["script"] == RESULT
        assert (await client.get(url)).json()["script"] == "人工定稿"
        apply = await client.post(url + f"/tasks/{task['id']}/apply", json=task_params(task))
        assert apply.is_success, apply.text
        assert (await client.get(url)).json()["script"] == RESULT
    else:
        assert finished["status"] == "cancelled", finished
        assert (await client.get(url)).json()["script"] == ""


def shot_response(title):
    return json.dumps({"shots": [{"title": title, "description": title + "的画面", "shot_size": "近景",
                                "duration_seconds": 4}]}, ensure_ascii=False)


async def save_two_scenes(client, url):
    before = (await client.get(url)).json()
    script = "## S1 | 内景 · 屋内 | 日\n" + "甲" * 3000 + "\n## S2 | 外景 · 山门 | 日\n" + "乙" * 3000
    assert (await client.patch(url, json={"revision": before["revision"], "script": script})).is_success


async def test_storyboard_retry_continues_after_completed_scene(client, drama, fake_llm):
    _, _, url = drama
    await save_two_scenes(client, url)
    fake_llm.push([shot_response("第一场")])
    fake_llm.push(AIProviderError("测试模拟第二场中断"))
    task = await begin(client, url, "drama.storyboard")
    failed = await wait_task(client, task, within=20)
    assert failed["status"] == "failed", failed
    assert (await client.get(url + "/shots")).json() == []
    fake_llm.push([shot_response("第二场")])
    retry = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
    assert retry.status_code == 202, retry.text
    done = await wait_task(client, retry.json(), within=20)
    assert done["status"] == "succeeded", done
    shots = (await client.get(url + "/shots")).json()
    assert [shot["title"] for shot in shots] == ["第一场", "第二场"]
    assert len(fake_llm.calls) == 3


async def test_editor_can_follow_storyboard_stream_across_checkpoint(app, client, make_client, drama, fake_llm):
    _, pid, url = drama
    editor, _ = await invite_editor(client, make_client, pid, allow_owner_ai=True)
    await save_two_scenes(client, url)
    gate = Gate()
    fake_llm.push([gate, shot_response("第一场")])
    fake_llm.push([shot_response("第二场")])
    task = await begin(editor, url, "drama.storyboard")
    try:
        streamed = await stream_with_gate(app, editor, task, gate)
    finally:
        gate.release()
    frames = parse_sse(streamed.text)
    assert frames[-1][0] == "done", frames
    result = frames[-1][1]["result"]
    assert result["applied"] is True
    assert [shot["title"] for shot in result["draft"]["shots"]] == ["第一场", "第二场"]
