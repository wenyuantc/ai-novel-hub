"""明确确认的关闭区块选区，仅当前操作单次发送，且采纳绑定原目标和快照。"""

import asyncio
import json
from hashlib import sha256
from uuid import uuid4

import pytest

from tests.helpers import create_ai_config, parse_sse, register
from tests.test_rich_document_api import KEY, MARKER, SECTION_AFTER, add_section, save_payload
from tests.test_scenes import setup_scene

ALLOWED = "87a7e858-6cb4-4215-b753-359ba054ff03"


async def _setup(client):
    project, _chapter, url, scene = await setup_scene(client, "允许的正式正文")
    await create_ai_config(client)
    document_url = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(document_url)).json())
    add_section(body)
    body["document"]["sections"][0].update(title="禁止发送的关闭标题", content="关闭前缀选中🙂关闭后缀")
    body["document"]["sections"].append({
        "key": SECTION_AFTER, "anchor": 2, "order": 0, "title": "另一个关闭标题",
        "content": "另一个关闭正文", "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []},
        "include_in_ai": False,
    })
    body["add_section_keys"].append(SECTION_AFTER)
    body["document"]["sections"].append({
        "key": ALLOWED, "anchor": 3, "order": 0, "title": "允许参照", "content": "允许的区块资料",
        "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []}, "include_in_ai": True,
    })
    body["add_section_keys"].append(ALLOWED)
    saved = await client.put(document_url, json=body)
    assert saved.status_code == 200, saved.text
    revisions = {f"expected_{key}": saved.json()[key] for key in ("content_revision", "planning_revision")}
    selected = "选中🙂"
    request = {**revisions, "request_id": str(uuid4()), "confirmed": True, "op": "rewrite", "start": 4,
               "end": 7, "selected_hash": sha256(selected.encode()).hexdigest()}
    return project, url, scene, document_url, request


async def test_closed_section_only_confirmed_selection_leaves_for_one_operation(client, fake_llm):
    _project, url, scene, document_url, request = await _setup(client)
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    denied = await client.post(path, json={**request, "confirmed": False})
    assert denied.status_code in (409, 422), denied.text
    assert not fake_llm.calls
    fake_llm.push("改好🙂")
    preview = await client.post(path, json=request)
    events = parse_sse(preview.text)
    assert events[-1][0] == "done", preview.text
    prompt = "\n".join(item["content"] for item in fake_llm.calls[-1]["messages"])
    assert "选中🙂" in prompt and "允许的正式正文" in prompt and "允许的区块资料" in prompt
    for private in ("禁止发送的关闭标题", "关闭前缀", "关闭后缀", "另一个关闭标题", "另一个关闭正文"):
        assert private not in prompt
    assert len(fake_llm.calls) == 1
    duplicate = await client.post(path, json=request)
    assert duplicate.status_code == 409 and len(fake_llm.calls) == 1
    changed_target = await client.post(f"{url}/scenes/{scene['id']}/sections/{SECTION_AFTER}/rewrite", json=request)
    assert changed_target.status_code == 409 and len(fake_llm.calls) == 1

    done = events[-1][1]
    assert done["section_key"] == KEY and done["request_id"] == request["request_id"]
    applied = await client.post(f"{path}/apply", json={
        **{key: request[key] for key in ("expected_content_revision", "expected_planning_revision", "request_id")},
        "log_id": done["log_id"], "start": 4, "end": 7, "original": "选中🙂", "replacement": done["text"],
    })
    assert applied.status_code == 200, applied.text
    document = (await client.get(document_url)).json()["document"]
    assert document["content"] == "允许的正式正文"
    assert document["sections"][0]["content"] == "关闭前缀改好🙂关闭后缀"
    assert document["sections"][0]["include_in_ai"] is False
    assert document["sections"][1]["content"] == "另一个关闭正文"


@pytest.mark.parametrize("confirmed", [False, 1, "true"])
async def test_only_true_boolean_is_explicit_confirmation(client, fake_llm, confirmed):
    _project, url, scene, _document_url, request = await _setup(client)
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    denied = await client.post(path, json={**request, "confirmed": confirmed})
    assert denied.status_code == 422 and not fake_llm.calls
    # 无效确认不能预先占用该 UUID；真正的 true 可执行且只发一次。
    fake_llm.push("确认成功")
    result = await client.post(path, json=request)
    assert parse_sse(result.text)[-1][0] == "done" and len(fake_llm.calls) == 1


