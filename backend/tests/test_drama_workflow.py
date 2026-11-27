"""通过工作台使用的 HTTP 和 SSE 接口走完一集；只替换外部付费模型。"""

import json
from io import BytesIO

from docx import Document

from tests.helpers import create_ai_config, create_project, register, run_task, streamed_text

BASE = "/api/v1/plugins/drama"
SCRIPT = "## S01 | 内景 · 茶馆 | 夜\n阿禾：（轻声）这盏灯给你。"


async def post_json(client, url, body):
    response = await client.post(url, json=body)
    assert response.is_success, response.text
    return response.json()


async def get_json(client, url):
    response = await client.get(url)
    assert response.is_success, response.text
    return response.json()


async def switch(admin, enabled, revision):
    result = await admin.patch("/api/v1/admin/plugins/drama", json={"enabled": enabled, "revision": revision})
    assert result.is_success, result.text


async def generate(client, fake_llm, episode_url, kind, output, **options):
    fake_llm.push(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
    response = await client.post(episode_url + "/tasks", json={"kind": "drama." + kind, **options})
    finished, events = await run_task(client, response)
    assert finished["status"] == "succeeded", finished
    assert events[-1][1]["result"]["applied"] is True, events[-1]
    return events


async def test_episode_from_chapters_to_export(client, make_client, system_admin, fake_llm):
    await register(client)
    await create_ai_config(client)
    project_id = (await create_project(client))["id"]
    project_url = f"/api/v1/projects/{project_id}"
    original = await post_json(client, project_url + "/characters", {"name": "阿禾", "appearance": "左眉有痣"})
    chapters = [
        await post_json(client, project_url + "/chapters", {"title": title, "chapter_no": i, "content": body})
        for i, (title, body) in enumerate([("灯", "阿禾推开茶馆门。"), ("交付", "她递过灯。")], 1)
    ]
    await switch(system_admin, True, 0)
    series = await post_json(client, f"{BASE}/projects/{project_id}/series", {"title": "灯火"})
    episode = await post_json(client, f"{BASE}/series/{series['id']}/episodes", {
        "title": "交灯", "chapter_ids": [chapter["id"] for chapter in chapters],
    })
    assert episode["source_text"] == "阿禾推开茶馆门。\n\n她递过灯。"
    episode_url = f"{BASE}/episodes/{episode['id']}"
    events = await generate(client, fake_llm, episode_url, "rewrite", SCRIPT)
    assert streamed_text(events) == SCRIPT
    assert (await get_json(client, episode_url))["script"] == SCRIPT

    await generate(client, fake_llm, episode_url, "extract", {
        "characters": [{"name": "阿禾", "looks": [{"name": "常服", "styling": "青衫"}]}],
        "scenes": [{"location": "茶馆", "time_of_day": "夜"}],
        "props": [{"name": "油灯"}],
    })
    assets_url = episode_url + "/assets"
    bundle = await get_json(client, assets_url)
    character, = bundle["characters"]
    assert character["novel_character_id"] == original["id"]
    assert character["appearance"] == "左眉有痣"
    look, = character["looks"]
    scene, = bundle["scenes"]
    prop, = bundle["props"]
    shots_url = episode_url + "/shots"
    description = "【镜头1】阿禾递灯。阿禾：这盏灯给你。"
    await generate(client, fake_llm, episode_url, "storyboard", {"shots": [{
        "title": "递灯", "description": description, "shot_size": "中景", "duration_seconds": 8,
        "look_ids": [look["id"]], "scene_id": scene["id"], "prop_ids": [prop["id"]],
    }]})
    shot, = await get_json(client, shots_url)
    assert (shot["look_ids"], shot["scene_id"], shot["prop_ids"]) == ([look["id"]], scene["id"], [prop["id"]])
    items = [("look", look, "青衫三视图"), ("scene", scene, "茶馆夜景"), ("prop", prop, "白底油灯"),
             ("shot", shot, "慢推至手部")]
    await generate(client, fake_llm, episode_url, "prompts", {
        "prompts": [{"kind": kind, "id": row["id"], "prompt": prompt} for kind, row, prompt in items],
    }, targets=[{"type": kind, "id": row["id"]} for kind, row, _ in items])

    bundle = await get_json(client, assets_url)
    shot, = await get_json(client, shots_url)
    assert bundle["characters"][0]["looks"][0]["image_prompt"] == "青衫三视图"
    assert bundle["scenes"][0]["image_prompt"] == "茶馆夜景"
    assert bundle["props"][0]["image_prompt"] == "白底油灯"
    assert shot["video_prompt"] == "慢推至手部"
    usage = await get_json(client, project_url + "/ai-usage")
    assert {row["kind"] for row in usage["items"]} == {
        "drama.rewrite", "drama.extract", "drama.storyboard", "drama.prompts",
    }

    for fmt in ("md", "docx"):
        exported = await client.get(episode_url + "/export", params={"format": fmt, "parts": "script,shots"})
        assert exported.is_success, exported.text[:200]
        if fmt == "md":
            text = exported.text
        else:
            document = Document(BytesIO(exported.content))
            table_text = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
            text = "\n".join([p.text for p in document.paragraphs] + table_text)
        assert "这盏灯给你" in text and description in text

    viewer = make_client()
    await register(viewer, "drama_viewer")
    assert (await viewer.get(shots_url)).status_code == 404
    invitation = await post_json(client, project_url + "/collaborators", {"username": "drama_viewer", "role": "viewer"})
    accept = f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept"
    assert (await viewer.post(accept, json={"expected_revision": invitation["revision"]})).is_success
    assert await get_json(viewer, shots_url) == [shot]
    assert (await viewer.post(episode_url + "/tasks", json={"kind": "drama.rewrite"})).status_code in (403, 404)
    assert len(fake_llm.calls) == 4
    await switch(system_admin, False, 1)
    assert (await viewer.get(shots_url)).status_code == 403
    await switch(system_admin, True, 2)
    assert await get_json(viewer, shots_url) == [shot]
