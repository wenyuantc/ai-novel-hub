"""预览只展示可见内容；内部 guard 必须参与不可枚举的确认版本。"""

import hashlib
import json
from copy import deepcopy

import pytest

from app.models import AgentChange, AgentConversation, Project, Task
from app.services.agent import effects, service
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import ToolCtx
from tests.helpers import wait_task
from tests.test_agent_project_style_privacy import _world
from tests.test_agent_runtime import call, change_action, send_task, setup_agent


async def test_preview_guard_hash_is_not_exposed_to_editor_or_administrator(app, client, make_client):
    owner, editor, manager, editor_client, manager_client, project, style, _, _ = await _world(
        app, client, make_client,
    )
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        before = await effects.snapshot(db, project["id"])
        original = before["projects"][str(project["id"])]
        preview = await get_tool("update_project").preview(
            ToolCtx(db, project_row, owner["id"], app.state.task_engine.ctx), {"theme": "新的主题"},
        )
        guard = next(item for item in preview["guards"] if item["table"] == "projects")
        # 已知其他字段时，原无密钥 hash 可离线枚举作者私有风格的整数 ID。
        candidates = []
        for style_id in (None, style["id"], style["id"] + 1):
            candidate = {**original, "default_style_id": style_id}
            content = json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()
            candidates.append(hashlib.sha256(content).hexdigest())
        assert candidates[1] == guard["hash"] and len(set(candidates)) == 3
        await db.rollback()

    ordinary = {"title": "外部结果", "diff": [], "warnings": [], "guards": [{"name": "普通卫兵"}]}
    payload = {"preview": preview, "nested": json.dumps({"preview": preview}, ensure_ascii=False),
               "external": {"preview": ordinary}}
    original_payload = deepcopy(payload)
    for actor, actor_client in ((editor, editor_client), (manager, manager_client)):
        async with app.state.sessionmaker() as db:
            task = Task(user_id=actor["id"], project_id=project["id"], kind="agent.turn", status="succeeded",
                        output_text=json.dumps(payload, ensure_ascii=False))
            conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
            db.add_all([task, conversation])
            await db.flush()
            change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                                 turn_task_key=task.statistics_key, tool_call_id="guard", tool_name="update_project",
                                 kind="small", arguments={"theme": "新的主题"}, preview=preview, status="proposed")
            db.add(change)
            await db.commit()
            task_id, task_key = task.id, task.statistics_key
            public = (await service.public_changes(db, actor["id"], project["id"], [change]))[0]
            assert "guards" not in public.preview and guard["hash"] not in public.model_dump_json()
            assert public.view_version not in candidates
            assert change.preview == preview
        response = await actor_client.get(f"/api/v1/tasks/{task_id}/output", params={"task_key": task_key})
        assert response.status_code == 200, response.text
        output = json.loads(response.json()["text"])
        assert "guards" not in output["preview"]
        assert "guards" not in json.loads(output["nested"])["preview"]
        assert output["external"]["preview"] == ordinary
        assert guard["hash"] not in response.text
    assert payload == original_payload


@pytest.mark.parametrize("changed", [False, True])
async def test_guard_refresh_rejects_old_confirmation_without_exposing_hash(app, client, fake_llm, changed):
    project, base, url = await setup_agent(client)
    fake_llm.push_tools([call("update_project", {"theme": "更新后的主题"})])
    fake_llm.push_tools([("text", {"text": "完成"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(url)).json()["changes"][0]
    assert "guards" not in change["preview"]
    path = f"{base}/changes/{change['id']}/confirm"
    if changed:
        async with app.state.sessionmaker() as db:
            (await db.get(Project, project["id"])).synopsis = "另一个未显示的并发修改"
            await db.commit()
        response = await client.post(path, json=change_action(change))
        assert response.status_code == 409 and response.json()["code"] == "preview_changed", response.text
        refreshed = response.json()["change"]
        assert refreshed["preview"] == change["preview"]
        assert refreshed["view_version"] != change["view_version"]
        assert "guards" not in refreshed["preview"]
        replay = await client.post(path, json=change_action(change))
        assert replay.status_code == 409 and replay.json()["code"] == "agent_change_changed", replay.text
        change = refreshed
    response = await client.post(path, json=change_action(change))
    assert response.status_code == 200 and response.json()["status"] == "executed", response.text
    assert "guards" not in response.json()["preview"]


async def test_view_key_rotation_requires_refresh_and_missing_key_fails_closed(app, client, fake_llm):
    _, base, url = await setup_agent(client)
    fake_llm.push_tools([call("update_project", {"theme": "刷新主题"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    before = (await client.get(url)).json()["changes"][0]
    factory = app.state.sessionmaker
    info = dict(factory.kw["info"])
    try:
        # 仅替换测试工厂中的派生密钥，认证配置不变。
        factory.configure(info={**info, "agent_view_hmac_key": b"r" * 32})
        after = (await client.get(url)).json()["changes"][0]
        assert after["view_version"] != before["view_version"]
        response = await client.post(f"{base}/changes/{before['id']}/reject", json=change_action(before))
        assert response.status_code == 409 and response.json()["code"] == "agent_change_changed", response.text
        factory.configure(info={key: value for key, value in info.items() if key != "agent_view_hmac_key"})
        response = await client.get(url)
        assert response.status_code == 500 and response.json()["detail"] == "服务器内部错误", response.text
        assert before["view_version"] not in response.text
    finally:
        factory.configure(info=info)
