"""旧写入入口不能在不知道富文档的情况下破坏作者资料。"""

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import Chapter, Project, Scene, SceneDocumentMeta, SceneMarker, SceneSection
from app.services import scene_service
from tests.test_scenes import setup_scene


async def _seed(db, scene_id, kind):
    if kind == "format":
        db.add(SceneDocumentMeta(scene_id=scene_id, formatting={
            "version": 1, "inline": [{"kind": "bold", "start": 0, "end": 1}],
        }))
    elif kind == "section":
        db.add(SceneSection(scene_id=scene_id, logical_key="12345678-1234-4123-8123-123456789abc",
                            anchor=1, sort=0, title="私密", content="作者资料", include_in_ai=False))
    else:
        db.add(SceneMarker(scene_id=scene_id, logical_key="12345678-1234-4123-8123-123456789def",
                           status="stale", color="blue", last_start=0, excerpt="曾经的字"))
    await db.commit()


@pytest.mark.parametrize("kind", ["format", "section", "marker"])
async def test_legacy_replace_primitives_refuse_to_guess_mapping(app, client, kind):
    _project, chapter, _url, scene = await setup_scene(client, "甲🙂乙")
    async with app.state.sessionmaker() as db:
        await _seed(db, scene["id"], kind)
        parent, target = await db.get(Chapter, chapter["id"]), await db.get(Scene, scene["id"])
        for replace in (
            lambda: scene_service.replace_scene_content(db, parent, target, "新稿"),
            lambda: scene_service.replace_chapter_contents(db, parent, {target.id: "新稿"}),
            lambda: scene_service.replace_legacy_content(db, parent, "新稿"),
        ):
            with pytest.raises(ConflictError) as exc:
                await replace()
            assert exc.value.extra["code"] == "rich_document_unmapped"
            assert target.content == "甲🙂乙" and parent.content_revision == 0


@pytest.mark.parametrize("kind", ["format", "section", "marker"])
@pytest.mark.parametrize("endpoint", ["scene", "chapter", "split", "restore"])
async def test_legacy_http_mutations_leave_original_document_unchanged(app, client, kind, endpoint):
    _project, chapter, url, scene = await setup_scene(client, "甲🙂乙")
    versions = (await client.get(f"{url}/versions")).json()
    async with app.state.sessionmaker() as db:
        await _seed(db, scene["id"], kind)
    if endpoint == "scene":
        response = await client.patch(f"{url}/scenes/{scene['id']}", json={
            "expected_content_revision": 0, "content": "不能覆盖",
        })
    elif endpoint == "chapter":
        response = await client.patch(url, json={"expected_content_revision": 0, "content": "不能覆盖"})
    elif endpoint == "split":
        response = await client.post(f"{url}/scenes/{scene['id']}/split", json={
            "expected_content_revision": 0, "offset": 1,
        })
    else:
        response = await client.post(f"{url}/versions/{versions[0]['id']}/restore", json={
            "expected_content_revision": 0,
        })
    assert response.status_code == 409, response.text
    expected_code = "rich_document_confirmation_required" if endpoint == "restore" else "rich_document_unmapped"
    assert response.json()["code"] == expected_code
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).content_revision == 0
        assert (await db.get(Scene, scene["id"])).content == "甲🙂乙"
        assert len(list(await db.scalars(select(Scene).where(Scene.chapter_id == chapter["id"])))) == 1


@pytest.mark.parametrize("operation", ["merge", "delete"])
async def test_legacy_structure_rejects_unproven_identity_retirement(app, client, operation):
    _project, chapter, url, first = await setup_scene(client, "甲乙")
    second = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "丙丁"})
    assert second.status_code == 201
    second_id = second.json()["id"]
    async with app.state.sessionmaker() as db:
        await _seed(db, second_id, "section")
        if operation == "merge":
            # 两景有无血缘证明的同 UUID，不能猜测它们是同一区块。
            await _seed(db, first["id"], "section")
    if operation == "delete":
        response = await client.delete(f"{url}/scenes/{second_id}?expected_content_revision=1")
    else:
        preview = await client.post(f"{url}/scenes/merge-preview", json={
            "expected_content_revision": 1, "scene_ids": [first["id"], second_id],
        })
        assert preview.status_code == 200, preview.text
        info = preview.json()
        assert info["blockers"] == [{"kind": "rich_document", "id": second_id, "scene_id": second_id,
                                      "label": "富格式、标记或区块需要完整文档映射"}]
        response = await client.post(f"{url}/scenes/merge", json={
            "expected_content_revision": 1, "scene_ids": info["scene_ids"],
            "expected_planning_revision": info["planning_revision"], "signature": info["signature"],
            "metadata_source": "left",
        })
    expected_code = "scene_reference_conflict" if operation == "merge" else "rich_document_unmapped"
    assert response.status_code == 409 and response.json()["code"] == expected_code
    async with app.state.sessionmaker() as db:
        assert (await db.get(Scene, second_id)).active is True
        assert (await db.get(Chapter, chapter["id"])).content_revision == 1


