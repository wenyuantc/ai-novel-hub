import base64
import io
import json
from copy import deepcopy

import pytest
from PIL import Image
from sqlalchemy import delete, event, select

from app.core.errors import BadRequestError, ConflictError
from app.models import Character
from app.services import personal_projection
from app.services.agent import effects
from app.services.agent.tools.base import preview_diff
from app.services.card_service import parse_card
from app.services.project_transfer import importer, thumbnails
from app.services.setting_image_codec import normalize_image
from app.services.setting_image_service import _current_book_resource, set_book_image_without_commit
from tests.helpers import create_project, register


def png(color="red"):
    out = io.BytesIO()
    Image.new("RGB", (35, 20), color).save(out, format="PNG")
    return out.getvalue()


async def make_entry(client, base, kind, path, body):
    response = await client.post(f"{base}/{path}", json=body)
    assert response.status_code == 201, response.text
    item = response.json()
    route = f"{base}/settings/{kind}/{item['id']}/thumbnail"
    uploaded = await client.put(route, files={"file": ("x.png", png(), "image/png")}, data={"expected_version": ""})
    assert uploaded.status_code == 200, uploaded.text
    return item, route, uploaded.json()


async def test_backup_and_card_images_roundtrip_with_explicit_selection(client):
    await register(client)
    source = await create_project(client, title="来源")
    target = await create_project(client, title="目标")
    base = f"/api/v1/projects/{source['id']}"
    dest = f"/api/v1/projects/{target['id']}"
    char, char_route, _ = await make_entry(client, base, "character", "characters", {"name": "甲"})
    org, _, _ = await make_entry(client, base, "organization", "organizations", {"name": "宗门"})
    career, _, _ = await make_entry(client, base, "career", "careers", {
        "name": "剑修", "stages": [{"name": "一阶"}],
    })
    world, _, _ = await make_entry(client, base, "world", "world", {"title": "城", "category": "place"})
    assigned = await client.post(f"{base}/characters/{char['id']}/careers", json={"career_id": career["id"]})
    assert assigned.status_code in (200, 201), assigned.text
    backup = await client.get(f"{base}/backup")
    assert backup.status_code == 200, backup.text
    data = backup.json()
    assert data["version"] == 16
    assert {item["ref"]["kind"] for item in data["thumbnails"]} == {"character", "organization", "career", "world"}
    assert all(set(item) == {"ref", "mime_type", "data"} for item in data["thumbnails"])
    assert all("id" not in item for item in data["thumbnails"])
    preview = await client.post("/api/v1/projects/import/preview", files={
        "file": ("b.json", json.dumps(data, ensure_ascii=False).encode(), "application/json"),
    })
    assert preview.status_code == 200, preview.text
    restored = await client.post("/api/v1/projects/import", json={"token": preview.json()["token"]})
    assert restored.status_code == 201, restored.text
    restored_base = f"/api/v1/projects/{restored.json()['project']['id']}"
    for kind, path, name in (("character", "characters", "甲"), ("organization", "organizations", "宗门"),
                             ("career", "careers", "剑修"), ("world", "world", "城")):
        rows = (await client.get(f"{restored_base}/{path}")).json()
        entity = next(row for row in rows if row.get("name", row.get("title")) == name)
        image = await client.get(f"{restored_base}/settings/{kind}/{entity['id']}/thumbnail")
        assert image.status_code == 200 and image.content == (await client.get(char_route)).content
        assert entity["thumbnail"]["version"] not in str(data["thumbnails"])
    card_response = await client.post(f"{base}/cards/export", json={
        "character_ids": [char["id"]], "organization_ids": [org["id"]],
    })
    assert card_response.status_code == 200, card_response.text
    card = card_response.json()
    assert card["version"] == 7
    assert all(item.get("thumbnail") for key in ("characters", "organizations", "careers") for item in card[key])
    assert "world" not in card
    pv = await client.post(f"{dest}/cards/import/preview", json={"data": card})
    assert pv.status_code == 200, pv.text
    info = pv.json()
    assert {row["kind"] for row in info["thumbnails"]} == {"character", "organization", "career"}
    selected = [row["ref"] for row in info["thumbnails"] if row["kind"] != "organization"]
    request = {"data": card, "thumbnail_refs": selected,
               **{f"expected_{key}": info[key] for key in (
                   "fields_revision", "categories_revision", "ai_visibility_revision")}}
    imported = await client.post(f"{dest}/cards/import", json=request)
    assert imported.status_code == 200, imported.text
    result = imported.json()
    for kind, group in (("character", "characters"), ("career", "careers")):
        route = f"{dest}/settings/{kind}/{result[group][0]['id']}/thumbnail"
        assert (await client.get(route)).status_code == 200
    route = f"{dest}/settings/organization/{result['organizations'][0]['id']}/thumbnail"
    assert (await client.get(route)).status_code == 404
    # 第二次全部同名跳过，已存在的图不能被卡片覆盖，也不能将未选择图写入组织。
    existing = await client.get(f"{dest}/settings/character/{result['characters'][0]['id']}/thumbnail")
    pv2 = (await client.post(f"{dest}/cards/import/preview", json={"data": card})).json()
    again = await client.post(f"{dest}/cards/import", json={"data": card,
        "thumbnail_refs": [row["ref"] for row in pv2["thumbnails"]],
        **{f"expected_{key}": pv2[key] for key in (
            "fields_revision", "categories_revision", "ai_visibility_revision")}})
    assert again.status_code == 200, again.text
    assert (await client.get(route)).status_code == 404
    again_image = await client.get(f"{dest}/settings/character/{result['characters'][0]['id']}/thumbnail")
    assert again_image.headers["etag"] == existing.headers["etag"]


