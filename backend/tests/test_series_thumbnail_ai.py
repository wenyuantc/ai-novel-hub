"""系列设定图的可见投影、本人模型和原邀请确认。"""

import base64

import pytest

from app.ai.providers.base import GeneratedImage
from tests.helpers import create_ai_config, register
from tests.test_setting_thumbnails import image_data


async def _entry(client, kind, data, ai_settings=None):
    series = await client.post("/api/v1/series", json={"name": "生图系列"})
    assert series.status_code == 201, series.text
    base = f"/api/v1/series/{series.json()['id']}"
    body = {"expected_revision": series.json()["revision"], "kind": kind, "data": data}
    if ai_settings is not None:
        body["ai_settings"] = ai_settings
    created = await client.post(f"{base}/entries", json=body)
    assert created.status_code == 201, created.text
    row = created.json()
    path = f"{base}/entries/{row['item']['id']}/thumbnail"
    return base, row["item"]["id"], path, row["revision"]


async def _prompt(client, path):
    result = await client.get(f"{path}/ai/prompt")
    assert result.status_code == 200, result.text
    return result.json()


async def _generate(client, path, prompt_info, revision, **overrides):
    body = {"prompt": prompt_info["prompt"], "prompt_token": prompt_info["prompt_token"], "expected_version": "",
            "expected_revision": revision}
    body.update(overrides)
    return await client.post(f"{path}/ai/generate", json=body)


@pytest.mark.parametrize(("kind", "data", "hidden", "visible"), [
    ("character", {"name": "角色甲", "profile": "私密简介", "appearance": "银色短发"}, "profile", "银色短发"),
    ("world", {"title": "青石城", "category": "place", "content": "私密简介"}, "content", "青石城"),
])
async def test_series_prompt_projects_only_visible_fields_and_confirmation_saves_preview(
    client, fake_llm, kind, data, hidden, visible,
):
    await register(client)
    await create_ai_config(client, model_type="image", model="my-image")
    _, _, path, revision = await _entry(client, kind, data, {
        "ai_mode": "manual_only", "ai_hidden_builtin_fields": [hidden],
    })
    prompt = await _prompt(client, path)
    assert visible in prompt["prompt"]
    assert "私密简介" not in prompt["prompt"]
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    result = await _generate(client, path, prompt, revision, prompt="用户编辑的提示词")
    assert result.status_code == 200, result.text
    preview = result.json()
    assert preview["mime_type"] == "image/webp" and preview["model_source"] == "actor"
    assert fake_llm.image_calls[-1]["model"] == "my-image"
    assert fake_llm.image_calls[-1]["prompt"] == "用户编辑的提示词"
    assert (await client.get(path)).status_code == 404

    applied = await client.post(f"{path}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"] == revision + 1
    assert (await client.get(path)).content == base64.b64decode(preview["image_base64"])
    assert applied.json()["thumbnail"]["width"] == 512


async def test_series_never_and_stale_prompt_stop_before_provider(client, fake_llm):
    await register(client)
    await create_ai_config(client, model_type="image")
    base, eid, path, revision = await _entry(client, "character", {"name": "保密角色"})
    old = await _prompt(client, path)
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    generated = await _generate(client, path, old, revision)
    assert generated.status_code == 200, generated.text
    preview = generated.json()
    changed = await client.patch(f"{base}/entries/{eid}", json={
        "expected_revision": revision, "ai_settings": {"ai_mode": "never"},
    })
    assert changed.status_code == 200, changed.text
    blocked = await _generate(client, path, old, revision)
    assert blocked.status_code == 409 and blocked.json()["code"] == "thumbnail_ai_changed", blocked.text
    assert len(fake_llm.image_calls) == 1
    applied = await client.post(f"{path}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert applied.status_code == 409 and applied.json()["code"] == "thumbnail_ai_changed", applied.text
    assert (await client.get(path)).status_code == 404
    forbidden = await client.get(f"{path}/ai/prompt")
    assert forbidden.status_code == 400 and forbidden.json()["code"] == "setting_not_available"


async def test_series_preview_checks_model_and_picture_digest(client, fake_llm):
    await register(client)
    cfg = await create_ai_config(client, model_type="image")
    _, _, path, revision = await _entry(client, "character", {"name": "角色"})
    prompt = await _prompt(client, path)
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    generated = await _generate(client, path, prompt, revision)
    assert generated.status_code == 200, generated.text
    preview = generated.json()
    tampered = await client.post(f"{path}/ai/apply", json={
        "image_base64": base64.b64encode(b"not the preview").decode(), "preview_token": preview["preview_token"],
    })
    assert tampered.status_code == 409, tampered.text
    assert (await client.get(path)).status_code == 404

    changed = await client.patch(f"/api/v1/ai-configs/{cfg['id']}", json={"model": "image-changed"})
    assert changed.status_code == 200, changed.text
    blocked = await client.post(f"{path}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert blocked.status_code == 409, blocked.text
    assert (await client.get(path)).status_code == 404


async def test_series_editor_uses_only_own_model_and_old_preview_cannot_use_reinvitation(client, make_client, fake_llm):
    await register(client, "series-ai-owner")
    await create_ai_config(client, model_type="image", model="owner-image")
    guest = make_client()
    guest_user = await register(guest, "series-ai-editor")
    base, _, path, revision = await _entry(client, "character", {"name": "共享角色"})
    invited = await client.post(f"{base}/collaborators", json={"username": "series-ai-editor"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await guest.post(f"/api/v1/collaboration/invitations/series/{invitation['invitation_id']}/accept", json={
        "expected_revision": invitation["revision"],
    })
    assert accepted.status_code == 200, accepted.text
    prompt = await _prompt(guest, path)
    missing = await _generate(guest, path, prompt, revision)
    assert missing.status_code == 400 and fake_llm.image_calls == []
    await create_ai_config(guest, model_type="image", model="editor-image")
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    generated = await _generate(guest, path, prompt, revision)
    assert generated.status_code == 200, generated.text
    assert fake_llm.image_calls[-1]["model"] == "editor-image"

    revoked = await client.delete(f"{base}/collaborators/{guest_user['id']}", params={
        "expected_revision": accepted.json()["revision"],
    })
    assert revoked.status_code == 204, revoked.text
    reinvited = await client.post(f"{base}/collaborators", json={"username": "series-ai-editor"})
    assert reinvited.status_code == 201, reinvited.text
    second = reinvited.json()
    renewed = await guest.post(f"/api/v1/collaboration/invitations/series/{second['invitation_id']}/accept", json={
        "expected_revision": second["revision"],
    })
    assert renewed.status_code == 200, renewed.text
    preview = generated.json()
    blocked = await guest.post(f"{path}/ai/apply", json={
        "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
    })
    assert blocked.status_code == 409, blocked.text
    assert (await client.get(path)).status_code == 404
