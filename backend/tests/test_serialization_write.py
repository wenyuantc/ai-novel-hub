"""通过章节与工作台 HTTP 入口验证作者的定稿意图、排期和实际登记。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update

from app.models import AdminAuditLog, Chapter, SerializationEdition, SerializationEntry
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scenes import setup_scene


async def setup_workbench(client, content="晨光穿过窗棂。"):
    project, chapter, url, scene = await setup_scene(client, content)
    workbench = f"/api/v1/projects/{project['id']}/serialization"
    return workbench, f"{workbench}/chapters/{chapter['id']}", url, scene


async def preview(client, path):
    response = await client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def confirmation(detail):
    return {
        "expected_revision": detail["chapter"]["revision"],
        "expected_scene_id": detail["scene_id"],
        "expected_content_revision": detail["draft"]["content_revision"],
        "expected_fingerprint": detail["draft"]["fingerprint"],
    }


def registration(edition_id, revision):
    return {
        "edition_id": edition_id, "expected_revision": revision, "channel": "起点",
        "published_at": "2026-01-18T12:30:00+08:00", "request_id": str(uuid4()),
    }


async def test_goal_and_schedule_use_independent_revisions_and_sparse_updates(app, client):
    base, chapter, _url, scene = await setup_workbench(client)
    response = await client.put(f"{base}/goal", json={"expected_revision": 0, "daily_goal": 2})
    assert response.json() == {"daily_goal": 2, "revision": 1}
    stale = await client.put(f"{base}/goal", json={"expected_revision": 0, "daily_goal": 4})
    assert stale.status_code == 409
    cleared = await client.put(f"{base}/goal", json={"expected_revision": 1, "daily_goal": None})
    assert cleared.json() == {"daily_goal": None, "revision": 2}
    anchor = {"expected_scene_id": scene["id"]}
    no_change = await client.patch(chapter, json=anchor | {"expected_revision": 0, "planned_date": None})
    assert no_change.json() == {"revision": 0}
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(SerializationEntry.id)) is None
    changed = await client.patch(chapter, json=anchor | {"expected_revision": 0, "planned_date": "2026-05-21"})
    assert changed.json() == {"revision": 1}
    changed = await client.patch(chapter, json=anchor | {"expected_revision": 1, "stage": "proofreading"})
    assert changed.json() == {"revision": 2}
    item = (await preview(client, chapter))["chapter"]
    assert (item["planned_date"], item["stage"]) == ("2026-05-21", "proofreading")
    changed = await client.patch(chapter, json=anchor | {"expected_revision": 2, "planned_date": None})
    assert changed.json() == {"revision": 3}
    item = (await preview(client, chapter))["chapter"]
    assert (item["planned_date"], item["stage"]) == (None, "proofreading")


async def test_frozen_reader_projection_skips_private_material_and_detects_format_changes(app, client):
    text = "甲\r\n乙🙂"
    _base, chapter, url, scene = await setup_workbench(client, text)
    document = f"{url}/scenes/{scene['id']}/document"
    first = await preview(client, chapter)
    saved = await client.post(f"{chapter}/editions", json=confirmation(first))
    assert saved.status_code == 200, saved.text
    draft = save_payload((await client.get(document)).json())
    add_section(draft)
    assert (await client.put(document, json=draft)).is_success
    private_edit = await preview(client, chapter)
    assert private_edit["draft_changed"] is False
    assert private_edit["draft"]["content_revision"] > first["draft"]["content_revision"]
    assert private_edit["draft"]["fingerprint"] == first["draft"]["fingerprint"]
    stale = confirmation(private_edit) | {"expected_content_revision": first["draft"]["content_revision"]}
    assert (await client.post(f"{chapter}/editions", json=stale)).status_code == 409
    draft = save_payload((await client.get(document)).json())
    draft["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 0, "end": 1}]
    assert (await client.put(document, json=draft)).is_success
    reformatted = await preview(client, chapter)
    assert reformatted["draft_changed"] is True
    assert reformatted["draft"]["content"] == reformatted["edition"]["content"] == text
    assert reformatted["draft"]["blocks"] and reformatted["edition"]["blocks"] is None
    confirmed = await client.post(f"{chapter}/editions", json=confirmation(reformatted))
    assert confirmed.json()["seq"] == 2
    async with app.state.sessionmaker() as db:
        editions = list(await db.scalars(select(SerializationEdition).order_by(SerializationEdition.seq)))
        assert [edition.content for edition in editions] == [text, text]
        assert editions[0].blocks is None and editions[1].blocks == reformatted["draft"]["blocks"]
        assert "作者🙂" not in str(editions[1].blocks)


async def test_registration_uses_frozen_text_even_during_generation(app, client):
    _base, chapter, url, _scene = await setup_workbench(client)
    first = await preview(client, chapter)
    edition = (await client.post(f"{chapter}/editions", json=confirmation(first))).json()
    write = await client.patch(url, json={"content": "另写一稿", "expected_content_revision": 0})
    assert write.is_success, write.text
    current = await preview(client, chapter)
    assert current["draft_changed"] and current["edition"]["content"] == first["draft"]["content"]
    async with app.state.sessionmaker() as db:
        chapter_id = first["chapter"]["chapter_id"]
        await db.execute(update(Chapter).where(Chapter.id == chapter_id).values(status="generating"))
        await db.commit()
    assert (await client.post(f"{chapter}/editions", json=confirmation(current))).status_code == 409
    published = await client.post(f"{chapter}/publications", json=registration(edition["id"], 1))
    assert published.is_success, published.text
    future = registration(edition["id"], 1) | {"published_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    assert (await client.post(f"{chapter}/publications", json=future)).status_code == 400


async def test_retired_unpublished_edition_cannot_be_registered_but_published_edition_can(app, client):
    base, chapter, url, scene = await setup_workbench(client)
    first = (await client.post(f"{chapter}/editions", json=confirmation(await preview(client, chapter)))).json()
    body = {"expected_revision": 1, "expected_scene_id": scene["id"], "stage": "revision"}
    assert (await client.patch(chapter, json=body)).json() == {"revision": 2}
    retry = await client.post(f"{chapter}/publications", json=registration(first["id"], 2))
    assert retry.status_code == 409
    assert (await client.get(f"{chapter}/editions/{first['id']}/export")).status_code == 404
    second = (await client.post(f"{chapter}/editions", json=confirmation(await preview(client, chapter)))).json()
    assert (await client.post(f"{chapter}/publications", json=registration(second["id"], 3))).is_success
    assert (await client.patch(url, json={"content": "新版", "expected_content_revision": 0})).is_success
    third = (await client.post(f"{chapter}/editions", json=confirmation(await preview(client, chapter)))).json()
    available = await preview(client, chapter)
    assert [row["id"] for row in available["editions"]] == [third["id"], second["id"]]
    late = registration(second["id"], 4) | {"channel": "番茄"}
    published = await client.post(f"{chapter}/publications", json=late)
    assert published.is_success, published.text
    history = (await client.get(f"{base}/publications/{published.json()['id']}")).json()
    assert history["edition"]["id"] == second["id"] and history["draft"]["content"] == "新版"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(SerializationEdition.id).where(SerializationEdition.id == first["id"]))


async def test_empty_draft_and_admin_audit(app, client, system_admin):
    _base, chapter, url, _scene = await setup_workbench(client, " \n ")
    detail = await preview(system_admin, chapter)
    rejected = await system_admin.post(f"{chapter}/editions", json=confirmation(detail))
    assert rejected.status_code == 400
    assert (await client.patch(url, json={"content": "审计中不应出现此句", "expected_content_revision": 0})).is_success
    detail = await preview(system_admin, chapter)
    edition = await system_admin.post(f"{chapter}/editions", json=confirmation(detail))
    assert edition.is_success, edition.text
    saved = await system_admin.post(f"{chapter}/publications", json=registration(edition.json()["id"], 1))
    assert saved.is_success, saved.text
    async with app.state.sessionmaker() as db:
        logs = list(await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.action == "chapter.update", AdminAuditLog.chapter_id == detail["chapter"]["chapter_id"],
        ).order_by(AdminAuditLog.id)))
        assert [log.result for log in logs] == ["failure", "success", "success"]
        assert "定稿" in logs[1].target_label and "发布" in logs[2].target_label
        assert all("审计中不应出现此句" not in str(log.summary) for log in logs)


async def test_recreated_identical_chapter_rejects_original_schedule_and_finalize_preview(app, client):
    base, path, chapter_url, _scene = await setup_workbench(client)
    original = await preview(client, path)
    removed = await client.delete(chapter_url)
    assert removed.status_code == 204
    new_chapter = await client.post(f"{base.removesuffix('/serialization')}/chapters", json={
        "chapter_no": original["draft"]["chapter_no"], "title": original["draft"]["title"],
        "content": original["draft"]["content"],
    })
    assert new_chapter.json()["id"] == original["chapter"]["chapter_id"]
    replacement = await preview(client, path)
    assert replacement["draft"] == original["draft"] and replacement["scene_id"] != original["scene_id"]
    stale_plan = {"expected_revision": 0, "expected_scene_id": original["scene_id"], "planned_date": "2026-05-22"}
    scheduled = await client.patch(path, json=stale_plan)
    finalized = await client.post(f"{path}/editions", json=confirmation(original))
    for result in (scheduled, finalized):
        assert result.status_code == 409 and result.json()["code"] == "stale_chapter_identity"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(SerializationEntry.id)) is None
