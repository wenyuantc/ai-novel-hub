"""AI 配音的授权、快照、真实模型绑定与用量记录走完整 ASGI 请求。"""

import asyncio
import json

import pytest
from sqlalchemy import select

from app.models.ai_usage import AiUsageCall
from tests.helpers import Gate, create_ai_config, create_project, register
from tests.test_collaboration_cover_ai import _invite

CONTENT = '🌧屋外正在下雨。林夏朝小舟点头：“我们走吧。”'


async def setup_chapter(client):
    owner = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = await client.post(
        f"{base}/chapters", json={"title": "渡口", "content": CONTENT},
    )
    assert chapter.status_code == 201, chapter.text
    reader_url = f"{base}/reader/chapters/{chapter.json()['id']}"
    saved = await client.get(reader_url)
    assert saved.status_code == 200, saved.text
    start = CONTENT.index("“")
    payload = {
        "content_hash": saved.json()["content_hash"],
        "cues": [{"start": start, "end": len(CONTENT)}],
        "voices": [{"id": "local-ting", "name": "婷婷", "lang": "zh-CN"}],
    }
    return owner, project, reader_url, payload


def model_answer():
    return {
        "cues": [{"cue": 0, "speaker": "林夏", "emotion": "happy"}],
        "voices": [{"speaker": "林夏", "voice": 0}, {"speaker": "@narrator", "voice": 0}],
    }


async def test_explicit_editor_model_uses_saved_chapter_and_tracks_its_payer(client, make_client, app, fake_llm):
    _, project, reader_url, payload = await setup_chapter(client)
    guest = make_client()
    editor = await register(guest, "casting_editor")
    await _invite(client, guest, f"/api/v1/projects/{project['id']}", "casting_editor")
    await create_ai_config(guest, model="other-text-model")
    selected = await create_ai_config(guest, model="selected-voice-director")
    payload["ai_config_id"] = selected["id"]
    fake_llm.push(json.dumps(model_answer(), ensure_ascii=False))

    response = await guest.post(f"{reader_url}/ai-casting", json=payload)

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["content_hash"] == payload["content_hash"]
    assert result["cues"] == [{"start": CONTENT.index("“"), "speaker": "林夏", "emotion": "happy"}]
    assert {v["speaker"]: v["voice_id"] for v in result["voices"]} == {"林夏": "local-ting", "@narrator": "local-ting"}
    assert len(fake_llm.calls) == 1
    call = fake_llm.calls[0]
    assert call["model"] == "selected-voice-director"
    sent = json.loads(call["messages"][-1]["content"])
    assert sent["chapter"] == CONTENT
    assert sent["voices"] == [{"voice": 0, "name": "婷婷", "lang": "zh-CN"}]
    assert "local-ting" not in call["messages"][-1]["content"]
    async with app.state.sessionmaker() as db:
        record = (await db.scalars(select(AiUsageCall))).one()
        assert record.actor_id == record.payer_id == editor["id"]
        assert record.kind == "reader.ai_casting"
        assert record.chapter_id == int(reader_url.rsplit("/", 1)[-1])
        assert record.project_id == project["id"]
        assert record.status == "succeeded"
    assert (await guest.get(reader_url)).json()["chapter"]["content"] == CONTENT


async def test_viewer_and_old_chapter_hash_cannot_send_to_provider(client, make_client, fake_llm):
    _, project, reader_url, payload = await setup_chapter(client)
    viewer = make_client()
    await register(viewer, "casting_viewer")
    await _invite(client, viewer, f"/api/v1/projects/{project['id']}", "casting_viewer", role="viewer")
    await create_ai_config(viewer)
    await create_ai_config(client)
    assert (await viewer.get(reader_url)).status_code == 200
    rejected = await viewer.post(f"{reader_url}/ai-casting", json=payload)
    assert rejected.status_code in {403, 404}, rejected.text
    rejected = await client.post(f"{reader_url}/ai-casting", json={**payload, "content_hash": "0" * 64})
    assert rejected.status_code == 409, rejected.text
    assert not fake_llm.calls


@pytest.mark.parametrize("alteration", ["malformed", "unknown_voice"])
async def test_invalid_cast_does_not_retry_or_return_partial_result(client, fake_llm, alteration):
    _, _, reader_url, payload = await setup_chapter(client)
    await create_ai_config(client)
    bad = model_answer()
    bad["voices"][0]["voice"] = 7
    fake_llm.push("not-json" if alteration == "malformed" else json.dumps(bad))

    response = await client.post(f"{reader_url}/ai-casting", json=payload)

    assert response.status_code >= 400
    assert "cues" not in response.json()
    assert len(fake_llm.calls) == 1
    assert (await client.get(reader_url)).json()["content_hash"] == payload["content_hash"]


async def test_revision_changed_while_model_ran_discards_returned_cast(client, fake_llm):
    _, _, reader_url, payload = await setup_chapter(client)
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push([gate, json.dumps(model_answer(), ensure_ascii=False)])
    pending = asyncio.create_task(client.post(f"{reader_url}/ai-casting", json=payload))
    try:
        await gate.wait_reached()
        edit = await client.patch(reader_url.replace("/reader/chapters/", "/chapters/"), json={"content": "新的正文"})
        assert edit.status_code == 200, edit.text
    finally:
        gate.release()
    result = await pending
    assert result.status_code == 409, result.text
    assert "cues" not in result.json()
    assert len(fake_llm.calls) == 1
