"""设定图片提示词复用个人模板，并始终按实际发起人解析。"""

import pytest

from app.ai.prompts.setting_thumbnail import build_thumbnail_prompt
from app.ai.providers.base import GeneratedImage
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_cover_ai import _invite
from tests.test_series_thumbnail_ai import _entry
from tests.test_setting_thumbnails import image_data

KEY = "setting.thumbnail"
TEMPLATE = f"/api/v1/prompt-templates/{KEY}"
ENDING = "画面中不要出现除了当前角色以外的人物、文字、字母、数字或水印。"


def _legacy_prompt(kind, fields):
    subject = {"character": "角色", "world": "世界观", "organization": "组织", "career": "职业"}[kind]
    intro = f"请根据以下{subject}设定绘制一张具有清晰主体和鲜明氛围的小说设定插图。"
    budget = 2000 - len(intro) - len(ENDING) - 2
    parts = [intro]
    for label, value in fields:
        text = str(value).strip()
        if not text or budget <= len(label) + 2:
            continue
        line = f"{label}：{text[:min(400, budget - len(label) - 2)]}"
        parts.append(line)
        budget -= len(line) + 1
    return "\n".join([*parts, ENDING])


@pytest.mark.parametrize("kind", ["character", "world", "organization", "career"])
@pytest.mark.parametrize("fields", [[], [("名称", "甲"), ("简介", "蓝色眼睛")],
                                    [(f"字段{i}", "🦊" * 600) for i in range(12)]])
def test_default_prompt_preserves_user_edited_text_and_field_budget(kind, fields):
    actual = build_thumbnail_prompt(kind, fields)
    assert actual == _legacy_prompt(kind, fields)
    assert actual.endswith(ENDING)
    assert len(actual) <= 2000


async def _book_character(client):
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = await client.post(f"{base}/characters", json={
        "name": "青霜", "appearance": "银色长发", "profile": "不可发送的秘密",
    })
    assert character.status_code == 201, character.text
    item = character.json()
    settings = await client.patch(f"{base}/setting-entries/character/{item['id']}/ai-settings", json={
        "expected_ai_visibility_revision": 0, "ai_mode": "manual_only", "ai_hidden_builtin_fields": ["profile"],
    })
    assert settings.status_code == 200, settings.text
    return base, f"{base}/settings/character/{item['id']}/thumbnail"


async def test_saved_template_component_preview_and_restore_are_used_for_generation(client, fake_llm):
    await register(client)
    await create_ai_config(client, model_type="image", model="image-test")
    _, path = await _book_character(client)
    default = await client.get(f"{path}/ai/prompt")
    assert default.status_code == 200, default.text
    default_text = default.json()["prompt"]
    assert "银色长发" in default_text and "不可发送的秘密" not in default_text
    assert default_text.endswith(ENDING)

    detail = await client.get(TEMPLATE)
    assert detail.status_code == 200, detail.text
    assert detail.json()["name"] == "设定图片"
    assert {v["name"] for v in detail.json()["variables"]} == {"subject", "fields"}
    component = await client.post("/api/v1/prompt-components", json={
        "slug": "ink", "name": "墨色", "content": "水墨风格",
    })
    assert component.status_code == 201, component.text
    template = {"system": "横幅", "user": "{{> ink }}的{{ subject }}\n{{ fields }}"}
    saved = await client.put(TEMPLATE, json=template)
    assert saved.status_code == 200, saved.text
    assert saved.json()["override"]["user"] == template["user"]
    preview = await client.post(f"{TEMPLATE}/preview", json=template)
    assert preview.status_code == 200 and preview.json()["errors"] == [], preview.text
    assert [x["slug"] for x in preview.json()["components"]] == ["ink"]

    # 重新读取时展开本人的组件，隐藏字段不会进入模板变量。
    loaded = await client.get(f"{path}/ai/prompt")
    assert loaded.status_code == 200, loaded.text
    prompt = loaded.json()
    assert prompt["prompt"].startswith("横幅\n\n水墨风格的角色\n")
    assert "银色长发" in prompt["prompt"] and "不可发送的秘密" not in prompt["prompt"]
    edited_prompt = prompt["prompt"] + "\n仅此次调整"
    fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
    generated = await client.post(f"{path}/ai/generate", json={
        "prompt": edited_prompt, "prompt_token": prompt["prompt_token"], "expected_version": "",
    })
    assert generated.status_code == 200, generated.text
    assert fake_llm.image_calls[-1]["prompt"] == edited_prompt
    assert (await client.get(TEMPLATE)).json()["override"]["user"] == template["user"]
    assert (await client.get(f"{path}/ai/prompt")).json()["prompt"] == prompt["prompt"]

    disabled = await client.patch(TEMPLATE, json={"enabled": False})
    assert disabled.status_code == 200 and not disabled.json()["enabled"]
    assert (await client.get(f"{path}/ai/prompt")).json()["prompt"] == default_text
    assert (await client.patch(TEMPLATE, json={"enabled": True})).status_code == 200
    assert (await client.get(f"{path}/ai/prompt")).json()["prompt"] == prompt["prompt"]
    versions = await client.get(f"{TEMPLATE}/versions")
    assert versions.status_code == 200 and versions.json(), versions.text
    previous_id = versions.json()[0]["id"]
    assert (await client.put(TEMPLATE, json={"user": "临时版本"})).status_code == 200
    restored = await client.post(f"{TEMPLATE}/versions/{previous_id}/restore")
    assert restored.status_code == 200, restored.text
    assert (await client.get(f"{path}/ai/prompt")).json()["prompt"] == prompt["prompt"]
    assert (await client.delete(TEMPLATE)).status_code == 204
    assert (await client.get(f"{path}/ai/prompt")).json()["prompt"] == default_text


