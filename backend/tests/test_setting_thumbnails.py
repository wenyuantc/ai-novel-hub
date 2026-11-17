import io

import pytest
from PIL import Image

from app.models.setting_images import SettingThumbnailBinding
from app.services import setting_image_service
from tests.helpers import create_project, register


def image_data(color="red"):
    out = io.BytesIO()
    Image.new("RGB", (800, 400), color).save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize(("kind", "path", "body"), [
    ("character", "characters", {"name": "甲"}),
    ("world", "world", {"title": "地点", "category": "place"}),
    ("organization", "organizations", {"name": "组织"}),
    ("career", "careers", {"name": "职业", "stages": [{"name": "一阶"}]}),
])
async def test_thumbnail_http_lifecycle_and_entity_projection(client, make_client, kind, path, body):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post(f"{base}/{path}", json=body)
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]
    route = f"{base}/settings/{kind}/{entity_id}/thumbnail"
    assert (await client.get(route)).status_code == 404
    first = await client.put(route, files={"file": ("a.png", image_data(), "image/png")}, data={"expected_version": ""})
    assert first.status_code == 200, first.text
    meta = first.json()
    assert (meta["width"], meta["height"], meta["mime_type"]) == (512, 256, "image/webp")
    listing = await client.get(f"{base}/{path}")
    assert listing.status_code == 200, listing.text
    entry = next(item for item in listing.json() if item["id"] == entity_id)
    assert entry["thumbnail"] == meta
    detail = await client.get(f"{base}/{path}/{entity_id}")
    assert detail.json()["thumbnail"] == meta
    got = await client.get(route)
    assert got.status_code == 200 and got.headers["content-type"] == "image/webp"
    assert (await client.get(route, headers={"If-None-Match": got.headers["etag"]})).status_code == 304
    stale = await client.put(route, files={"file": ("b.png", image_data("blue"), "image/png")},
                             data={"expected_version": ""})
    assert stale.status_code == 409 and stale.json()["code"] == "thumbnail_changed"
    second = await client.put(route, files={"file": ("b.png", image_data("blue"), "image/png")},
                              data={"expected_version": meta["version"]})
    assert second.status_code == 200 and second.json()["version"] != meta["version"]
    assert (await client.delete(route, params={"expected_version": meta["version"]})).status_code == 409
    assert (await client.delete(route, params={"expected_version": second.json()["version"]})).status_code == 200
    assert (await client.get(route)).status_code == 404
    other = make_client()
    await register(other, username="other", password="secret123")
    assert (await other.get(route)).status_code == 404
    assert (await other.get(route, headers={"If-None-Match": got.headers["etag"]})).status_code == 404
    await other.aclose()


