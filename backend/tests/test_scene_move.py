"""从实际移动入口验证场景身份、投影、继承和故事时间的双章事务。"""

import pytest
from sqlalchemy import select

from app.models import Chapter, ChapterVersion, Scene, SettingTimeline, Task
from app.services.generation_service import BATCH_KIND
from tests.helpers import create_project, register
from tests.test_batch_revision import chapter as create_chapter
from tests.test_rich_document_api import MARKER, add_section, save_payload
from tests.test_scene_planning import planning_batch, scene_setup
from tests.test_scene_progressions import _scene_change, _timeline


def scenes_path(base, chapter):
    return f"{base}/chapters/{chapter['id']}/scenes"


async def scene_page(client, scenes_url):
    response = await client.get(scenes_url)
    assert response.is_success, response.text
    return response.json()


async def preview(client, source, scene_id, target, before=None):
    request = {"target_chapter_id": target["id"], "insert_before_scene_id": before}
    response = await client.post(f"{source}/{scene_id}/move-preview", json=request)
    assert response.status_code == 200, response.text
    return response.json(), {**request, "signature": response.json()["signature"]}


async def test_move_preserves_document_and_reinherits_target_planning(client, app):
    project, base, origin, source, scenes = await scene_setup(client, content="原景👩‍🚀\r\n")
    scene = scenes[1]
    sid = scene["id"]
    moved_text = "第二段"
    target_chapter = await create_chapter(client, base, 2, "目的稿\u3000  ")
    target = scenes_path(base, target_chapter)
    right = (await scene_page(client, target))["items"][0]
    source_character = (await client.post(f"{base}/characters", json={"name": "源章选角"})).json()
    target_character = (await client.post(f"{base}/characters", json={"name": "目标章选角"})).json()

    for chapter, character in ((origin, source_character), (target_chapter, target_character)):
        resp = await planning_batch(
            client, base, source, [{"kind": "chapter", "id": chapter["id"]}],
            pov={"op": "set", "character_id": character["id"]},
            references=[{"kind": "character", "op": "replace", "ids": [character["id"]]}],
        )
        assert resp.is_success, resp.text
    overridden = await planning_batch(client, base, source, [{"kind": "scene", "id": sid}], pov={"op": "clear"})
    assert overridden.is_success, overridden.text
    plan_before = (await client.get(f"{source}/{sid}")).json()["planning"]
    doc = save_payload((await client.get(f"{source}/{sid}/document")).json())
    add_section(doc, enabled=True)
    doc["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 0, "end": 1}]
    doc["add_marker_keys"].append(MARKER)
    doc["document"]["markers"].append({
        "key": MARKER, "start": 1, "end": 2, "section_key": None,
        "color": "yellow", "status": "active",
    })
    saved = await client.put(f"{source}/{sid}/document", json=doc)
    assert saved.is_success, saved.text
    world = (await client.post(f"{base}/world", json={"title": "山门", "content": "原状"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    timeline_url = f"{base}/setting-timelines/{timeline['id']}"
    changed = await _scene_change(client, timeline_url, timeline["revision"], sid, value="已变化")
    assert changed.status_code == 201, changed.text
    revision_before = (await scene_page(client, source))["planning_revision"]
    subplot_ids = []
    for name in ("寻亲", "破局"):
        subplot = await client.post(f"{base}/planning/subplots", json={
            "expected_revision": revision_before, "name": name,
        })
        assert subplot.status_code == 201, subplot.text
        subplot_ids.append(subplot.json()["item"]["id"])
        revision_before = subplot.json()["revision"]
    crossing = await client.post(f"{base}/planning/crossings", json={
        "expected_revision": revision_before, "source": {"kind": "scene", "id": sid},
        "title": "场景交汇", "description": "共同推进", "subplot_ids": subplot_ids,
    })
    assert crossing.status_code == 201, crossing.text

    launch, body = await preview(client, source, sid, target_chapter, right["id"])
    assert launch["source_position"] == 2 and launch["target_position"] == 1
    assert launch["planning_changes"] == [
        {"field": "character", "before": ["源章选角"], "after": ["目标章选角"]},
    ]
    assert launch["timeline_events"][0]["id"] == changed.json()["item"]["id"]
    assert launch["timeline_events"][0]["boundary"] == "end"
    assert launch["blockers"] == []
    assert launch["moved_start"] == "\n\n" + moved_text
    response = await client.post(f"{source}/{sid}/move", json=body)
    assert response.is_success, response.text
    revision = max(launch["source"]["content_revision"], launch["target"]["content_revision"]) + 1
    assert response.json() == {
        "scene_id": sid, "source_chapter_id": origin["id"], "target_chapter_id": target_chapter["id"],
        "source_content_revision": revision, "target_content_revision": revision,
        "planning_revision": launch["planning_revision"] + 1,
    }

    assert (await client.get(f"{source}/{sid}")).status_code == 404
    target_view = (await client.get(f"{target}/{sid}")).json()
    assert target_view["planning"]["unit_id"] == plan_before["unit_id"]
    assert target_view["planning"]["raw"] == plan_before["raw"]
    assert target_view["planning"]["effective"]["pov_character_id"] is None
    assert target_view["planning"]["effective"]["references"] == [
        {"kind": "character", "id": target_character["id"]},
    ]
    arrived_doc = (await client.get(f"{target}/{sid}/document")).json()
    assert arrived_doc["document"] == saved.json()["document"]
    marked = await client.get(f"{base}/markers")
    assert marked.is_success, marked.text
    marker, = [item for item in marked.json()["items"] if item["key"] == MARKER]
    assert (marker["chapter_id"], marker["scene_id"]) == (target_chapter["id"], sid)
    crossings = await client.get(f"{base}/planning/crossings", params={"source_kind": "scene", "source_id": sid})
    assert crossings.is_success, crossings.text
    saved_crossing, = crossings.json()["items"]
    assert saved_crossing["id"] == crossing.json()["item"]["id"]
    matrix = await client.get(f"{base}/planning/matrix", params={"granularity": "scene"})
    assert matrix.is_success, matrix.text
    moved_row, = [row for row in matrix.json()["rows"] if row["source"]["id"] == sid]
    assert moved_row["chapter_id"] == target_chapter["id"]
    assert [item["id"] for item in (await scene_page(client, target))["items"]] == [sid, right["id"]]
    assert (await client.get(target.rsplit("/", 1)[0])).json()["content"] == "\n\n第二段目的稿\u3000  "
    assert (await client.get(source.rsplit("/", 1)[0])).json()["content"] == "原景👩‍🚀\r\n"

    original_setting = (await client.get(f"{timeline_url}/effective", params={"scene_id": scenes[0]["id"]})).json()
    followed_setting = (await client.get(f"{timeline_url}/effective", params={"scene_id": right["id"]})).json()
    assert original_setting["fields"]["content"] == "原状"
    assert followed_setting["fields"]["content"] == "已变化"
    retune = await planning_batch(
        client, base, target, [{"kind": "chapter", "id": target_chapter["id"]}],
        references=[{"kind": "character", "op": "replace", "ids": [source_character["id"]]}],
    )
    assert retune.is_success, retune.text
    retuned = (await client.get(f"{target}/{sid}")).json()["planning"]
    assert retuned["effective"]["references"] == [{"kind": "character", "id": source_character["id"]}]
    assert retuned["effective"]["pov_character_id"] is None
    async with app.state.sessionmaker() as db:
        assert (await db.get(Scene, sid)).chapter_id == target_chapter["id"]
        advanced = (await db.execute(select(Chapter.content, Chapter.word_count).where(
            Chapter.id == target_chapter["id"],
        ))).one()
        assert advanced.word_count == len("第二段目的稿")


async def test_move_rechecks_target_and_anchor_revision_before_apply(client, app):
    _, base, origin, source, scenes = await scene_setup(client)
    sid = scenes[0]["id"]
    target_chapter = await create_chapter(client, base, 2, "二章原")
    target = scenes_path(base, target_chapter)
    target_sid = (await scene_page(client, target))["items"][0]["id"]
    _, body = await preview(client, source, sid, target_chapter, target_sid)
    edited = await client.patch(f"{target}/{target_sid}", json={"expected_content_revision": 0, "content": "二章新"})
    assert edited.is_success, edited.text
    stale = await client.post(f"{source}/{sid}/move", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "scene_move_preview_changed", stale.text

    world = (await client.post(f"{base}/world", json={"title": "山门"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    timeline_url = f"{base}/setting-timelines/{timeline['id']}"
    _, before_event = await preview(client, source, sid, target_chapter, target_sid)
    event = await _scene_change(client, timeline_url, timeline["revision"], sid, value="已变化")
    assert event.status_code == 201, event.text
    stale_event = await client.post(f"{source}/{sid}/move", json=before_event)
    assert stale_event.status_code == 409, stale_event.text
    assert stale_event.json()["code"] == "scene_move_preview_changed"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Scene, sid)).chapter_id == origin["id"]
        assert (await db.get(Chapter, target_chapter["id"])).content == "二章新"


async def test_move_preview_protects_rich_anchor_at_new_crlf_boundary(client):
    _, base, _, source, scenes = await scene_setup(client, content="\n原场景", count=1)
    sid = scenes[0]["id"]
    target = await create_chapter(client, base, 2, "目标场景\r")
    endpoint = f"{source}/{sid}/document"
    payload = save_payload((await client.get(endpoint)).json())
    add_section(payload)
    payload["document"]["sections"][0]["anchor"] = 0
    saved = await client.put(endpoint, json=payload)
    assert saved.is_success, saved.text
    launch, body = await preview(client, source, sid, target)
    assert launch["blockers"][0]["kind"] == "rich_document"
    failed = await client.post(f"{source}/{sid}/move", json=body)
    assert failed.status_code == 409, failed.text
    assert (await client.get(endpoint)).json()["document"] == saved.json()["document"]


async def test_move_rolls_back_both_chapters_when_story_order_is_invalid(client, app):
    project, base, origin, source, scenes = await scene_setup(client)
    sid = scenes[1]["id"]
    target = await create_chapter(client, base, 2, "另一职业开始")
    target_sid = (await scene_page(client, scenes_path(base, target)))["items"][0]["id"]
    character = (await client.post(f"{base}/characters", json={"name": "主角"})).json()
    careers = [(await client.post(f"{base}/careers", json={
        "name": name, "kind": "main", "stages": [{"name": "初阶"}],
    })).json() for name in ("旧职业", "新职业")]
    ids = []
    async with app.state.sessionmaker() as db:
        for career in careers:
            timeline = SettingTimeline(project_id=project["id"], subject_kind="career", character_id=character["id"],
                                       career_id=career["id"], baseline={"exists": False})
            db.add(timeline)
            await db.flush()
            ids.append(timeline.id)
        await db.commit()
    first, second = [f"{base}/setting-timelines/{identity}" for identity in ids]

    def presence(career):
        return {"exists": True, "state": {"role": "main", "stage_id": career["stages"][0]["id"], "note": ""}}

    enter_first = await _scene_change(client, first, 1, scenes[0]["id"], field="presence",
                                      value=presence(careers[0]), boundary="start")
    assert enter_first.status_code == 201, enter_first.text
    leave_first = await _scene_change(client, first, enter_first.json()["timeline_revision"], sid,
                                      field="presence", value={"exists": False})
    assert leave_first.status_code == 201, leave_first.text
    enter_second = await _scene_change(client, second, 1, target_sid, field="presence",
                                       value=presence(careers[1]), boundary="start")
    assert enter_second.status_code == 201, enter_second.text
    launch, body = await preview(client, source, sid, target)
    chapter_ids = [origin["id"], target["id"]]
    versions = select(ChapterVersion.id).where(ChapterVersion.chapter_id.in_(chapter_ids)).order_by(ChapterVersion.id)
    async with app.state.sessionmaker() as db:
        before_versions = list(await db.scalars(versions))
    response = await client.post(f"{source}/{sid}/move", json=body)
    assert response.status_code == 409, response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(Scene, sid)).chapter_id == origin["id"]
        assert (await db.get(Chapter, origin["id"])).content_revision == launch["source"]["content_revision"]
        assert (await db.get(Chapter, target["id"])).content_revision == launch["target"]["content_revision"]
        assert list(await db.scalars(versions)) == before_versions


@pytest.mark.parametrize("foreign", ["project", "insertion"])
async def test_move_rejects_scope_and_wrong_chapter_insertion(client, make_client, foreign):
    _, base, _, source, scenes = await scene_setup(client)
    other = make_client()
    await register(other, "其他作者")
    foreign_book = await create_project(other)
    target_base = f"/api/v1/projects/{foreign_book['id']}" if foreign == "project" else base
    chapter = await create_chapter(other if foreign == "project" else client, target_base, 2, "目标")
    response = await client.post(f"{source}/{scenes[0]['id']}/move-preview", json={
        "target_chapter_id": chapter["id"],
        "insert_before_scene_id": scenes[1]["id"] if foreign == "insertion" else None,
    })
    assert response.status_code == (404 if foreign == "project" else 422), response.text


@pytest.mark.parametrize("batch", [False, True])
async def test_move_preview_and_submit_guard_busy_target(client, app, batch):
    project, base, _, source, scenes = await scene_setup(client)
    target = await create_chapter(client, base, 2, "正在处理的章节")
    sid = scenes[0]["id"]
    _, body = await preview(client, source, sid, target)
    async with app.state.sessionmaker() as db:
        if batch:
            actor = (await client.get("/api/v1/auth/me")).json()
            db.add(Task(
                user_id=actor["id"], project_id=project["id"], kind=BATCH_KIND, status="running",
                title="占用目标章", input={"chapter_ids": [target["id"]]}, step_cursor=0,
            ))
        else:
            (await db.get(Chapter, target["id"])).status = "generating"
        await db.commit()
    now, _ = await preview(client, source, sid, target)
    assert len(now["blockers"]) == 1 and now["blockers"][0]["id"] == target["id"]
    blocked = await client.post(f"{source}/{sid}/move", json=body)
    assert blocked.status_code == 409 and blocked.json()["code"] == "scene_move_blocked", blocked.text
    assert (await client.get(f"{source}/{sid}")).status_code == 200


async def test_move_confirmation_is_bound_to_actor_and_current_grant(client, make_client):
    _, base, _, source, scenes = await scene_setup(client)
    target = await create_chapter(client, base, 2, "目标")
    guest = make_client()
    user = await register(guest, "moving-collaborator")
    invited = await client.post(f"{base}/collaborators", json={"username": user["username"], "role": "editor"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    invitation_path = f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept"
    accepted = await guest.post(invitation_path, json={"expected_revision": invitation["revision"]})
    assert accepted.status_code == 200, accepted.text
    sid = scenes[0]["id"]
    _, body = await preview(guest, source, sid, target)
    mismatch = await client.post(f"{source}/{sid}/move", json=body)
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "scene_move_preview_changed"

    member = f"{base}/collaborators/{user['id']}"
    downgraded = await client.patch(member, json={"expected_revision": accepted.json()["revision"], "role": "viewer"})
    assert downgraded.status_code == 200, downgraded.text
    readonly = await guest.post(f"{source}/{sid}/move", json=body)
    assert readonly.status_code == 403 and readonly.json()["code"] == "project_readonly"
    restored = await client.patch(member, json={"expected_revision": downgraded.json()["revision"], "role": "editor"})
    assert restored.status_code == 200, restored.text
    stale = await guest.post(f"{source}/{sid}/move", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "scene_move_preview_changed"
    _, fresh = await preview(guest, source, sid, target)
    applied = await guest.post(f"{source}/{sid}/move", json=fresh)
    assert applied.status_code == 200, applied.text
