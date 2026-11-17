"""图片只供人工界面，真实工具调用的模型消息不得包含图片身份或元数据。"""

import json

from tests.helpers import wait_task
from tests.test_agent_runtime import call, send_task, setup_agent
from tests.test_setting_thumbnails import image_data


async def test_setting_thumbnails_never_enter_agent_messages(client, fake_llm):
    project, _, url = await setup_agent(client)
    base = f"/api/v1/projects/{project['id']}"
    org = await client.post(f"{base}/organizations", json={"name": "组织可读标记"})
    career = await client.post(f"{base}/careers", json={
        "name": "职业可读标记", "stages": [{"name": "一阶"}],
    })
    char = await client.post(f"{base}/characters", json={"name": "角色可读标记"})
    world = await client.post(f"{base}/world", json={"title": "地点可读标记", "category": "place"})
    assert all(response.status_code == 201 for response in (org, career, char, world))
    versions = []
    for kind, item in (("organization", org.json()), ("career", career.json()),
                       ("character", char.json()), ("world", world.json())):
        settings_url = f"{base}/setting-entries/{kind}/{item['id']}/ai-settings"
        settings = await client.get(settings_url)
        assert settings.status_code == 200, settings.text
        visible = await client.patch(settings_url, json={
            "expected_ai_visibility_revision": settings.json()["ai_visibility_revision"], "ai_mode": "always",
        })
        assert visible.status_code == 200, visible.text
        response = await client.put(f"{base}/settings/{kind}/{item['id']}/thumbnail", files={
            "file": ("a.png", image_data(), "image/png"),
        }, data={"expected_version": ""})
        assert response.status_code == 200, response.text
        versions.append(response.json()["version"])
    fake_llm.push_tools([
        call("list_organizations", {}, "org-list"),
        call("get_organization", {"id": org.json()["id"]}, "org-detail"),
        call("list_careers", {}, "career-list"),
        call("list_characters", {}, "character-list"),
        call("list_world_settings", {}, "world-list"),
    ])
    fake_llm.push_tools([("text", {"text": "完成"})])
    task = await wait_task(client, await send_task(client, url, "请读取组织和职业"), within=30)
    assert task["status"] == "succeeded", task
    messages = fake_llm.tool_calls[-1]["messages"]
    tools = [item for item in messages if item["role"] == "tool"]
    assert len(tools) == 5
    wire = json.dumps(messages, ensure_ascii=False)
    assert "组织可读标记" in wire and "职业可读标记" in wire
    assert "角色可读标记" in wire and "地点可读标记" in wire
    assert "thumbnail" not in wire
    assert all(version not in wire for version in versions)
    history = json.dumps((await client.get(url)).json()["messages"], ensure_ascii=False)
    assert "thumbnail" not in history and all(version not in history for version in versions)
