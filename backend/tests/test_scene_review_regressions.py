"""独立审查：一致读取屏障和结构历史的真实规划归属。"""

import json

import httpx
import pytest

from app.services import scene_ai_context, scene_service
from app.services.ai_dispatch import guard_provider
from tests.test_ai_visibility_dispatch import PROVIDERS, _collect, _server, _sse
from tests.test_project_transfer import _backup, _preview_raw
from tests.test_scenes import setup_scene


@pytest.mark.parametrize("offset", [0, 50])
async def test_scene_page_rejects_chapter_changed_between_parent_and_rows(client, monkeypatch, offset):
    _, _, url, scene = await setup_scene(client, "原稿")
    original = scene_service.get_manageable_chapter
    changed = False

    async def update_after_parent_read(*args, **kwargs):
        nonlocal changed
        chapter = await original(*args, **kwargs)
        if not changed:
            changed = True
            response = await client.patch(f"{url}/scenes/{scene['id']}", json={
                "expected_content_revision": 0, "content": "另一请求提交的新稿",
            })
            assert response.status_code == 200, response.text
        return chapter

    monkeypatch.setattr(scene_service, "get_manageable_chapter", update_after_parent_read)
    response = await client.get(f"{url}/scenes?offset={offset}")
    assert changed
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "scene_context_changed"


async def _planned_backup(client):
    project, chapter, url, first = await setup_scene(client, "第一章原稿")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "视角人物"})).json()

    async def plan(scene, revision, content_revision):
        response = await client.patch(f"{base}/planning/units/batch", json={
            "expected_revision": revision,
            "expected_content_revisions": [{"chapter_id": scene["chapter_id"],
                                            "content_revision": content_revision}],
            "targets": [{"kind": "scene", "id": scene["id"]}],
            "pov": {"op": "set", "character_id": character["id"]},
        })
        assert response.status_code == 200, response.text
        return response.json()["revision"]

    await plan(first, first["planning_revision"], 0)
    second_chapter = await client.post(f"{base}/chapters", json={"title": "第二章", "content": "第二章原稿"})
    assert second_chapter.status_code == 201, second_chapter.text
    second_page = (await client.get(f"{base}/chapters/{second_chapter.json()['id']}/scenes")).json()
    await plan(second_page["items"][0], second_page["planning_revision"], 0)
    data = await _backup(client, project["id"])
    first_unit = next(row for row in data["tables"]["planning_units"] if row["scene_id"] == first["id"])
    first_history = next(
        row for row in data["tables"]["chapter_versions"]
        if row["chapter_id"] == chapter["id"]
        and row["scene_snapshot"]["scenes"][0]["planning"] is not None
    )
    assert first_history["scene_snapshot"]["scenes"][0]["planning"]["unit_id"] == first_unit["id"]
    return data, first_history, first_unit, url


async def test_backup_preview_rejects_scene_history_unit_from_another_chapter(client):
    data, history, unit, _ = await _planned_backup(client)
    other_unit = next(row for row in data["tables"]["planning_units"] if row["id"] != unit["id"])
    history["scene_snapshot"]["scenes"][0]["planning"]["unit_id"] = other_unit["id"]
    response = await _preview_raw(client, json.dumps(data).encode())
    assert response.status_code == 400, response.text
    assert any("场景历史" in error and "章节" in error for error in response.json()["errors"])