async def test_pending_document_is_protected_before_flush(app, client):
    _project, chapter, _url, scene = await setup_scene(client, "甲乙")
    async with app.state.sessionmaker() as db:
        parent, target = await db.get(Chapter, chapter["id"]), await db.get(Scene, scene["id"])
        db.add(SceneDocumentMeta(scene_id=target.id, formatting={}))
        with pytest.raises(ConflictError) as exc:
            await scene_service.replace_scene_content(db, parent, target, "新稿")
        assert exc.value.extra["code"] == "rich_document_unmapped"
        assert target.content == "甲乙"


async def test_plain_legacy_split_preserves_existing_crlf_codepoint_contract(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲\r\n乙")
    response = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert response.status_code == 200
    assert (await client.get(url)).json()["content"] == "甲\r\n乙"


@pytest.mark.parametrize("kind", ["format", "section", "marker"])
@pytest.mark.parametrize("endpoint", ["scene", "chapter"])
async def test_legacy_tail_edit_preserves_unaffected_rich_structure(app, client, kind, endpoint):
    _project, _chapter, url, scene = await setup_scene(client, "甲🙂乙")
    async with app.state.sessionmaker() as db:
        await _seed(db, scene["id"], kind)
    document_url = f"{url}/scenes/{scene['id']}/document"
    before = (await client.get(document_url)).json()["document"]
    target = f"{url}/scenes/{scene['id']}" if endpoint == "scene" else url
    result = await client.patch(target, json={"expected_content_revision": 0, "content": "甲🙂乙尾\r\n"})
    assert result.status_code == 200, result.text
    after = (await client.get(document_url)).json()["document"]
    assert after == {**before, "content": "甲🙂乙尾\r\n"}


async def test_legacy_edit_at_section_anchor_remains_ambiguous_and_rejected(app, client):
    _project, _chapter, url, scene = await setup_scene(client, "甲🙂乙")
    async with app.state.sessionmaker() as db:
        await _seed(db, scene["id"], "section")
    response = await client.patch(f"{url}/scenes/{scene['id']}", json={
        "expected_content_revision": 0, "content": "甲新🙂乙",
    })
    assert response.status_code == 409 and response.json()["code"] == "rich_document_unmapped"


@pytest.mark.parametrize("kind", ["format", "section", "active_marker"])
async def test_split_and_merge_keep_earlier_structure_without_relocation(app, client, kind):
    _project, _chapter, url, scene = await setup_scene(client, "甲🙂乙尾")
    async with app.state.sessionmaker() as db:
        if kind == "active_marker":
            db.add(SceneMarker(scene_id=scene["id"], logical_key="12345678-1234-4123-8123-123456789def",
                               status="active", color="blue", start_offset=0, end_offset=1))
            await db.commit()
        else:
            await _seed(db, scene["id"], kind)
    endpoint = f"{url}/scenes/{scene['id']}/document"
    original = (await client.get(endpoint)).json()["document"]
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 3,
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    assert (await client.get(endpoint)).json()["document"] == {**original, "content": "甲🙂乙"}
    assert (await client.get(url)).json()["content"] == "甲🙂乙尾"
    preview = await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 1, "scene_ids": [left["id"], right["id"]],
    })
    assert preview.status_code == 200, preview.text
    info = preview.json()
    assert not info["blockers"]
    merged = await client.post(f"{url}/scenes/merge", json={
        "expected_content_revision": 1, "scene_ids": info["scene_ids"],
        "expected_planning_revision": info["planning_revision"], "signature": info["signature"],
        "metadata_source": "left",
    })
    assert merged.status_code == 200, merged.text
    assert (await client.get(endpoint)).json()["document"] == original
    assert (await client.get(url)).json()["content"] == "甲🙂乙尾"


async def test_split_crossing_active_marker_cannot_guess_ownership(app, client):
    _project, _chapter, url, scene = await setup_scene(client, "甲🙂乙尾")
    async with app.state.sessionmaker() as db:
        db.add(SceneMarker(scene_id=scene["id"], logical_key="12345678-1234-4123-8123-123456789def",
                           status="active", color="blue", start_offset=1, end_offset=4))
        await db.commit()
    result = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert result.status_code == 409 and result.json()["code"] == "rich_document_unmapped"


