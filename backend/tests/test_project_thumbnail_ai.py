"""作品设定图 AI：字段投影、预览确认和受控版本。"""

import base64

import pytest

from app.ai.providers.base import GeneratedImage
from app.services.setting_image_codec import normalize_image
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_cover_ai import _invite
from tests.test_setting_thumbnails import image_data


@pytest.mark.parametrize(("kind", "path", "body", "visible"), [
    ("character", "characters", {"name": "夜行者", "appearance": "银发"}, "银发"),
    ("world", "world", {"title": "旧港", "category": "place", "content": "灯火通明"}, "灯火通明"),
    ("organization", "organizations", {"name": "协会", "purpose": "守护古城"}, "守护古城"),
    ("career", "careers", {"name": "航者", "abilities": "驾驭风暴", "stages": [{"name": "初阶"}]}, "驾驭风暴"),
])
async def test_project_thumbnail_generation_preview_then_apply(client, fake_llm, kind, path, body, visible):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client, model_type="image", model="image-test")
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/{path}", json=body)
    assert response.status_code == 201, response.text
    route = f"{base}/settings/{kind}/{response.json()['id']}/thumbnail"
    response = await client.get(f"{route}/ai/prompt")
    assert response.status_code == 200, response.text
    prompt = response.json()
    assert visible in prompt["prompt"]
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    response = await client.post(f"{route}/ai/generate", json={
        "prompt": prompt["prompt"] + "\n自定义", "prompt_token": prompt["prompt_token"], "expected_version": "",
    })
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["mime_type"] == "image/webp" and preview["model_source"] == "actor"
    assert fake_llm.image_calls[-1]["prompt"].endswith("自定义")
    assert (await client.get(route)).status_code == 404
    response = await client.post(f"{route}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert response.status_code == 200, response.text
    assert (response.json()["width"], response.json()["height"]) == (512, 256)
    image = await client.get(route)
    assert image.content == base64.b64decode(preview["image_base64"])


async def test_prompt_visibility_and_stale_source_fail_closed(client, fake_llm):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client, model_type="image", model="image-test")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={
        "name": "甲", "profile": "秘密", "appearance": "金发",
    })).json()
    ai_settings = f"{base}/setting-entries/character/{character['id']}/ai-settings"
    changed = await client.patch(ai_settings, json={"expected_ai_visibility_revision": 0,
                                                    "ai_mode": "manual_only", "ai_hidden_builtin_fields": ["profile"]})
    assert changed.status_code == 200, changed.text
    route = f"{base}/settings/character/{character['id']}/thumbnail"
    response = await client.get(f"{route}/ai/prompt")
    assert response.status_code == 200, response.text
    prompt = response.json()
    assert "金发" in prompt["prompt"] and "秘密" not in prompt["prompt"]
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    generated = await client.post(f"{route}/ai/generate", json={
        "prompt": prompt["prompt"], "prompt_token": prompt["prompt_token"], "expected_version": "",
    })
    assert generated.status_code == 200, generated.text
    preview = generated.json()
    changed = await client.patch(ai_settings, json={"expected_ai_visibility_revision": 1, "ai_mode": "never"})
    assert changed.status_code == 200, changed.text
    response = await client.post(f"{route}/ai/generate", json={
        "prompt": prompt["prompt"], "prompt_token": prompt["prompt_token"], "expected_version": "",
    })
    assert response.status_code == 409 and response.json()["code"] == "entity_source_changed", response.text
    assert len(fake_llm.image_calls) == 1
    applied = await client.post(f"{route}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert applied.status_code == 409 and applied.json()["code"] == "entity_source_changed", applied.text
    assert (await client.get(route)).status_code == 404
    assert (await client.get(f"{route}/ai/prompt")).status_code == 400


async def test_source_text_change_invalidates_prompt_before_provider_call(client, fake_llm):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client, model_type="image", model="image-test")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "甲", "profile": "旧设定"})).json()
    route = f"{base}/settings/character/{character['id']}/thumbnail"
    prompt = (await client.get(f"{route}/ai/prompt")).json()
    changed = await client.patch(f"{base}/characters/{character['id']}", json={"profile": "新设定"})
    assert changed.status_code == 200, changed.text
    response = await client.post(f"{route}/ai/generate", json={
        "prompt": prompt["prompt"], "prompt_token": prompt["prompt_token"], "expected_version": "",
    })
    assert response.status_code == 409 and response.json()["code"] == "entity_source_changed"
    assert not fake_llm.image_calls