async def test_shared_image_promote_use_replace_and_move_are_scoped(client):
    await register(client)
    a = await create_project(client, title="甲")
    b = await create_project(client, title="乙")
    project = f"/api/v1/projects/{a['id']}"
    char = (await client.post(f"{project}/characters", json={"name": "甲角色"})).json()
    thumbnail = f"{project}/settings/character/{char['id']}/thumbnail"
    first = await client.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")},
                             data={"expected_version": ""})
    assert first.status_code == 200, first.text
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    path = f"/api/v1/series/{series['id']}"
    for book in (a, b):
        response = await client.post(f"{path}/books", json={"project_id": book["id"],
                                                              "expected_revision": series["revision"]})
        assert response.status_code == 200, response.text
        series = response.json()
    impact = await client.get(f"{path}/impact", params={"action": "promote", "project_id": a["id"],
                                                        "kind": "character", "entry_id": char["id"]})
    assert impact.status_code == 200, impact.text
    replacement = await client.put(thumbnail, files={"file": ("b.png", image_data("blue"), "image/png")},
                                   data={"expected_version": first.json()["version"]})
    payload = {"project_id": a["id"], "ref": {"kind": "character", "id": char["id"]},
               "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"]}
    assert (await client.post(f"{path}/entries/promote", json=payload)).json()["code"] == "series_source_changed"
    impact = await client.get(f"{path}/impact", params={"action": "promote", "project_id": a["id"],
                                                        "kind": "character", "entry_id": char["id"]})
    payload["source_stamp"] = impact.json()["source_stamp"]
    promoted = await client.post(f"{path}/entries/promote", json=payload)
    assert promoted.status_code == 201, promoted.text
    row = promoted.json()
    eid, revision = row["item"]["id"], row["revision"]
    shared = f"{path}/entries/{eid}/thumbnail"
    shared_img = await client.get(shared)
    assert shared_img.status_code == 200
    assert (await client.get(thumbnail)).content == shared_img.content
    assert row["item"]["thumbnail"]["version"] != replacement.json()["version"]
    assert (await client.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")},
                             data={"expected_version": row["item"]["thumbnail"]["version"]})).status_code == 409
    used = await client.post(f"{path}/entries/{eid}/uses", json={"project_id": b["id"],
                                                                   "expected_revision": revision})
    assert used.status_code == 200, used.text
    revision = used.json()["revision"]
    ref_b = used.json()["ref"]["id"]
    route_b = f"/api/v1/projects/{b['id']}/settings/character/{ref_b}/thumbnail"
    assert (await client.get(route_b)).content == shared_img.content
    shared_meta = used.json()["item"]["thumbnail"]
    changed = await client.put(shared, files={"file": ("c.png", image_data("green"), "image/png")}, data={
        "expected_revision": str(revision), "expected_version": shared_meta["version"],
    })
    assert changed.status_code == 200, changed.text
    assert (await client.get(thumbnail)).content == (await client.get(route_b)).content
    moved = await client.post(f"{path}/entries/{eid}/move-to-book", json={
        "project_id": a["id"], "expected_revision": changed.json()["revision"],
    })
    assert moved.status_code == 200, moved.text
    assert (await client.get(shared)).status_code == 404
    assert (await client.get(thumbnail)).status_code == 200
    assert (await client.get(route_b)).status_code == 404