@pytest.mark.parametrize("kind", ["format", "active_marker"])
async def test_split_and_merge_move_right_format_or_marker_with_history(app, client, kind):
    _project, _chapter, url, scene = await setup_scene(client, "甲🙂乙丙丁尾")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    async with app.state.sessionmaker() as db:
        if kind == "format":
            db.add(SceneDocumentMeta(scene_id=scene["id"], formatting={
                "version": 1, "inline": [{"kind": "bold", "start": 4, "end": 5}],
            }))
        else:
            db.add(SceneMarker(scene_id=scene["id"], logical_key="12345678-1234-4123-8123-123456789def",
                               status="active", color="blue", start_offset=4, end_offset=5))
        await db.commit()
    original = (await client.get(endpoint)).json()["document"]
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 3,
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    right_doc = (await client.get(f"{url}/scenes/{right['id']}/document")).json()["document"]
    if kind == "format":
        assert right_doc["formatting"]["inline"] == [{"kind": "bold", "start": 1, "end": 2}]
    else:
        assert (right_doc["markers"][0]["start"], right_doc["markers"][0]["end"]) == (1, 2)
    preview = await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 1, "scene_ids": [left["id"], right["id"]],
    })
    assert preview.status_code == 200 and not preview.json()["blockers"], preview.text
    info = preview.json()
    merged = await client.post(f"{url}/scenes/merge", json={
        "expected_content_revision": 1, "scene_ids": info["scene_ids"],
        "expected_planning_revision": info["planning_revision"], "signature": info["signature"],
        "metadata_source": "left",
    })
    assert merged.status_code == 200, merged.text
    assert (await client.get(endpoint)).json()["document"] == original
    # 历史身份在拆分时已作为墓碑保留；恢复拆分状态须能重新启用它。
    versions = (await client.get(f"{url}/versions")).json()
    split_version = next(item for item in versions if item["note"].startswith("手动拆分"))
    restored = await client.post(f"{url}/versions/{split_version['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert (await client.get(f"{url}/scenes/{right['id']}/document")).json()["document"] == right_doc


async def test_merge_rejects_unrelated_marker_identity_even_if_text_and_color_match(app, client):
    _project, _chapter, url, first = await setup_scene(client, "甲乙")
    created = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "甲乙"})
    assert created.status_code == 201
    second = created.json()
    async with app.state.sessionmaker() as db:
        for scene in (first, second):
            db.add(SceneMarker(scene_id=scene["id"], logical_key="12345678-1234-4123-8123-123456789def",
                               status="active", color="blue", start_offset=0, end_offset=1))
        await db.commit()
    preview = await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 1, "scene_ids": [first["id"], second["id"]],
    })
    assert preview.status_code == 200 and preview.json()["blockers"]


async def test_split_and_merge_right_section_with_dependent_marker_and_visibility(app, client):
    project, _chapter, url, scene = await setup_scene(client, "甲🙂乙丙丁尾")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    async with app.state.sessionmaker() as db:
        section = SceneSection(scene_id=scene["id"], logical_key="12345678-1234-4123-8123-123456789abc",
                               anchor=4, sort=0, title="旁注", content="作者🙂", include_in_ai=True)
        db.add(section)
        await db.flush()
        db.add(SceneMarker(scene_id=scene["id"], logical_key="12345678-1234-4123-8123-123456789def",
                           section_id=section.id, status="active", color="blue", start_offset=0, end_offset=2))
        original_revision = (await db.get(Project, project["id"])).ai_visibility_revision
        await db.commit()
    original = (await client.get(endpoint)).json()["document"]
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 3,
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    right_doc = (await client.get(f"{url}/scenes/{right['id']}/document")).json()["document"]
    assert right_doc["sections"][0]["anchor"] == 1 and right_doc["sections"][0]["include_in_ai"] is True
    assert right_doc["markers"][0]["section_key"] == right_doc["sections"][0]["key"]
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).ai_visibility_revision == original_revision + 1
    preview = await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 1, "scene_ids": [left["id"], right["id"]],
    })
    assert preview.status_code == 200 and not preview.json()["blockers"], preview.text
    info = preview.json()
    merged = await client.post(f"{url}/scenes/merge", json={
        "expected_content_revision": 1, "scene_ids": info["scene_ids"],
        "expected_planning_revision": info["planning_revision"], "signature": info["signature"],
        "metadata_source": "left",
    })
    assert merged.status_code == 200, merged.text
    assert (await client.get(endpoint)).json()["document"] == original
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).ai_visibility_revision == original_revision + 2
    versions = (await client.get(f"{url}/versions")).json()
    split_version = next(item for item in versions if item["note"].startswith("手动拆分"))
    restored = await client.post(f"{url}/versions/{split_version['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert (await client.get(f"{url}/scenes/{right['id']}/document")).json()["document"] == right_doc