async def test_preview_is_bound_to_image_version_and_bytes(client, fake_llm):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client, model_type="image", model="image-test")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "甲"})).json()
    route = f"{base}/settings/character/{character['id']}/thumbnail"
    prompt = (await client.get(f"{route}/ai/prompt")).json()
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    result = await client.post(f"{route}/ai/generate", json={
        "prompt": prompt["prompt"], "prompt_token": prompt["prompt_token"], "expected_version": "",
    })
    assert result.status_code == 200, result.text
    preview = result.json()
    altered = base64.b64encode(normalize_image(image_data("blue"), "image/png").data).decode("ascii")
    result = await client.post(f"{route}/ai/apply", json={
        "image_base64": altered, "preview_token": preview["preview_token"],
    })
    assert result.status_code == 409 and result.json()["code"] == "thumbnail_ai_token_invalid"
    assert (await client.get(route)).status_code == 404
    second = await client.put(route, files={"file": ("manual.png", image_data("blue"), "image/png")},
                              data={"expected_version": ""})
    assert second.status_code == 200, second.text
    result = await client.post(f"{route}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert result.status_code == 409 and result.json()["code"] == "thumbnail_changed"
    assert (await client.get(route)).headers["etag"] == f'"{second.json()["version"]}"'


async def test_project_editor_uses_authorized_owner_model(client, make_client, fake_llm):
    await register(client, "thumbnail-owner")
    project = await create_project(client)
    await create_ai_config(client, model_type="image", model="owner-image")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "甲"})).json()
    guest = make_client()
    await register(guest, "thumbnail-editor")
    await _invite(client, guest, base, "thumbnail-editor", allow_owner_ai=True)
    route = f"{base}/settings/character/{character['id']}/thumbnail"
    prompt = await guest.get(f"{route}/ai/prompt")
    assert prompt.status_code == 200, prompt.text
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    result = await guest.post(f"{route}/ai/generate", json={"prompt": prompt.json()["prompt"],
                                                             "prompt_token": prompt.json()["prompt_token"],
                                                             "expected_version": ""})
    assert result.status_code == 200, result.text
    assert result.json()["model_source"] == "owner"
    assert fake_llm.image_calls[-1]["model"] == "owner-image"
    await guest.aclose()


async def test_preview_rejects_changed_model_and_unauthorized_user(client, make_client, fake_llm):
    await register(client, "thumbnail-preview-owner")
    project = await create_project(client)
    config = await create_ai_config(client, model_type="image", model="image-test")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "甲"})).json()
    route = f"{base}/settings/character/{character['id']}/thumbnail"
    prompt = (await client.get(f"{route}/ai/prompt")).json()
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    result = await client.post(f"{route}/ai/generate", json={
        "prompt": prompt["prompt"], "prompt_token": prompt["prompt_token"], "expected_version": "",
    })
    assert result.status_code == 200, result.text
    preview = result.json()
    other = make_client()
    await register(other, "thumbnail-other")
    assert (await other.get(f"{route}/ai/prompt")).status_code == 404
    result = await other.post(f"{route}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert result.status_code in (403, 404, 409)
    await other.aclose()
    changed = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "another-model"})
    assert changed.status_code == 200, changed.text
    result = await client.post(f"{route}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert result.status_code in (400, 403, 409), result.text
    assert (await client.get(route)).status_code == 404


@pytest.mark.parametrize(("path", "limit"), [
    ("/api/v1/projects/1/settings/character/1/thumbnail/ai/generate", 64 * 1024),
    ("/api/v1/series/1/entries/1/thumbnail/ai/generate", 64 * 1024),
    ("/api/v1/projects/1/settings/character/1/thumbnail/ai/apply", 768 * 1024),
    ("/api/v1/series/1/entries/1/thumbnail/ai/apply", 768 * 1024),
])
async def test_thumbnail_ai_body_is_limited_before_json_parsing(client, path, limit):
    response = await client.post(path, content=b"x" * (limit + 1), headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and "过大" in response.json()["detail"]

    async def chunks():
        yield b"x" * limit
        yield b"x"

    response = await client.post(path, content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and "过大" in response.json()["detail"]