def test_invalid_and_legacy_card_images_are_rejected():
    image = normalize_image(png())
    thumbnail = {"mime_type": "image/webp", "data": base64.b64encode(image.data).decode()}
    # 旧版不能通过增加未来字段绕过显式选择。
    with pytest.raises(BadRequestError):
        parse_card({"format": "ainovel.cards", "version": 1, "characters": [{"name": "甲", "thumbnail": thumbnail}]})
    with pytest.raises(BadRequestError):
        parse_card({"format": "ainovel.cards", "version": 1, "characters": [{"name": "甲",
            "thumbnail": {"mime_type": "image/webp", "data": "bad"}}]})


async def test_effects_restore_images_and_reject_later_images(client, app):
    user = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character, route, meta = await make_entry(client, base, "character", "characters", {"name": "甲"})
    sql = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        sql.append(statement.lower())
    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with app.state.sessionmaker() as db:
            before = await effects.snapshot(db, project["id"])
            assert not any("setting_image_resources.data" in statement for statement in sql)
            await db.execute(delete(Character).where(Character.id == character["id"]))
            after = await effects.snapshot(db, project["id"])
            changes = effects.diff(before, after)
            assert any(row["table"] == "setting_thumbnail_bindings" for row in changes)
            assert meta["version"] not in json.dumps(preview_diff(changes))
            projection = personal_projection._project(changes, {}, {}, user["id"], project["id"], True)
            assert meta["version"] not in json.dumps(projection)
            await db.commit()
            await effects.undo(db, project["id"], changes)
            await db.commit()
            restored = await _current_book_resource(db, project["id"], "character", character["id"])
            assert restored.id == meta["version"]
        assert (await client.get(route)).status_code == 200
        async with app.state.sessionmaker() as db:
            initial = await effects.snapshot(db, project["id"])
            new = Character(project_id=project["id"], name="乙")
            db.add(new)
            await db.flush()
            new_id = new.id
            created = await effects.snapshot(db, project["id"])
            delta = effects.diff(initial, created)
            await set_book_image_without_commit(db, project["id"], "character", new_id, normalize_image(png("blue")))
            await db.commit()
            with pytest.raises(ConflictError):
                await effects.undo(db, project["id"], delta)
            await db.rollback()
            assert await db.scalar(select(Character.id).where(Character.id == new_id)) == new_id
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)