async def test_backup_preview_preserves_legitimate_same_chapter_later_unit_rebinding(client):
    data, history, unit, url = await _planned_backup(client)
    # 同章改挂是合法的后续作者意图；旧快照不可恢复，不等于备份应该丢弃这段历史。
    created = await client.post(f"{url}/scenes", json={"expected_content_revision": 1, "content": "同章后景"})
    assert created.status_code == 201, created.text
    scene = created.json()
    rebound = await client.post(
        f"/api/v1/projects/{data['project']['id']}/planning/units/{unit['id']}/binding", json={
            "expected_revision": scene["planning_revision"],
            "expected_content_revisions": [{"chapter_id": scene["chapter_id"], "content_revision": 2}],
            "target": {"kind": "scene", "id": scene["id"]},
        },
    )
    assert rebound.status_code == 200, rebound.text
    current = await _backup(client, data["project"]["id"])
    moved_unit = next(row for row in current["tables"]["planning_units"] if row["id"] == unit["id"])
    assert moved_unit["scene_id"] == scene["id"] and moved_unit["binding_epoch"] > unit["binding_epoch"]
    assert history["scene_snapshot"]["scenes"][0]["id"] != moved_unit["scene_id"]
    response = await _preview_raw(client, json.dumps(current).encode())
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("source", ["current", "history"])
async def test_scene_tag_delete_preserves_local_intent_and_restorable_backup(client, source):
    project, chapter, url, scene = await setup_scene(client, "正文")
    base = f"/api/v1/projects/{project['id']}"
    tag_response = await client.post(f"{base}/planning/tags", json={
        "expected_revision": scene["planning_revision"], "name": "作者标签",
    })
    assert tag_response.status_code == 201, tag_response.text
    tag = tag_response.json()["item"]
    response = await client.patch(f"{base}/planning/units/batch", json={
        "expected_revision": tag_response.json()["revision"],
        "expected_content_revisions": [{"chapter_id": chapter["id"], "content_revision": 0}],
        "targets": [{"kind": "scene", "id": scene["id"]}],
        "tags": {"op": "replace", "ids": [tag["id"]]},
    })
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    if source == "history":
        response = await client.patch(f"{base}/planning/units/batch", json={
            "expected_revision": revision,
            "expected_content_revisions": [{"chapter_id": chapter["id"], "content_revision": 1}],
            "targets": [{"kind": "scene", "id": scene["id"]}], "tags": {"op": "inherit"},
        })
        assert response.status_code == 200, response.text
        revision = response.json()["revision"]
    deleted = await client.delete(f"{base}/planning/tags/{tag['id']}?expected_revision={revision}")
    data = await _backup(client, project["id"])
    preview = await _preview_raw(client, json.dumps(data).encode())
    assert deleted.status_code == 409, f"删除={deleted.text}，随后完整备份预览={preview.status_code} {preview.text}"
    assert deleted.json()["code"] == "planning_tag_in_use"
    wanted = "scene_planning" if source == "current" else "scene_history"
    assert any(item["kind"] == wanted for item in deleted.json()["sources"])
    assert preview.status_code == 200, preview.text
    latest = (await client.get(f"{url}/scenes/{scene['id']}")).json()
    assert latest["planning_revision"] == revision


async def test_chapter_only_tag_still_can_be_deleted_without_scene_local_dependency(client):
    project, chapter, _, scene = await setup_scene(client, "正文")
    base = f"/api/v1/projects/{project['id']}"
    tag = (await client.post(f"{base}/planning/tags", json={
        "expected_revision": scene["planning_revision"], "name": "旧章标签",
    })).json()
    planned = await client.patch(f"{base}/planning/units/batch", json={
        "expected_revision": tag["revision"], "targets": [{"kind": "chapter", "id": chapter["id"]}],
        "tags": {"op": "replace", "ids": [tag["item"]["id"]]},
    })
    assert planned.status_code == 200, planned.text
    deleted = await client.delete(
        f"{base}/planning/tags/{tag['item']['id']}?expected_revision={planned.json()['revision']}",
    )
    assert deleted.status_code == 200, deleted.text
    data = await _backup(client, project["id"])
    preview = await _preview_raw(client, json.dumps(data).encode())
    assert preview.status_code == 200, preview.text


async def test_editor_full_payload_merges_only_body_saves_but_keeps_metadata_boundaries(client):
    _, _, url, scene = await setup_scene(client, "初稿")
    for revision, content in enumerate(("第一遍改稿🙂  ", "第二遍改稿\n 尾  ")):
        saved = await client.patch(f"{url}/scenes/{scene['id']}", json={
            "expected_content_revision": revision, "title": scene["title"], "summary": scene["summary"],
            "content": content,
        })
        assert saved.status_code == 200 and saved.json()["content"] == content, saved.text
    versions = (await client.get(f"{url}/versions")).json()
    assert len(versions) == 1, versions
    assert versions[0]["source"] == "manual" and versions[0]["note"] == ""

    for revision, title, summary in ((2, " 新标题 ", ""), (3, "新标题", "作者摘要")):
        saved = await client.patch(f"{url}/scenes/{scene['id']}", json={
            "expected_content_revision": revision, "title": title, "summary": summary,
            "content": "第二遍改稿\n 尾  ",
        })
        assert saved.status_code == 200, saved.text
    versions = (await client.get(f"{url}/versions")).json()
    assert len(versions) == 3
    assert all(version["note"] == "修改场景资料" for version in versions[:2])
    assert (await client.get(f"{url}/versions/{versions[2]['id']}")).json()["content"] == "第二遍改稿\n 尾  "


