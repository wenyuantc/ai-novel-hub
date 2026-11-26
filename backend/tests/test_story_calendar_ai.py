"""AI 候选经过作品模型绑定、用量、快照与正常历法保存入口。"""

import asyncio
import json

import pytest
from sqlalchemy import select

from app.core.errors import BadRequestError
from app.models import AiUsageCall
from app.schemas.story_timeline import CalendarOut
from app.services.story_calendar_ai import parse_draft
from tests.helpers import Gate, create_ai_config, create_project, register
from tests.test_collaboration_cover_ai import _invite
from tests.test_story_timeline import CALENDAR

ANSWER = {
    "era_name": "归航纪",
    "months": [{"existing_id": "snow", "name": "冬至月", "days": 16},
               {"existing_id": None, "name": "雾月", "days": 20},
               {"existing_id": None, "name": "归航月", "days": 7}],
}


async def test_owner_model_usage_and_revocation_before_delivering_draft(app, client, make_client, fake_llm):
    owner = await register(client)
    project = await create_project(client)
    editor = make_client()
    actor = await register(editor, "calendar-guest")
    base = f"/api/v1/projects/{project['id']}"
    accepted = await _invite(client, editor, base, actor["username"], allow_owner_ai=True)
    await create_ai_config(client, model="owners-calendar-model")
    calendar = f"{base}/story-timeline/calendar"
    payload = {"expected_revision": 0}
    answer = json.dumps({"era_name": "初元", "months": [{"existing_id": None, "name": "初月", "days": 30}]})
    fake_llm.push(answer)
    result = await editor.post(f"{calendar}/generate", json=payload)
    assert result.status_code == 200, result.text
    assert fake_llm.calls[0]["model"] == "owners-calendar-model"
    async with app.state.sessionmaker() as db:
        call = (await db.scalars(select(AiUsageCall))).one()
        assert (call.actor_id, call.payer_id, call.project_id) == (actor["id"], owner["id"], project["id"])
    gate = Gate()
    fake_llm.push([gate, answer])
    pending = asyncio.create_task(editor.post(f"{calendar}/generate", json=payload))
    try:
        await gate.wait_reached()
        updated = await client.patch(f"{base}/collaborators/{actor['id']}", json={
            "expected_revision": accepted["revision"], "allow_owner_ai": False,
        })
        assert updated.status_code == 200, updated.text
    finally:
        gate.release()
    withheld = await pending
    assert withheld.status_code in {403, 404, 409}, withheld.text
    assert "months" not in withheld.json()
    assert (await editor.get(calendar)).json() == {"revision": 0, "era_name": None, "months": []}


async def test_editor_generates_with_model_and_reviews_before_save(app, client, make_client, fake_llm):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    root = f"{base}/story-timeline/calendar"
    saved = (await client.put(root, json=CALENDAR)).json()
    editor = make_client()
    actor = await register(editor, "calendar-editor")
    await _invite(client, editor, base, "calendar-editor")
    cfg = await create_ai_config(editor, model="calendar-model")
    fake_llm.push(json.dumps(ANSWER, ensure_ascii=False))
    request = {"expected_revision": 1, "instruction": "请添加航海月", "ai_config_id": cfg["id"]}
    result = await editor.post(f"{root}/generate", json=request)
    assert result.status_code == 200, result.text
    draft = result.json()
    assert draft["revision"] == 1 and draft["era_name"] == "归航纪"
    assert [m["id"] for m in draft["months"]][:2] == ["snow", "mist"]
    assert draft["months"][2]["id"] not in {"snow", "mist"}
    assert (await client.get(root)).json() == saved
    call, = fake_llm.calls
    assert call["model"] == "calendar-model"
    sent = json.loads(call["messages"][-1]["content"])
    assert sent == {"calendar": saved, "instruction": request["instruction"],
                    "project": {key: project[key] for key in ("title", "genre", "premise")}}
    async with app.state.sessionmaker() as db:
        record = (await db.scalars(select(AiUsageCall))).one()
        assert record.actor_id == record.payer_id == actor["id"] and record.project_id == project["id"]
        assert record.kind == "story_timeline.calendar.generate" and record.status == "succeeded"
    proposed = {"expected_revision": 1, "era_name": draft["era_name"], "months": draft["months"]}
    assert (await editor.post(f"{root}/preview", json=proposed)).json() == {"blocked": [], "reordered": []}
    assert (await editor.put(root, json=proposed)).json() == draft | {"revision": 2}


async def test_viewer_and_stale_revision_cannot_call_model_and_inflight_edit_discards_draft(
    client, make_client, fake_llm,
):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    calendar = f"{base}/story-timeline/calendar"
    generate = f"{calendar}/generate"
    await create_ai_config(client)
    viewer = make_client()
    await register(viewer, "calendar-viewer")
    await _invite(client, viewer, base, "calendar-viewer", role="viewer")
    assert (await viewer.post(generate, json={"expected_revision": 0})).status_code in {403, 404}
    assert (await client.post(generate, json={"expected_revision": 1})).status_code == 409
    assert not fake_llm.calls
    gate = Gate()
    fake_llm.push([gate, '{"era_name":"草稿","months":[{"existing_id":null,"name":"新月","days":12}]}'])
    pending = asyncio.create_task(client.post(generate, json={"expected_revision": 0}))
    try:
        await gate.wait_reached()
        saved = await client.put(calendar, json=CALENDAR)
        assert saved.status_code == 200, saved.text
    finally:
        gate.release()
    assert (await pending).status_code == 409
    assert (await client.get(calendar)).json() == saved.json()
    assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("raw", [
    "模型回答了散文",
    '{"era_name":"新历","months":[]}',
    json.dumps(ANSWER | {"months": [{"existing_id": "fabricated", "name": "未知", "days": 1}]}),
    json.dumps(ANSWER | {"months": [ANSWER["months"][0]] * 2}),
    json.dumps(ANSWER | {"months": [ANSWER["months"][0] | {"days": 0}]}),
    json.dumps(ANSWER | {"months": [ANSWER["months"][0] | {"days": True}]}),
])
def test_invalid_proposals_are_rejected(raw):
    current = CalendarOut(revision=1, era_name=CALENDAR["era_name"], months=CALENDAR["months"])
    with pytest.raises(BadRequestError, match="模型返回的历法"):
        parse_draft(raw, current)


async def test_owner_fallback_charges_owner_and_revocation_discards_draft(client, make_client, fake_llm, app):
    owner = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    calendar = f"{base}/story-timeline/calendar"
    editor_client = make_client()
    editor = await register(editor_client, "calendar-fallback")
    grant = await _invite(client, editor_client, base, "calendar-fallback", allow_owner_ai=True)
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push([gate, '{"era_name":"霜纪","months":[{"existing_id":null,"name":"雪月","days":20}]}'])
    pending = asyncio.create_task(editor_client.post(f"{calendar}/generate", json={"expected_revision": 0}))
    try:
        await gate.wait_reached()
        revoked = await client.delete(f"{base}/collaborators/{editor['id']}",
                                      params={"expected_revision": grant["revision"]})
        assert revoked.status_code == 204, revoked.text
    finally:
        gate.release()
    assert (await pending).status_code in {403, 404, 409}
    assert not (await client.get(calendar)).json()["months"]
    async with app.state.sessionmaker() as db:
        record = (await db.scalars(select(AiUsageCall))).one()
        assert (record.actor_id, record.payer_id) == (editor["id"], owner["id"])