async def test_backup_rejects_invalid_images_and_legacy_injection(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await make_entry(client, base, "character", "characters", {"name": "甲"})
    data = (await client.get(f"{base}/backup")).json()
    for mutate in (lambda d: d["thumbnails"].append(deepcopy(d["thumbnails"][0])),
                   lambda d: d["thumbnails"][0].update(data="bad"),
                   lambda d: d.update(version=10)):
        value = deepcopy(data)
        mutate(value)
        with pytest.raises(BadRequestError):
            importer.parse_backup(json.dumps(value).encode())


async def test_backup_spool_rejects_size_and_image_version_changes(client, app, monkeypatch):
    from app.api.v1 import transfer
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    _, route, meta = await make_entry(client, base, "character", "characters", {"name": "甲"})
    opened = []
    original = transfer.SpooledTemporaryFile
    def spool(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(transfer, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(thumbnails, "MAX_BACKUP_BYTES", 100)
    response = await client.get(f"{base}/backup")
    assert response.status_code == 400 and "100MB" in response.text
    assert opened[-1].closed
    monkeypatch.setattr(thumbnails, "MAX_BACKUP_BYTES", 100 * 1024 * 1024)
    original_iter = thumbnails.iter_serialized
    async def change_during_export(db, snapshot, **kwargs):
        async for item in original_iter(db, snapshot, **kwargs):
            yield item
            changed = await client.put(route, files={"file": ("b.png", png("blue"), "image/png")},
                                       data={"expected_version": meta["version"]})
            assert changed.status_code == 200, changed.text
    monkeypatch.setattr(thumbnails, "iter_serialized", change_during_export)
    response = await client.get(f"{base}/backup")
    assert response.status_code == 409 and response.json()["code"] == "thumbnail_changed"
    assert opened[-1].closed
    monkeypatch.setattr(thumbnails, "iter_serialized", original_iter)
    response = await client.get(f"{base}/backup")
    assert response.status_code == 200, response.text
    assert opened[-1].closed


@pytest.mark.parametrize("change", ["actor", "project"])
async def test_backup_aborts_if_actor_or_project_changes_during_staging(client, app, monkeypatch, change):
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.api.v1 import transfer
    from app.models import Project, User

    user = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await make_entry(client, base, "character", "characters", {"name": "甲"})
    opened = []
    original_spool = transfer.SpooledTemporaryFile
    def spool(*args, **kwargs):
        result = original_spool(*args, **kwargs)
        opened.append(result)
        return result
    monkeypatch.setattr(transfer, "SpooledTemporaryFile", spool)
    original_iter = thumbnails.iter_serialized
    async def modify_after_image(db, snapshot, **kwargs):
        async for image in original_iter(db, snapshot, **kwargs):
            yield image
            async with app.state.sessionmaker() as other:
                if change == "actor":
                    await other.execute(update(User).where(User.id == user["id"]).values(is_active=False))
                else:
                    await other.execute(update(Project).where(Project.id == project["id"])
                                        .values(deleted_at=datetime.now(UTC)))
                await other.commit()
    monkeypatch.setattr(thumbnails, "iter_serialized", modify_after_image)
    response = await client.get(f"{base}/backup")
    assert response.status_code == (401 if change == "actor" else 404), response.text
    assert opened[-1].closed


async def test_backup_send_rechecks_versions_and_cleans_up_on_disconnect(client, app):
    from tempfile import SpooledTemporaryFile

    from app.api.v1.transfer import _validated_backup_stream
    from app.schemas.transfer import BackupOptions
    from app.services.project_transfer import exporter

    user = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    _, route, meta = await make_entry(client, base, "character", "characters", {"name": "甲"})
    async with app.state.sessionmaker() as db:
        plan = await exporter.prepare_export(db, user["id"], project["id"], BackupOptions())
    backing = SpooledTemporaryFile(max_size=8, mode="w+b")
    backing.write(b'{"safe":true}')
    backing.seek(0)
    stream = _validated_backup_stream(backing, 13, app.state.sessionmaker, plan)
    part = await anext(stream)
    assert not part.endswith(b"e}")
    updated = await client.put(route, files={"file": ("b.png", png("blue"), "image/png")},
                               data={"expected_version": meta["version"]})
    assert updated.status_code == 200, updated.text
    with pytest.raises(ConflictError):
        await anext(stream)
    assert backing.closed
    backing = SpooledTemporaryFile(max_size=8, mode="w+b")
    backing.write(b'{"safe":true}')
    backing.seek(0)
    async with app.state.sessionmaker() as db:
        plan = await exporter.prepare_export(db, user["id"], project["id"], BackupOptions())
    stream = _validated_backup_stream(backing, 13, app.state.sessionmaker, plan)
    await anext(stream)
    await stream.aclose()
    assert backing.closed


async def test_shared_images_export_as_independent_book_resources(client, app):
    await register(client)
    source = await create_project(client, title="共享来源")
    target = await create_project(client, title="共享采用")
    base = f"/api/v1/projects/{source['id']}"
    char, _, _ = await make_entry(client, base, "character", "characters", {"name": "共享角色"})
    series = (await client.post("/api/v1/series", json={"name": "世界系列"})).json()
    path = f"/api/v1/series/{series['id']}"
    for book in (source, target):
        response = await client.post(f"{path}/books", json={"project_id": book["id"],
                                                              "expected_revision": series["revision"]})
        assert response.status_code == 200, response.text
        series = response.json()
    impact = await client.get(f"{path}/impact", params={"action": "promote", "project_id": source["id"],
                                                        "kind": "character", "entry_id": char["id"]})
    promoted = await client.post(f"{path}/entries/promote", json={
        "project_id": source["id"], "ref": {"kind": "character", "id": char["id"]},
        "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"],
    })
    assert promoted.status_code == 201, promoted.text
    entry = promoted.json()["item"]
    used = await client.post(f"{path}/entries/{entry['id']}/uses", json={"project_id": target["id"],
        "expected_revision": promoted.json()["revision"]})
    assert used.status_code == 200, used.text
    projected_id = used.json()["ref"]["id"]
    target_base = f"/api/v1/projects/{target['id']}"
    original = await client.get(f"{target_base}/settings/character/{projected_id}/thumbnail")
    backup = await client.get(f"{target_base}/backup")
    assert backup.status_code == 200, backup.text
    data = backup.json()
    assert len(data["thumbnails"]) == 1
    assert data["thumbnails"][0]["ref"] == {"kind": "character", "id": projected_id}
    assert entry["thumbnail"]["version"] not in backup.text
    preview = await client.post("/api/v1/projects/import/preview", files={
        "file": ("b.json", json.dumps(data, ensure_ascii=False).encode(), "application/json"),
    })
    assert preview.status_code == 200, preview.text
    restored = await client.post("/api/v1/projects/import", json={"token": preview.json()["token"]})
    assert restored.status_code == 201, restored.text
    new_id = restored.json()["project"]["id"]
    rows = (await client.get(f"/api/v1/projects/{new_id}/characters")).json()
    new_char = next(item for item in rows if item["name"] == "共享角色")
    image = await client.get(f"/api/v1/projects/{new_id}/settings/character/{new_char['id']}/thumbnail")
    assert image.status_code == 200 and image.content == original.content
    async with app.state.sessionmaker() as db:
        resource = await _current_book_resource(db, new_id, "character", new_char["id"])
        assert resource.project_id == new_id and resource.series_id is None


async def test_thumbnail_undo_cannot_override_later_series_binding(client, app):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    char, _, _ = await make_entry(client, base, "character", "characters", {"name": "角色"})
    async with app.state.sessionmaker() as db:
        before = await effects.snapshot(db, project["id"])
        await set_book_image_without_commit(db, project["id"], "character", char["id"], None)
        await db.flush()
        changes = effects.diff(before, await effects.snapshot(db, project["id"]))
        await db.commit()
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    path = f"/api/v1/series/{series['id']}"
    added = await client.post(f"{path}/books", json={"project_id": project["id"],
                                                      "expected_revision": series["revision"]})
    assert added.status_code == 200, added.text
    impact = await client.get(f"{path}/impact", params={"action": "promote", "project_id": project["id"],
                                                        "kind": "character", "entry_id": char["id"]})
    promoted = await client.post(f"{path}/entries/promote", json={"project_id": project["id"],
        "ref": {"kind": "character", "id": char["id"]}, "expected_revision": added.json()["revision"],
        "source_stamp": impact.json()["source_stamp"]})
    assert promoted.status_code == 201, promoted.text
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await effects.undo(db, project["id"], changes)
        await db.rollback()
        assert await _current_book_resource(db, project["id"], "character", char["id"]) is None


async def test_old_text_undo_keeps_thumbnail_added_later(client, app):
    from sqlalchemy import update

    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/characters", json={"name": "甲", "profile": "旧描述"})
    assert response.status_code == 201, response.text
    ident = response.json()["id"]
    async with app.state.sessionmaker() as db:
        before = await effects.snapshot(db, project["id"])
        await db.execute(update(Character).where(Character.id == ident).values(profile="新描述"))
        after = await effects.snapshot(db, project["id"])
        old_effects = effects.diff(before, after)
        for change in old_effects:
            change["format_version"] = 6
        await db.commit()
        await set_book_image_without_commit(db, project["id"], "character", ident, normalize_image(png()))
        await db.commit()
        await effects.undo(db, project["id"], old_effects)
        await db.commit()
        assert await db.scalar(select(Character.profile).where(Character.id == ident)) == "旧描述"
        assert await _current_book_resource(db, project["id"], "character", ident) is not None