async def test_deleting_chapter_keeps_orphan_scene_local_cast_without_legacy_detached_copy(client):
    project, chapter, url, scene = await setup_scene(client, "正文")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "场景本地角色"})).json()
    planned = await client.patch(f"{base}/planning/units/batch", json={
        "expected_revision": scene["planning_revision"],
        "expected_content_revisions": [{"chapter_id": chapter["id"], "content_revision": 0}],
        "targets": [{"kind": "scene", "id": scene["id"]}],
        "references": [{"kind": "character", "op": "replace", "ids": [character["id"]]}],
    })
    assert planned.status_code == 200, planned.text
    deleted = await client.delete(url)
    assert deleted.status_code == 204, deleted.text
    data = await _backup(client, project["id"])
    preview = await _preview_raw(client, json.dumps(data).encode())
    assert preview.status_code == 200, preview.text
    unit = data["tables"]["planning_units"][0]
    assert unit["scene_id"] is None and unit["source_level"] == "scene"
    assert unit["detached_character_ids"] == []
    assert data["tables"]["planning_unit_characters"][0]["character_id"] == character["id"]


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_actual_sdk_send_rejects_old_scene_stamp_without_visibility_change(app, client, kind):
    project, chapter, url, scene = await setup_scene(client, "发送前原稿")
    maker = app.state.sessionmaker
    async with maker() as db:
        stamp = await scene_ai_context.read_stamp(db, project["id"], chapter["id"])
    saved = await client.patch(f"{url}/scenes/{scene['id']}", json={
        "expected_content_revision": 0, "content": "作者已改稿，旧输入不得发送",
    })
    assert saved.status_code == 200, saved.text
    async with maker() as db:
        current = await scene_ai_context.read_stamp(db, project["id"], chapter["id"])
    # 旧可见性门禁单独无法发现这次正文变化，必须真实调用新增Scene verify。
    assert current.visibility == stamp.visibility and current.content_revision != stamp.content_revision
    async with _server(payload=_sse(kind)) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        raw = PROVIDERS[kind]("local-key", f"{url}/v1/" if kind == "openai" else url, 5)
        provider = guard_provider(raw, stamp.visibility, maker, verify=lambda db: scene_ai_context.require_stamp(
            db, stamp, sending=True,
        ))
        with pytest.raises(scene_ai_context.SceneContextChanged):
            await _collect(provider.generate_stream([{"role": "user", "content": "旧场景负载哨兵"}], "test-model"))
        assert requests == []


@pytest.mark.parametrize("identity", [0, -1, 2_147_483_648, 10**50])
@pytest.mark.parametrize("part", ["project", "chapter", "scene"])
async def test_basic_scene_invalid_db_path_identity_is_request_validation(app, client, part, identity):
    project, chapter, _, scene = await setup_scene(client, "原稿")
    identities = {"project": project["id"], "chapter": chapter["id"], "scene": scene["id"]}
    identities[part] = identity
    # 独立HTTP传输捕获真实500响应，而非让ASGITransport重抛整数绑定异常。
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://testserver", cookies=client.cookies) as raw:
        response = await raw.get(
            f"/api/v1/projects/{identities['project']}/chapters/{identities['chapter']}/scenes/{identities['scene']}",
        )
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("请求参数校验失败")


@pytest.mark.parametrize("identity", [2_147_483_648, 10**50])
@pytest.mark.parametrize("operation", ["after_scene_id", "reorder"])
async def test_basic_scene_invalid_db_body_identity_is_request_validation(app, client, identity, operation):
    _, _, url, scene = await setup_scene(client, "原稿")
    path = f"{url}/scenes"
    body = {"expected_content_revision": 0}
    if operation == "reorder":
        path += "/reorder"
        body["scene_ids"] = [scene["id"], identity]
    else:
        body["after_scene_id"] = identity
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://testserver", cookies=client.cookies) as raw:
        response = await raw.post(path, json=body)
    assert response.status_code == 422, response.text
