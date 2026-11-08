import asyncio
import base64
import json

import pytest
from sqlalchemy import select

from app.ai.images import MAX_COVER_BYTES, TOO_LARGE
from app.ai.providers.base import AIProviderError, GeneratedImage
from app.models import ProjectCover
from tests.cover_fixtures import JPEG, PNG, WEBP
from tests.helpers import create_ai_config, create_project, purge_trashed_project, register


@pytest.mark.parametrize(("data", "mime"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
async def test_cover_upload_read_replace_delete(client, app, data, mime):
    await register(client)
    project = await create_project(client)
    url = f"/api/v1/projects/{project['id']}/cover"
    assert project["cover_updated_at"] is None
    assert (await client.get(url)).status_code == 404
    response = await client.put(url, files={"file": ("wrong.txt", data, "text/plain")})
    assert response.status_code == 200, response.text
    version = response.json()["cover_updated_at"]
    assert version
    image = await client.get(url, params={"v": version})
    assert image.status_code == 200 and image.content == data
    assert image.headers["content-type"] == mime
    assert image.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert (await client.get(url)).headers["cache-control"] == "private, no-cache"
    # 列表/详情只需要版本字段，保持原有项目字段与统计。
    assert (await client.get("/api/v1/projects")).json()[0]["cover_updated_at"] == version
    assert (await client.get(f"/api/v1/projects/{project['id']}")).json()["cover_updated_at"] == version
    again = await client.put(url, files={"file": ("a.png", PNG)})
    assert again.status_code == 200 and again.json()["cover_updated_at"] != version
    async with app.state.sessionmaker() as db:
        assert len(list(await db.scalars(select(ProjectCover)))) == 1
    removed = await client.delete(url)
    assert removed.status_code == 200 and removed.json()["cover_updated_at"] is None
    assert (await client.get(url)).status_code == 404
    assert (await client.delete(url)).status_code == 200
    assert (await client.get("/api/v1/projects")).json()[0]["cover_updated_at"] is None


async def test_invalid_upload_preserves_cover_and_enforces_request_limit(client):
    await register(client)
    project = await create_project(client)
    url = f"/api/v1/projects/{project['id']}/cover"
    original = await client.put(url, files={"file": ("a.png", PNG)})
    for data in (b"not an image", b"", PNG + b"x" * MAX_COVER_BYTES):
        response = await client.put(url, files={"file": ("fake.jpg", data, "image/jpeg")})
        assert response.status_code == 400, response.text
        assert isinstance(response.json()["detail"], str)
        assert (await client.get(url)).content == PNG
    oversized = await client.put(url, content=b"", headers={"Content-Length": str(MAX_COVER_BYTES * 2)})
    assert oversized.status_code == 400 and oversized.json()["detail"] == TOO_LARGE
    assert (await client.get(f"/api/v1/projects/{project['id']}")).json()["cover_updated_at"] == original.json()[
        "cover_updated_at"
    ]


async def test_cover_owner_and_missing_project(client, make_client):
    await register(client)
    project = await create_project(client)
    other = make_client()
    await register(other, "bob")
    for pid in (project["id"], 99999):
        url = f"/api/v1/projects/{pid}/cover"
        for method, path, kwargs in (
            ("get", url, {}),
            ("put", url, {"files": {"file": ("a.png", PNG)}}),
            ("delete", url, {}),
            ("get", url + "/prompt", {}),
            ("post", url + "/generate", {"json": {"prompt": "封面"}}),
        ):
            response = await getattr(other, method)(path, **kwargs)
            assert response.status_code == 404, response.text


async def test_cover_prompt_and_generate_preview_only(client, fake_llm):
    await register(client)
    style = (await client.post("/api/v1/styles", json={"name": "清冷", "description": "远景", "tone": "肃穆"})).json()
    project = await create_project(
        client, style="", default_style_id=style["id"], tags=["太空", "冒险"], synopsis="群星下的探索"
    )
    url = f"/api/v1/projects/{project['id']}/cover"
    prompt = (await client.get(url + "/prompt")).json()["prompt"]
    expected = (
        project["title"], project["genre"], project["premise"],
        "太空", "清冷", "远景", "肃穆", "2:3", "不要出现任何文字",
    )
    for text in expected:
        assert text in prompt
    config = await create_ai_config(client, model_type="image", model="image-test")
    fake_llm.images.append(GeneratedImage(PNG, "image/png"))
    generated = await client.post(url + "/generate", json={"prompt": " 用户修改的画面 ", "config_id": config["id"]})
    assert generated.status_code == 200, generated.text
    assert generated.json() == {"image_base64": base64.b64encode(PNG).decode(), "mime_type": "image/png"}
    assert fake_llm.image_calls == [{"provider": "openai_compat", "model": "image-test", "prompt": "用户修改的画面"}]
    assert (await client.get(url)).status_code == 404
    # 确认走同一上传接口，随后重新生成仍只产生预览。
    assert (await client.put(url, files={"file": ("cover.png", PNG)})).status_code == 200
    fake_llm.images.append(GeneratedImage(JPEG, "image/jpeg"))
    assert (await client.post(url + "/generate", json={"prompt": "另一张"})).status_code == 200
    assert (await client.get(url)).content == PNG


async def test_cover_prompt_long_premise_retains_requirements(client):
    await register(client)
    project = await create_project(client, premise="长设定" * 1200)
    url = f"/api/v1/projects/{project['id']}/cover"
    prompt = (await client.get(url + "/prompt")).json()["prompt"]
    assert len(prompt) <= 2000
    assert "竖版 2:3" in prompt and "不要出现任何文字" in prompt
    project2 = await create_project(client, premise="", synopsis="简介里的设定", style="")
    prompt2 = (await client.get(f"/api/v1/projects/{project2['id']}/cover/prompt")).json()["prompt"]
    assert "简介里的设定" in prompt2


async def test_cover_generate_errors_do_not_replace(client, fake_llm):
    await register(client)
    project = await create_project(client)
    url = f"/api/v1/projects/{project['id']}/cover"
    await client.put(url, files={"file": ("a.png", PNG)})
    no_config = await client.post(url + "/generate", json={"prompt": "一张封面"})
    assert no_config.status_code == 400 and "图片模型" in no_config.json()["detail"]
    text = await create_ai_config(client)
    wrong_type = await client.post(url + "/generate", json={"prompt": "一张封面", "config_id": text["id"]})
    assert wrong_type.status_code == 400
    unsupported = await create_ai_config(client, provider="anthropic", model_type="image")
    response = await client.post(url + "/generate", json={"prompt": "一张封面", "config_id": unsupported["id"]})
    assert response.status_code == 400 and "不支持图片生成" in response.json()["detail"]
    image_cfg = await create_ai_config(client, model_type="image")
    for item in (AIProviderError("图片模型不可用"), GeneratedImage(b"text", "image/png"),
                 GeneratedImage(PNG, "image/jpeg"), GeneratedImage(PNG + b"x" * MAX_COVER_BYTES, "image/png")):
        fake_llm.images.append(item)
        response = await client.post(url + "/generate", json={"prompt": "一张封面", "config_id": image_cfg["id"]})
        assert response.status_code == 400 and isinstance(response.json()["detail"], str)
        assert (await client.get(url)).content == PNG
    for prompt in ("", "   ", "a" * 2001):
        assert (await client.post(url + "/generate", json={"prompt": prompt})).status_code == 422


@pytest.mark.parametrize("settings_overrides", [{"ai_request_timeout": 0.03}])
async def test_cover_generate_timeout_cancels_provider_and_preserves_cover(client, app, monkeypatch):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client, model_type="image")
    url = f"/api/v1/projects/{project['id']}/cover"
    uploaded = await client.put(url, files={"file": ("original.png", PNG)})
    assert uploaded.status_code == 200
    original_version = uploaded.json()["cover_updated_at"]
    started, cancelled = asyncio.Event(), asyncio.Event()
    factory = app.state.provider_factory

    async def blocked_generate(prompt, model):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def blocked_factory(*args):
        provider = factory(*args)
        monkeypatch.setattr(provider, "generate_image", blocked_generate)
        return provider

    monkeypatch.setattr(app.state, "provider_factory", blocked_factory)
    response = await asyncio.wait_for(
        client.post(url + "/generate", json={"prompt": "需要等待的封面"}), 2
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "封面生成超时，请稍后重试"
    assert started.is_set() and cancelled.is_set()
    assert (await client.get(url)).content == PNG
    assert (await client.get(f"/api/v1/projects/{project['id']}")).json()["cover_updated_at"] == original_version


async def test_project_delete_preserves_cover_until_explicit_purge(client, app, recycle_admin):
    await register(client)
    project = await create_project(client)
    await client.put(f"/api/v1/projects/{project['id']}/cover", files={"file": ("a.png", PNG)})
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    assert (await client.get(f"/api/v1/projects/{project['id']}/cover")).status_code == 404
    async with app.state.sessionmaker() as db:
        assert (await db.get(ProjectCover, project["id"])).data == PNG
    await purge_trashed_project(recycle_admin, project["id"])
    async with app.state.sessionmaker() as db:
        assert await db.get(ProjectCover, project["id"]) is None


async def test_cover_backup_roundtrip_legacy_and_corruption(client):
    await register(client)
    project = await create_project(client)
    url = f"/api/v1/projects/{project['id']}/cover"
    estimate_before = (await client.get(f"/api/v1/projects/{project['id']}/backup/estimate")).json()
    assert (await client.put(url, files={"file": ("a.png", PNG)})).status_code == 200
    estimate_after = (await client.get(f"/api/v1/projects/{project['id']}/backup/estimate")).json()
    assert estimate_after["core"]["bytes"] - estimate_before["core"]["bytes"] == 4 * ((len(PNG) + 2) // 3)
    backup = (await client.get(f"/api/v1/projects/{project['id']}/backup")).json()
    assert backup["version"] == 9 and backup["app_schema"] == "0041"
    assert backup["cover"] == {"mime_type": "image/png", "data": base64.b64encode(PNG).decode()}

    async def restore(data):
        response = await client.post(
            "/api/v1/projects/import", files={"file": ("backup.json", json.dumps(data).encode(), "application/json")}
        )
        assert response.status_code == 201, response.text
        return response.json()["project"]

    new = await restore(backup)
    assert new["id"] != project["id"] and new["cover_updated_at"]
    assert (await client.get(f"/api/v1/projects/{new['id']}/cover")).content == PNG
    old = {k: v for k, v in backup.items() if k != "cover"}
    assert (await restore(old))["cover_updated_at"] is None
    for cover in (False, {"mime_type": "image/png", "data": "%%%"},
                  {"mime_type": "image/jpeg", "data": base64.b64encode(PNG).decode()},
                  {"mime_type": "image/svg+xml", "data": ""}, {"mime_type": "image/png", "data": ""}):
        response = await client.post("/api/v1/projects/import/preview", files={
            "file": ("bad.json", json.dumps({**backup, "cover": cover}).encode(), "application/json")
        })
        assert response.status_code == 400 and response.json()["errors"]