async def test_project_viewer_cannot_trigger_local_or_series_image_decode(client, make_client, monkeypatch):
    await register(client, "thumbnail-owner")
    guest = make_client()
    await register(guest, "thumbnail-viewer")
    project = await create_project(client)
    project_url = f"/api/v1/projects/{project['id']}"
    char = (await client.post(f"{project_url}/characters", json={"name": "只读角色"})).json()
    invitation = await client.post(f"{project_url}/collaborators", json={
        "username": "thumbnail-viewer", "role": "viewer",
    })
    assert invitation.status_code == 201, invitation.text
    member = invitation.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{member['invitation_id']}/accept",
        json={"expected_revision": member["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert (await guest.get(f"{project_url}/characters/{char['id']}")).status_code == 200
    series = (await client.post("/api/v1/series", json={"name": "私有系列"})).json()
    path = f"/api/v1/series/{series['id']}"
    entry = await client.post(f"{path}/entries", json={
        "kind": "character", "data": {"name": "系列角色"}, "expected_revision": series["revision"],
    })
    assert entry.status_code == 201, entry.text

    async def never_decode(*args, **kwargs):
        raise AssertionError("未授权用户不得触发图片解码")

    monkeypatch.setattr(setting_image_service, "normalize_image_async", never_decode)
    data = {"expected_version": ""}
    files = {"file": ("a.png", image_data(), "image/png")}
    local = await guest.put(f"{project_url}/settings/character/{char['id']}/thumbnail", files=files, data=data)
    assert local.status_code in (403, 404), local.text
    shared = await guest.put(f"{path}/entries/{entry.json()['item']['id']}/thumbnail", files=files, data={
        **data, "expected_revision": str(entry.json()["revision"]),
    })
    assert shared.status_code == 404, shared.text
    await guest.aclose()


async def test_book_viewer_can_see_adopted_image_without_series_access_and_loses_it_after_revoke(client, make_client):
    await register(client, "thumbnail-shared-owner")
    guest = make_client()
    user = await register(guest, "thumbnail-shared-viewer")
    book = await create_project(client)
    base = f"/api/v1/projects/{book['id']}"
    char = (await client.post(f"{base}/characters", json={"name": "共享角色"})).json()
    thumbnail = f"{base}/settings/character/{char['id']}/thumbnail"
    uploaded = await client.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")},
                                data={"expected_version": ""})
    assert uploaded.status_code == 200, uploaded.text
    series = (await client.post("/api/v1/series", json={"name": "作者私有系列"})).json()
    path = f"/api/v1/series/{series['id']}"
    joined = await client.post(f"{path}/books", json={"project_id": book["id"], "expected_revision": 0})
    impact = await client.get(f"{path}/impact", params={
        "action": "promote", "project_id": book["id"], "kind": "character", "entry_id": char["id"],
    })
    promoted = await client.post(f"{path}/entries/promote", json={
        "project_id": book["id"], "ref": {"kind": "character", "id": char["id"]},
        "expected_revision": joined.json()["revision"], "source_stamp": impact.json()["source_stamp"],
    })
    assert promoted.status_code == 201, promoted.text
    invitation = await client.post(f"{base}/collaborators", json={
        "username": "thumbnail-shared-viewer", "role": "viewer",
    })
    member = invitation.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{member['invitation_id']}/accept",
        json={"expected_revision": member["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    response = await guest.get(thumbnail)
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert (await guest.get(f"{path}/entries/{promoted.json()['item']['id']}/thumbnail")).status_code == 404
    assert (await guest.get(path)).status_code == 404
    revoked = await client.delete(f"{base}/collaborators/{user['id']}", params={"expected_revision": 2})
    assert revoked.status_code == 204, revoked.text
    assert (await guest.get(thumbnail, headers={"If-None-Match": response.headers["etag"]})).status_code == 404
    await guest.aclose()


async def test_shared_setting_does_not_show_legacy_local_image_when_series_image_removed(app, client):
    await register(client, "thumbnail-legacy-owner")
    book = await create_project(client)
    base = f"/api/v1/projects/{book['id']}"
    char = (await client.post(f"{base}/characters", json={"name": "共享角色"})).json()
    thumbnail = f"{base}/settings/character/{char['id']}/thumbnail"
    local = await client.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")},
                             data={"expected_version": ""})
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    path = f"/api/v1/series/{series['id']}"
    joined = await client.post(f"{path}/books", json={"project_id": book["id"], "expected_revision": 0})
    impact = await client.get(f"{path}/impact", params={
        "action": "promote", "project_id": book["id"], "kind": "character", "entry_id": char["id"],
    })
    promoted = await client.post(f"{path}/entries/promote", json={
        "project_id": book["id"], "ref": {"kind": "character", "id": char["id"]},
        "expected_revision": joined.json()["revision"], "source_stamp": impact.json()["source_stamp"],
    })
    assert promoted.status_code == 201, promoted.text
    item = promoted.json()["item"]
    # 模拟旧版本异常恢复出的本地绑定；当前采用关系仍必须优先。
    async with app.state.sessionmaker() as db:
        db.add(SettingThumbnailBinding(project_id=book["id"], character_id=char["id"],
                                       image_resource_id=local.json()["version"]))
        await db.commit()
    removed = await client.delete(f"{path}/entries/{item['id']}/thumbnail", params={
        "expected_version": item["thumbnail"]["version"], "expected_revision": promoted.json()["revision"],
    })
    assert removed.status_code == 200, removed.text
    assert (await client.get(f"{base}/characters/{char['id']}")).json()["thumbnail"] is None
    assert (await client.get(f"{base}/characters")).json()[0]["thumbnail"] is None
    assert (await client.get(thumbnail)).status_code == 404


@pytest.mark.parametrize(("method", "path", "limit"), [
    ("PUT", "/api/v1/projects/1/settings/character/1/thumbnail", 11 * 1024 * 1024),
    ("PUT", "/api/v1/series/1/entries/1/thumbnail", 11 * 1024 * 1024),
    ("POST", "/api/v1/projects/1/cards/import", 17 * 1024 * 1024),
    ("POST", "/api/v1/projects/1/cards/import/preview", 17 * 1024 * 1024),
])
async def test_upload_body_limits_are_enforced_before_parsing(client, method, path, limit):
    response = await client.request(method, path, content=b"", headers={"Content-Length": str(limit + 1)})
    assert response.status_code == 400
    assert "过大" in response.json()["detail"]


@pytest.mark.parametrize(("kind", "path", "body", "patch", "has_image"), [
    ("character", "characters", {"name": "共享角色"}, {"profile": "草稿"}, True),
    ("world", "world", {"title": "共享地点", "category": "place"}, {"content": "草稿"}, False),
])
async def test_thumbnail_editable_follows_book_permission_without_disclosing_series(
    client, make_client, kind, path, body, patch, has_image,
):
    await register(client, f"thumb-owner-{kind}")
    editor, viewer = make_client(), make_client()
    await register(editor, f"thumb-editor-{kind}")
    await register(viewer, f"thumb-viewer-{kind}")
    book = await create_project(client)
    base = f"/api/v1/projects/{book['id']}"
    shared = await client.post(f"{base}/{path}", json=body)
    assert shared.status_code == 201 and shared.json()["thumbnail_editable"] is True, shared.text
    entity_id = shared.json()["id"]
    updated = await client.patch(f"{base}/{path}/{entity_id}", json=patch)
    assert updated.status_code == 200 and updated.json()["thumbnail_editable"] is True, updated.text
    local = await client.post(f"{base}/{path}", json=body)
    assert local.status_code == 201 and local.json()["thumbnail_editable"] is True, local.text
    local_id = local.json()["id"]
    for guest, role in ((editor, "editor"), (viewer, "viewer")):
        invite = await client.post(f"{base}/collaborators", json={
            "username": f"thumb-{role}-{kind}", "role": role,
        })
        assert invite.status_code == 201, invite.text
        member = invite.json()
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{member['invitation_id']}/accept",
            json={"expected_revision": member["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    for guest, allowed in ((client, True), (editor, True), (viewer, False)):
        detail = await guest.get(f"{base}/{path}/{entity_id}")
        assert detail.status_code == 200 and detail.json()["thumbnail_editable"] is allowed, detail.text
        rows = (await guest.get(f"{base}/{path}")).json()
        assert all(item["thumbnail_editable"] is allowed for item in rows)
    thumbnail = f"{base}/settings/{kind}/{entity_id}/thumbnail"
    if has_image:
        upload = await client.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")},
                                  data={"expected_version": ""})
        assert upload.status_code == 200, upload.text
    series = (await client.post("/api/v1/series", json={"name": "隐藏系列"})).json()
    series_url = f"/api/v1/series/{series['id']}"
    joined = await client.post(f"{series_url}/books", json={"project_id": book["id"], "expected_revision": 0})
    impact = await client.get(f"{series_url}/impact", params={
        "action": "promote", "project_id": book["id"], "kind": kind, "entry_id": entity_id,
    })
    assert impact.status_code == 200, impact.text
    promoted = await client.post(f"{series_url}/entries/promote", json={
        "project_id": book["id"], "ref": {"kind": kind, "id": entity_id},
        "expected_revision": joined.json()["revision"], "source_stamp": impact.json()["source_stamp"],
    })
    assert promoted.status_code == 201, promoted.text
    for guest, local_allowed in ((client, True), (editor, True), (viewer, False)):
        response = await guest.get(f"{base}/{path}/{entity_id}")
        assert response.status_code == 200 and response.json()["thumbnail_editable"] is False, response.text
        rows = {item["id"]: item for item in (await guest.get(f"{base}/{path}")).json()}
        assert rows[entity_id]["thumbnail_editable"] is False
        assert rows[local_id]["thumbnail_editable"] is local_allowed
        if guest is not client:
            assert rows[entity_id]["origin"]["scope"] == "book"
            assert rows[entity_id]["origin"]["series_id"] is None
    forbidden = await editor.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")},
                                 data={"expected_version": ""})
    assert forbidden.status_code == 409, forbidden.text
    patched = await editor.patch(f"{base}/{path}/{entity_id}", json={"sort": 2})
    assert patched.status_code == 200 and patched.json()["thumbnail_editable"] is False, patched.text

    # 系列 editor 能使用系列入口，仍不能通过作品入口修改共享图。
    invite = await client.post(f"{series_url}/collaborators", json={
        "username": f"thumb-editor-{kind}",
    })
    assert invite.status_code == 201, invite.text
    member = invite.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/series/{member['invitation_id']}/accept",
        json={"expected_revision": member["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert (await editor.get(f"{base}/{path}/{entity_id}")).json()["thumbnail_editable"] is False
    item = promoted.json()["item"]
    expected = item["thumbnail"]["version"] if item["thumbnail"] else ""
    changed = await editor.put(f"{series_url}/entries/{item['id']}/thumbnail", files={
        "file": ("b.png", image_data("blue"), "image/png"),
    }, data={"expected_version": expected, "expected_revision": str(promoted.json()["revision"])})
    assert changed.status_code == 200, changed.text
    await editor.aclose()
    await viewer.aclose()