async def test_closed_section_rejects_stale_selection_and_revalidates_before_apply(client, fake_llm):
    _project, url, scene, document_url, request = await _setup(client)
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    wrong = await client.post(path, json={**request, "selected_hash": "0" * 64})
    assert wrong.status_code == 409 and not fake_llm.calls
    fake_llm.push("预览")
    preview = await client.post(path, json=request)
    events = parse_sse(preview.text)
    assert events[-1][0] == "done", preview.text
    edited = save_payload((await client.get(document_url)).json())
    edited["document"]["sections"][0]["content"] += "改动"
    saved = await client.put(document_url, json=edited)
    assert saved.status_code == 200, saved.text
    done = events[-1][1]
    applied = await client.post(f"{path}/apply", json={
        **{key: request[key] for key in ("expected_content_revision", "expected_planning_revision", "request_id")},
        "log_id": done["log_id"], "start": 4, "end": 7, "original": "选中🙂", "replacement": "预览",
    })
    assert applied.status_code == 409, applied.text
    assert (await client.get(document_url)).json() == saved.json()


async def test_confirmation_uuid_concurrent_replay_cannot_send_twice(client, fake_llm):
    _project, url, scene, _document_url, request = await _setup(client)
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    fake_llm.push("唯一一次")
    first, second = await asyncio.gather(client.post(path, json=request), client.post(path, json=request))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner = first if first.status_code == 200 else second
    assert parse_sse(winner.text)[-1][0] == "done"
    assert len(fake_llm.calls) == 1


async def test_real_redirect_does_not_send_confirmed_section_selection_twice(app, client):
    from uuid import UUID

    from app.ai.providers import OpenAICompatProvider
    from app.schemas.scene_ai import SectionRewriteRequest
    from app.services import rewrite_service, rich_document_section_ai
    from tests.test_ai_visibility_dispatch import _server

    project, _url, scene, _document_url, request = await _setup(client)
    async with _server(status="307 Temporary Redirect", mime="application/json", headers={"Location": "/next"}) as (
        server, _received, send_headers, send_body, requests,
    ):
        send_headers.set()
        send_body.set()

        def factory(_kind, key, _base, timeout):
            return OpenAICompatProvider(key, f"{server}/v1/", timeout)

        async with app.state.sessionmaker() as db:
            job = await rich_document_section_ai.prepare_section_rewrite(
                db, user_id=1, project_id=project["id"], chapter_id=scene["chapter_id"], scene_id=scene["id"],
                section_key=UUID(KEY), req=SectionRewriteRequest.model_validate(request), cipher=app.state.cipher,
                factory=factory, ai_timeout=5, allow_private=True, context_budget=4_000,
            )
        events = [event async for event in rewrite_service.run_rewrite_job(job, app.state.sessionmaker)]
        assert events[-1][0] == "error" and len(requests) == 1
        sent = json.loads(requests[0])
        text = "\n".join(item["content"] for item in sent["messages"])
        assert "选中🙂" in text and "关闭前缀" not in text and "关闭后缀" not in text


async def test_grant_rechecked_after_preparation_before_first_model_send(app, client, fake_llm):
    from uuid import UUID

    from app.schemas.scene_ai import SectionRewriteRequest
    from app.services import rewrite_service, rich_document_section_ai

    project, _url, scene, document_url, request = await _setup(client)
    async with app.state.sessionmaker() as db:
        job = await rich_document_section_ai.prepare_section_rewrite(
            db, user_id=1, project_id=project["id"], chapter_id=scene["chapter_id"], scene_id=scene["id"],
            section_key=UUID(KEY), req=SectionRewriteRequest.model_validate(request), cipher=app.state.cipher,
            factory=app.state.provider_factory, ai_timeout=5, allow_private=True, context_budget=4_000,
        )
    changed = save_payload((await client.get(document_url)).json())
    changed["document"]["sections"][0]["content"] += "修改"
    assert (await client.put(document_url, json=changed)).status_code == 200
    fake_llm.push("不可发送")
    events = [event async for event in rewrite_service.run_rewrite_job(job, app.state.sessionmaker)]
    assert events[-1][0] == "error" and not fake_llm.calls


