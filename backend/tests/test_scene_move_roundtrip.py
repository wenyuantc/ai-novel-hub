"""双连接竞争下，跨章移动和作品备份保留历史与冻结稿件。"""

from tests import test_serialization_concurrency as database_cases
from tests.test_project_transfer import API, _backup, _import
from tests.test_scenes import setup_scene

settings_overrides = database_cases.settings_overrides


async def test_move_once_and_move_again_after_restoring_project(app, client):
    manuscript = "卷首🙂\r\n尾声  "
    project, source, source_url, scene = await setup_scene(client, manuscript)
    base = f"{API}/{project['id']}"
    checked = database_cases.successful
    target_resp = await client.post(f"{base}/chapters", json={"title": "下一章", "content": "续章正文"})
    assert target_resp.status_code == 201, target_resp.text
    target = target_resp.json()
    target_url = f"{base}/chapters/{target['id']}"
    created = await client.post(f"{base}/characters", json={"name": "尾声"})
    assert created.status_code == 201, created.text
    mentions = {"entity_kind": "character", "entity_id": created.json()["id"], "source_type": "chapter"}
    before_hit, = checked(await client.get(f"{base}/mentions", params=mentions))["items"]
    assert before_hit["source"]["id"] == source["id"]

    entry_url = f"{base}/serialization/chapters/{source['id']}"
    draft = checked(await client.get(entry_url))
    checked(await client.post(f"{entry_url}/editions", json=database_cases.finalize_body(draft)))
    edition = checked(await client.get(entry_url))
    publication = checked(await client.post(
        f"{entry_url}/publications", json=database_cases.publish_body(edition),
    ))

    original, = checked(await client.get(f"{source_url}/versions"))
    move_url = f"{source_url}/scenes/{scene['id']}/move"
    position = {"target_chapter_id": target["id"], "insert_before_scene_id": None}
    preview = checked(await client.post(f"{move_url}-preview", json=position))
    request = {**position, "signature": preview["signature"]}

    first, second = await database_cases.together(
        app, client.post(move_url, json=request), client.post(move_url, json=request),
    )
    accepted, = [response for response in (first, second) if response.is_success]
    stale, = [response for response in (first, second) if response is not accepted]
    assert stale.status_code in {404, 409}, stale.text
    assert accepted.json()["scene_id"] == scene["id"]
    remaining, = checked(await client.get(f"{source_url}/scenes"))["items"]
    assert remaining["id"] != scene["id"]
    assert checked(await client.get(source_url))["content"] == ""
    assert checked(await client.get(target_url))["content"] == f"续章正文{manuscript}"
    reader = checked(await client.get(f"{base}/reader/chapters/{target['id']}"))
    assert reader["chapter"]["content"] == f"续章正文{manuscript}"
    current_hit, = checked(await client.get(f"{base}/mentions", params=mentions))["items"]
    assert current_hit["source"]["id"] == target["id"]
    location = checked(await client.get(f"{target_url}/scenes/resolve-position", params={
        "fingerprint": current_hit["content_hash"], "start": current_hit["start"], "end": current_hit["end"],
    }))
    assert location["scene_id"] == scene["id"]
    stale_location = checked(await client.get(f"{source_url}/scenes/resolve-position", params={
        "fingerprint": before_hit["content_hash"], "start": before_hit["start"], "end": before_hit["end"],
    }))
    assert stale_location["reason"] == "stale"
    old = checked(await client.get(f"{source_url}/versions/{original['id']}"))
    assert old["content"] == manuscript and old["scene_snapshot"]["scenes"][0]["id"] == scene["id"]

    old_edition = checked(await client.get(entry_url))
    assert old_edition["edition"]["content"] == manuscript
    assert old_edition["draft_changed"] is True
    publications = checked(await client.get(f"{base}/serialization/publications"))
    assert [item["id"] for item in publications["items"]] == [publication["id"]]
    protected = await client.delete(target_url)
    assert protected.status_code == 409 and protected.json()["code"] == "scene_move_history_in_use", protected.text
    assert checked(await client.get(target_url))["content"] == f"续章正文{manuscript}"

    backup = await _backup(client, project["id"])
    copy_id = (await _import(client, backup))["project"]["id"]
    copy = await _backup(client, copy_id)
    copy_source, copy_target = sorted(copy["tables"]["chapters"], key=lambda row: row["chapter_no"])
    copied_scene, = [
        row for row in copy["tables"]["scenes"]
        if row["chapter_id"] == copy_target["id"] and row["content"] == manuscript
    ]
    assert copied_scene["id"] != scene["id"]

    copy_source_url = f"{API}/{copy_id}/chapters/{copy_source['id']}"
    copy_target_url = f"{API}/{copy_id}/chapters/{copy_target['id']}"
    copy_versions = checked(await client.get(f"{copy_source_url}/versions"))
    copied_version, = [item for item in copy_versions if item["seq"] == original["seq"]]
    copied_history = checked(await client.get(f"{copy_source_url}/versions/{copied_version['id']}"))
    assert copied_history["scene_snapshot"]["scenes"][0]["id"] == copied_scene["id"]
    return_url = f"{copy_target_url}/scenes/{copied_scene['id']}/move"
    return_position = {"target_chapter_id": copy_source["id"], "insert_before_scene_id": None}
    return_preview = checked(await client.post(f"{return_url}-preview", json=return_position))
    return_request = {**return_position, "signature": return_preview["signature"]}
    returned = checked(await client.post(return_url, json=return_request))
    assert returned["scene_id"] == copied_scene["id"]
    assert checked(await client.get(copy_source_url))["content"] == manuscript
    assert checked(await client.get(copy_target_url))["content"] == "续章正文"
    assert checked(await client.get(source_url))["content"] == ""