@pytest.mark.parametrize("value", ["", "   ", "🦊" * 2001])
async def test_empty_or_oversized_saved_prompt_is_reported(client, value):
    await register(client)
    _, path = await _book_character(client)
    saved = await client.put(TEMPLATE, json={"system": "", "user": value})
    assert saved.status_code == 200, saved.text
    result = await client.get(f"{path}/ai/prompt")
    assert result.status_code == 400, result.text
    assert "提示词" in result.json()["detail"] and "模板" in result.json()["detail"]


async def test_combined_system_and_user_prompt_length_is_validated(client):
    await register(client)
    _, path = await _book_character(client)
    saved = await client.put(TEMPLATE, json={"system": "系统", "user": "🦊" * 1997})
    assert saved.status_code == 200, saved.text
    result = await client.get(f"{path}/ai/prompt")
    assert result.status_code == 400, result.text
    assert "2000" in result.json()["detail"]


@pytest.mark.parametrize(("kind", "path", "body", "subject", "visible"), [
    ("character", "characters", {"name": "霜羽"}, "角色", "霜羽"),
    ("world", "world", {"title": "星港", "category": "place"}, "世界观", "星港"),
    ("organization", "organizations", {"name": "浮光会"}, "组织", "浮光会"),
    ("career", "careers", {"name": "观星师", "stages": [{"name": "初阶"}]}, "职业", "观星师"),
])
async def test_personal_template_is_used_for_each_book_setting(client, kind, path, body, subject, visible):
    await register(client)
    saved = await client.put(TEMPLATE, json={"user": "墨色{{ subject }}\n{{ fields }}"})
    assert saved.status_code == 200, saved.text
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post(f"{base}/{path}", json=body)
    assert created.status_code == 201, created.text
    prompt = await client.get(f"{base}/settings/{kind}/{created.json()['id']}/thumbnail/ai/prompt")
    assert prompt.status_code == 200, prompt.text
    assert prompt.json()["prompt"].startswith(f"墨色{subject}\n")
    assert visible in prompt.json()["prompt"]