async def test_section_rewrite_preserves_unaffected_format_and_stales_covered_marker(client, fake_llm):
    _project, url, scene, document_url, request = await _setup(client)
    body = save_payload((await client.get(document_url)).json())
    body["document"]["sections"][0]["formatting"]["inline"] = [{"kind": "bold", "start": 7, "end": 11}]
    body["document"]["markers"].append({
        "key": MARKER, "section_key": KEY, "status": "active", "start": 4, "end": 7,
        "color": "red", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(document_url, json=body)
    assert saved.status_code == 200, saved.text
    request["expected_content_revision"] = saved.json()["content_revision"]
    fake_llm.push("改得更好🙂")
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    preview = await client.post(path, json=request)
    events = parse_sse(preview.text)
    assert events[-1][0] == "done", preview.text
    done = events[-1][1]
    applied = await client.post(f"{path}/apply", json={
        **{key: request[key] for key in ("expected_content_revision", "expected_planning_revision", "request_id")},
        "log_id": done["log_id"], "start": 4, "end": 7, "original": "选中🙂", "replacement": done["text"],
    })
    assert applied.status_code == 200, applied.text
    document = applied.json()["document"]
    assert document["sections"][0]["formatting"]["inline"] == [{"kind": "bold", "start": 9, "end": 13}]
    marker = document["markers"][0]
    assert (marker["section_key"], marker["status"], marker["excerpt"], marker["excerpt_truncated"]) == (
        KEY, "stale", "选中🙂", False,
    )


async def test_section_rewrite_format_loss_requires_confirmation_and_preserves_unaffected(client, fake_llm):
    _project, url, scene, document_url, request = await _setup(client)
    body = save_payload((await client.get(document_url)).json())
    body["document"]["sections"][0]["formatting"] = {
        "version": 1, "boundaries": [],
        "inline": [{"kind": "bold", "start": 2, "end": 9}, {"kind": "italic", "start": 0, "end": 3}],
        "blocks": [{"kind": "heading", "start": 0, "end": 11, "level": 2}],
    }
    body["document"]["markers"].append({
        "key": MARKER, "section_key": KEY, "status": "active", "start": 4, "end": 7,
        "color": "red", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(document_url, json=body)
    assert saved.status_code == 200, saved.text
    before = saved.json()
    request["expected_content_revision"] = before["content_revision"]
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    fake_llm.push("新文")
    events = parse_sse((await client.post(path, json=request)).text)
    assert events[-1][0] == "done"
    apply = {**{key: request[key] for key in ("expected_content_revision", "expected_planning_revision", "request_id")},
             "log_id": events[-1][1]["log_id"], "start": 4, "end": 7, "original": "选中🙂", "replacement": "新文"}
    refused = await client.post(f"{path}/apply", json=apply)
    assert refused.status_code == 409 and refused.json()["code"] == "rich_document_confirmation_required"
    assert (await client.get(document_url)).json() == before
    for invalid in [1, "true"]:
        assert (await client.post(f"{path}/apply", json=apply | {"confirm_format_loss": invalid})).status_code == 422
    assert (await client.post(f"{path}/apply", json=apply | {"confirm_format_loss": False})).status_code == 409
    assert (await client.get(document_url)).json() == before and len(fake_llm.calls) == 1
    accepted = await client.post(f"{path}/apply", json=apply | {"confirm_format_loss": True})
    assert accepted.status_code == 200, accepted.text
    document = accepted.json()["document"]
    changed = document["sections"][0]
    assert changed["content"] == "关闭前缀新文关闭后缀"
    assert changed["formatting"]["inline"] == [
        {"kind": "bold", "start": 2, "end": 4}, {"kind": "bold", "start": 6, "end": 8},
        {"kind": "italic", "start": 0, "end": 3},
    ]
    assert changed["formatting"]["blocks"] == [{"kind": "heading", "start": 0, "end": 10, "level": 2}]
    assert document["markers"][0]["status"] == "stale" and document["markers"][0]["excerpt"] == "选中🙂"
    assert document["sections"][1:] == before["document"]["sections"][1:]
    assert (await client.post(f"{path}/apply", json=apply | {"confirm_format_loss": True})).status_code == 409
    assert len(fake_llm.calls) == 1


async def test_section_confirmation_is_scoped_to_own_project(client, make_client, fake_llm):
    _project, url, scene, _document_url, request = await _setup(client)
    other = make_client()
    await register(other, "section_outsider")
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    denied = await other.post(path, json=request)
    assert denied.status_code == 404 and not fake_llm.calls
    fake_llm.push("仅本人")
    preview = await client.post(path, json=request)
    events = parse_sse(preview.text)
    assert events[-1][0] == "done" and len(fake_llm.calls) == 1
    done = events[-1][1]
    apply = {**{key: request[key] for key in ("expected_content_revision", "expected_planning_revision", "request_id")},
             "log_id": done["log_id"], "start": 4, "end": 7, "original": "选中🙂", "replacement": done["text"]}
    assert (await other.post(f"{path}/apply", json=apply)).status_code == 404
    assert (await client.post(f"{path}/apply", json=apply)).status_code == 200


@pytest.mark.parametrize("change", ["disabled", "token_version"])
async def test_section_send_rechecks_authenticated_actor_before_first_http_send(
    app, client, fake_llm, monkeypatch, change,
):
    from sqlalchemy import update

    from app.models import User
    from app.services import rewrite_service

    _project, url, scene, _document_url, request = await _setup(client)
    path = f"{url}/scenes/{scene['id']}/sections/{KEY}/rewrite"
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = rewrite_service.run_rewrite_job

    async def delay(job, sessionmaker):
        entered.set()
        await proceed.wait()
        async for event in original(job, sessionmaker):
            yield event

    monkeypatch.setattr(rewrite_service, "run_rewrite_job", delay)
    pending = asyncio.create_task(client.post(path, json=request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with app.state.sessionmaker() as db:
            values = {"is_active": False} if change == "disabled" else {"token_version": User.token_version + 1}
            await db.execute(update(User).where(User.id == 1).values(**values))
            await db.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, 5)
        assert parse_sse(response.text)[-1][0] == "error"
        assert not fake_llm.calls
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
