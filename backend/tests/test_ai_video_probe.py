from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from app.media import service
from tests.helpers import create_ai_config, register


@pytest.mark.parametrize("provider", ["volcengine", "minimax", "aliyun_wan", "openai_compat"])
async def test_video_config_crud_and_visibility(client, make_client, provider):
    await register(client)
    cfg = await create_ai_config(client, provider=provider, model_type="video")
    assert cfg["is_default"] and cfg["model_type"] == "video"
    url = f"/api/v1/ai-configs/{cfg['id']}"
    assert (await client.patch(url, json={"name": "视频 A"})).json()["name"] == "视频 A"
    assert (await client.patch(url, json={"provider": "gemini"})).status_code == 400
    if provider != "openai_compat":
        assert (await client.patch(url, json={"model_type": "text"})).status_code == 400
        assert (await client.post("/api/v1/ai-configs", json={
            "provider": provider, "model": "m", "name": "m", "model_type": "text",
        })).status_code == 422
    other = make_client()
    await register(other, "other")
    assert (await other.get(url)).status_code == 404
    assert (await other.get(url + "/video-probe")).status_code == 404
    assert (await client.delete(url)).status_code == 204


async def test_probe_reference_scope_and_cleanup(app, client, make_client):
    await register(client)
    other = make_client()
    await register(other, "other")
    cfg = await create_ai_config(client, model_type="video")
    route = f"/api/v1/ai-configs/{cfg['id']}/video-probe"
    assert (await client.get(route)).json() is None
    data = BytesIO()
    Image.new("RGB", (12, 10)).save(data, "PNG")
    files = {"file": ("参考.png", data.getvalue(), "image/png")}
    assert (await other.post(route + "/reference", files=files)).status_code == 404
    res = await client.post(route + "/reference", files=files)
    assert res.status_code == 201, res.text
    media = "/api/v1/media/" + res.json()["object_id"]
    assert (await client.get(media)).content == data.getvalue()
    assert (await other.get(media)).status_code == 404
    await service.sweep(app.state.sessionmaker, now=datetime.now(UTC) + timedelta(hours=25))
    assert (await client.get(media)).status_code == 404


@pytest.mark.parametrize("cancel", [False, True])
async def test_probe_through_task_engine(app, client, make_client, monkeypatch, cancel):
    import asyncio

    import httpx

    from app.ai.providers.video.base import VideoProvider
    from tests.helpers import wait_task
    from tests.test_media_service import VIDEO

    await register(client)
    cfg = await create_ai_config(client, model_type="video", model="sora-2")
    url = f"/api/v1/ai-configs/{cfg['id']}/video-probe"
    buf = BytesIO()
    Image.new("RGB", (480, 270)).save(buf, "JPEG")
    reference = await client.post(url + "/reference", files={"file": ("frame.jpg", buf.getvalue(), "image/jpeg")})
    ref_id = reference.json()["object_id"]
    submitted = []
    polled = asyncio.Event()

    def respond(req):
        if req.method == "POST":
            submitted.append(req.content)
            return httpx.Response(200, json={"id": "video1"})
        if req.url.path.endswith("/content"):
            return httpx.Response(200, content=VIDEO)
        if req.method == "DELETE":
            return httpx.Response(200, json={})
        polled.set()
        reply = {"status": "in_progress", "progress": 73} if cancel else {"status": "completed", "seconds": 4}
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(VideoProvider, "_transport", httpx.MockTransport(respond))
    response = await client.post(url, json={"image_ids": [ref_id]})
    assert response.status_code == 202, response.text
    accepted = response.json()
    if cancel:
        await asyncio.wait_for(polled.wait(), timeout=10)
        params = {"task_key": accepted["task_key"]}
        async with asyncio.timeout(8):
            while True:
                task = (await client.get(f"/api/v1/tasks/{accepted['id']}", params=params)).json()
                if task["progress"] == 73:
                    assert "等待" in task["stage"]
                    break
                await asyncio.sleep(0.03)
        cancel_url = f"/api/v1/tasks/{accepted['id']}/cancel"
        cancelled = await client.post(cancel_url, params={"task_key": accepted["task_key"]})
        assert cancelled.status_code == 200, cancelled.text
    finished = await wait_task(client, accepted, within=15)
    assert finished["status"] == ("cancelled" if cancel else "succeeded"), finished
    assert len(submitted) == 1 and b'name="input_reference"' in submitted[0]
    assert (await client.get(url)).json()["task_key"] == accepted["task_key"]
    other = make_client()
    await register(other, "other")
    if cancel:
        from sqlalchemy import select

        from app.models.ai_external_jobs import AiExternalJob

        assert finished["video_object_id"] is None
        async with app.state.sessionmaker() as db:
            job = (await db.scalars(select(AiExternalJob))).one()
            assert job.state == "cancelled" and job.usage_call_id is None
    else:
        media = "/api/v1/media/" + finished["video_object_id"]
        assert (await client.get(media)).content == VIDEO
        assert (await other.get(media)).status_code == 404
        await service.sweep(app.state.sessionmaker, now=datetime.now(UTC) + timedelta(hours=25))
        assert (await client.get(media)).status_code == 404
        assert (await client.get(url)).json()["video_object_id"] is None


async def test_probe_budget_and_private_endpoint_reject_before_task(client):
    await register(client)
    cfg = await create_ai_config(client, model_type="video", model="sora-2")
    url = f"/api/v1/ai-configs/{cfg['id']}"
    version = (await client.get(url + "/price")).json()["version"]
    price = {"currency": "CNY", "per_video_second": "1"}
    assert (await client.put(url + "/price", json={"expected_version": version, "price": price})).status_code == 200
    budget = await client.put("/api/v1/ai-usage/budgets/CNY", json={"limit": "1", "threshold_percent": 80})
    assert budget.status_code == 200, budget.text
    blocked = await client.post(url + "/video-probe")
    assert blocked.status_code == 400 and "预算" in blocked.text
    assert (await client.get(url + "/video-probe")).json() is None
    assert (await client.patch(url, json={"base_url": "http://127.0.0.1"})).status_code == 200
    private = await client.post(url + "/video-probe")
    assert private.status_code == 400 and "内网" in private.text
    assert (await client.get(url + "/video-probe")).json() is None


async def test_probe_vendor_rejection_shows_readable_reason(client, monkeypatch):
    import httpx

    from app.ai.providers.video.base import VideoProvider
    from tests.helpers import wait_task

    await register(client)
    cfg = await create_ai_config(client, model_type="video", model="sora-2")

    def respond(req):
        if req.method == "POST":
            return httpx.Response(200, json={"id": "rejected-video"})
        return httpx.Response(200, json={"status": "failed", "error": {"code": "moderation_blocked"}})

    monkeypatch.setattr(VideoProvider, "_transport", httpx.MockTransport(respond))
    url = f"/api/v1/ai-configs/{cfg['id']}/video-probe"
    response = await client.post(url)
    assert response.status_code == 202, response.text
    finished = await wait_task(client, response.json(), within=15)
    assert finished["status"] == "failed" and "审核" in finished["error"]
    assert finished["video_object_id"] is None