async def test_project_editor_uses_own_template_even_when_owner_pays(client, make_client, fake_llm):
    await register(client, "prompt-owner")
    await create_ai_config(client, model_type="image", model="owner-image")
    base, path = await _book_character(client)
    saved = await client.put(TEMPLATE, json={"user": "【作者专属】{{ subject }}\n{{ fields }}"})
    assert saved.status_code == 200, saved.text
    guest = make_client()
    try:
        await register(guest, "prompt-editor")
        await _invite(client, guest, base, "prompt-editor", allow_owner_ai=True)
        before = await guest.get(f"{path}/ai/prompt")
        assert before.status_code == 200 and before.json()["prompt"].endswith(ENDING), before.text
        assert "【作者专属】" not in before.json()["prompt"]
        guest_saved = await guest.put(TEMPLATE, json={"user": "【协作者专属】{{ subject }}\n{{ fields }}"})
        assert guest_saved.status_code == 200, guest_saved.text
        own = (await client.get(f"{path}/ai/prompt")).json()["prompt"]
        response = await guest.get(f"{path}/ai/prompt")
        assert response.status_code == 200, response.text
        prompt = response.json()
        assert "【作者专属】" in own and "【协作者专属】" not in own
        assert "【协作者专属】" in prompt["prompt"] and "【作者专属】" not in prompt["prompt"]
        assert "不可发送的秘密" not in prompt["prompt"]
        fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
        generated = await guest.post(f"{path}/ai/generate", json={
            "prompt": prompt["prompt"], "prompt_token": prompt["prompt_token"], "expected_version": "",
        })
        assert generated.status_code == 200 and generated.json()["model_source"] == "owner", generated.text
        assert fake_llm.image_calls[-1]["prompt"] == prompt["prompt"]
    finally:
        await guest.aclose()


@pytest.mark.parametrize(("kind", "data", "hidden", "visible"), [
    ("character", {"name": "共享人物", "profile": "系列秘密", "appearance": "翠绿色眼睛"},
     "profile", "翠绿色眼睛"),
    ("world", {"title": "共享地点", "category": "place", "content": "系列秘密"}, "content", "共享地点"),
])
async def test_series_editor_uses_own_template_and_filtered_fields(client, make_client, fake_llm,
                                                            kind, data, hidden, visible):
    await register(client, "series-prompt-owner")
    owner_saved = await client.put(TEMPLATE, json={"user": "系列作者私有 {{ subject }}\n{{ fields }}"})
    assert owner_saved.status_code == 200, owner_saved.text
    base, _, path, revision = await _entry(client, kind, data, {
        "ai_mode": "manual_only", "ai_hidden_builtin_fields": [hidden],
    })
    guest = make_client()
    try:
        await register(guest, "series-prompt-editor")
        invited = await client.post(f"{base}/collaborators", json={"username": "series-prompt-editor"})
        assert invited.status_code == 201, invited.text
        invite = invited.json()
        accepted = await guest.post(f"/api/v1/collaboration/invitations/series/{invite['invitation_id']}/accept", json={
            "expected_revision": invite["revision"],
        })
        assert accepted.status_code == 200, accepted.text
        await create_ai_config(guest, model_type="image", model="editor-image")
        before = await guest.get(f"{path}/ai/prompt")
        assert before.status_code == 200 and before.json()["prompt"].endswith(ENDING), before.text
        assert "系列作者私有" not in before.json()["prompt"]
        saved = await guest.put(TEMPLATE, json={"user": "系列协作者 {{ subject }}\n{{ fields }}"})
        assert saved.status_code == 200, saved.text
        owner_prompt = (await client.get(f"{path}/ai/prompt")).json()["prompt"]
        response = await guest.get(f"{path}/ai/prompt")
        assert response.status_code == 200, response.text
        prompt = response.json()
        assert "系列作者私有" in owner_prompt
        assert "系列协作者" in prompt["prompt"] and "系列作者私有" not in prompt["prompt"]
        assert visible in prompt["prompt"] and "系列秘密" not in prompt["prompt"]
        fake_llm.images.append(GeneratedImage(data=image_data(), mime_type="image/png"))
        generated = await guest.post(f"{path}/ai/generate", json={
            "prompt": prompt["prompt"], "prompt_token": prompt["prompt_token"], "expected_version": "",
            "expected_revision": revision,
        })
        assert generated.status_code == 200, generated.text
        assert fake_llm.image_calls[-1]["prompt"] == prompt["prompt"]
        assert fake_llm.image_calls[-1]["model"] == "editor-image"
    finally:
        await guest.aclose()
