"""通过真实 HTTP 图片模型协议，验证短剧工作台按角色、造型生成与读取图片。"""

import base64
import json
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from io import BytesIO

import pytest
from PIL import Image

from app.ai.providers import create_provider
from tests.helpers import create_ai_config, create_project, register, run_task, wait_task
from tests.test_collaboration_cover_ai import _invite, _sequence_server
from tests.test_image_options import png

BASE = "/api/v1/plugins/drama"


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True}


async def post(client, url, body):
    response = await client.post(url, json=body)
    assert response.is_success, response.text
    return response.json()


async def history(client, lid):
    response = await client.get(f"{BASE}/assets/look/{lid}/images")
    assert response.is_success, response.text
    return response.json()


def image_response(data):
    return 200, {"created": 1, "data": [{"b64_json": base64.b64encode(data).decode()}]}


def decode_image_edit(body):
    boundary = body.split(b"\r\n", 1)[0][2:]
    header = b"Content-Type: multipart/form-data; boundary=" + boundary + b"\r\n\r\n"
    parts = BytesParser(policy=policy.default).parsebytes(header + body)
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in parts.iter_parts()}


async def test_generate_distinct_look_images_and_serve_selected_media(app, client, system_admin, make_client):
    await register(client)
    pid = (await create_project(client))["id"]
    project_url = f"/api/v1/projects/{pid}"
    switch_url = "/api/v1/admin/plugins/drama"
    assert (await system_admin.patch(switch_url, json={"enabled": True, "revision": 0})).is_success
    series = await post(client, f"{BASE}/projects/{pid}/series", {"title": "夜探"})
    series_url = f"{BASE}/series/{series['id']}"
    heroine = await post(client, series_url + "/characters", {"name": "阿禾", "appearance": "左眉有痣"})
    other = await post(client, series_url + "/characters", {"name": "顾舟", "appearance": "灰色短发"})
    first = heroine["looks"][0]
    unrelated = other["looks"][0]
    for look, changes in [(first, {"image_prompt": "青衣书童装束"}), (unrelated, {"styling": "灰色斗篷"})]:
        saved = await client.patch(f"{BASE}/looks/{look['id']}", json={**changes, "revision": look["revision"]})
        assert saved.is_success, saved.text
    second = await post(client, f"{BASE}/characters/{heroine['id']}/looks", {"name": "夜行",
                                                                             "image_prompt": "黑衣夜行装束"})
    viewer = make_client()
    await register(viewer, "look-image-viewer")
    await _invite(client, viewer, project_url, "look-image-viewer", "viewer")

    def submit(actor, look_id):
        request = {"kind": "drama.image", "targets": [{"type": "look", "id": look_id}]}
        return actor.post(series_url + "/tasks", json=request)

    # 运行真实图片 SDK，只将模型端点指向测试专用的本地服务。
    app.state.task_engine.ctx.factory = create_provider
    rasters = [png((160, 90), color) for color in ("red", "green", "blue")]
    ready = []
    async with _sequence_server([image_response(data) for data in rasters]) as (url, requests):
        cfg = await create_ai_config(client, base_url=f"{url}/v1/", model_type="image", model="gpt-image-2")
        price_url = f"/api/v1/ai-configs/{cfg['id']}/price"
        version = (await client.get(price_url)).json()["version"]
        price = await client.put(price_url, json={"expected_version": version, "price": {"currency": "CNY",
                                                                                        "per_image": "2"}})
        assert price.is_success, price.text
        for look, expected in zip([first, unrelated, second], rasters, strict=True):
            finished, _ = await run_task(client, await submit(client, look["id"]))
            assert finished["status"] == "succeeded", finished
            entry, = await history(viewer, look["id"])
            assert entry["status"] == "ready" and entry["is_selected"] and entry["owner_id"] == look["id"]
            media_url = f"/api/v1/media/{entry['media_object_id']}"
            response = await viewer.get(media_url)
            assert response.status_code == 200 and response.content == expected
            thumbnail = await viewer.get(media_url, params={"variant": "thumb"})
            assert thumbnail.is_success and thumbnail.headers["content-type"].startswith("image/")
            with Image.open(BytesIO(thumbnail.content)) as thumb:
                assert thumb.width * 9 == thumb.height * 16
            ready.append(entry)

        assert "青衣书童装束" in json.loads(requests[0])["prompt"]
        other_prompt = json.loads(requests[1])["prompt"]
        assert all(value in other_prompt for value in ("顾舟", "灰色短发", "灰色斗篷"))
        assert "阿禾" not in other_prompt
        edit = decode_image_edit(requests[2])
        assert "黑衣夜行装束" in edit["prompt"].decode()
        files = [value for key, value in edit.items() if key.startswith("image")]
        assert len(files) == 1
        with Image.open(BytesIO(files[0])) as ref:
            r, g, b = ref.getpixel((0, 0))
            assert r > 245 and max(g, b) < 10
        assert ready[0]["reference_image_ids"] == ready[1]["reference_image_ids"] == []
        assert ready[2]["reference_image_ids"] == [ready[0]["id"]]
        assert len({entry["media_object_id"] for entry in ready}) == 3
        reloaded = (await viewer.get(series_url + "/assets")).json()["characters"]
        by_look = {look["id"]: look for char in reloaded for look in char["looks"]}
        for entry in ready:
            look = by_look[entry["owner_id"]]
            assert look["selected_image"]["id"] == entry["id"] and look["image_count"] == 1
        denied = await submit(viewer, first["id"])
        assert denied.status_code in (403, 404), denied.text
        usage = (await client.get(project_url + "/ai-usage")).json()["items"]
        assert len(usage) == 3
        assert all(row["kind"] == "drama.image" and row["image_count"] == 1 for row in usage)
        assert all(Decimal(row["estimated_cost"]) == 2 for row in usage)
        budget = await client.put(project_url + "/ai-usage/budgets/CNY", json={"limit": "1", "threshold_percent": 80})
        assert budget.is_success, budget.text
        blocked = await submit(client, first["id"])
        if blocked.status_code == 202:
            finished = await wait_task(client, blocked.json(), within=15)
            assert finished["status"] == "failed" and "预算" in finished["error"]
        else:
            assert blocked.status_code == 400 and "预算" in blocked.text
        assert len(requests) == 3

    media_url = f"/api/v1/media/{ready[-1]['media_object_id']}"
    assert (await system_admin.patch(switch_url, json={"enabled": False, "revision": 1})).is_success
    assert (await viewer.get(media_url)).status_code in (403, 404)
    assert (await system_admin.patch(switch_url, json={"enabled": True, "revision": 2})).is_success
    assert (await viewer.get(media_url)).content == rasters[-1]
    assert await history(viewer, second["id"]) == [ready[-1]]
