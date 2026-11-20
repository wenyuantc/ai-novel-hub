"""使用实际移动和删章入口保护仍存续的结构历史与章节身份。"""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text

from app.models import Base, ChapterVersion, Scene
from app.schemas.book_search import SelectedHit
from tests.test_batch_revision import chapter as create_chapter
from tests.test_consistency_migration import _engine
from tests.test_project_transfer import API, _backup, _import, _preview_raw
from tests.test_scene_planning import planning_batch
from tests.test_scenes import setup_scene
from tests.test_setting_fields_migration import _config, _schema_contract


async def _first_scene(client, url: str) -> int:
    return (await client.get(f"{url}/scenes")).json()["items"][0]["id"]


async def _move(client, source: str, scene_id: int, target: dict) -> dict:
    url = f"{source}/scenes/{scene_id}"
    body = {"target_chapter_id": target["id"], "insert_before_scene_id": None}
    ready = await client.post(f"{url}/move-preview", json=body)
    assert ready.status_code == 200, ready.text
    applied = await client.post(f"{url}/move", json={**body, "signature": ready.json()["signature"]})
    assert applied.status_code == 200, applied.text
    return applied.json()


def test_0065_retains_movement_proof_until_last_scene_is_deleted(tmp_path):
    path = tmp_path / "scene-moves.db"
    config = _config(path)
    command.upgrade(config, "0064")
    engine = _engine(path)
    reference = create_engine("sqlite://")
    try:
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES (1,1,'作品','','','','planning')"))
            db.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                            "VALUES(1,1,1,'源章','','draft',0),(2,1,2,'目标章','现稿','draft',2)"))
            db.execute(text("INSERT INTO scenes(id,chapter_id,title,content,word_count) VALUES(1,2,'原景','现稿',2)"))
        command.upgrade(config, "0065")
        Base.metadata.create_all(reference)
        assert _schema_contract(inspect(engine), "scene_moves") == _schema_contract(inspect(reference), "scene_moves")
        with engine.begin() as db:
            assert db.execute(text("SELECT id,chapter_id,content FROM scenes")).one() == (1, 2, "现稿")
            db.execute(text("INSERT INTO scene_moves(scene_id,from_chapter_id,to_chapter_id,first_move,"
                            "from_version_seq,to_version_seq) VALUES (1,1,2,TRUE,2,3)"))
            db.execute(text("DELETE FROM chapters WHERE id=1"))
            assert db.execute(text("SELECT from_chapter_id,to_chapter_id,first_move FROM scene_moves")).one() == (
                None, 2, True,
            )
        with pytest.raises(RuntimeError, match="先备份"):
            command.downgrade(config, "0064")
        with engine.begin() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0065"
            db.execute(text("DELETE FROM chapters WHERE id=2"))
            assert db.scalar(text("SELECT count(*) FROM scene_moves")) == 0
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        command.downgrade(config, "0064")
        assert "scene_moves" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        reference.dispose()


async def test_deleting_a_chapter_protects_only_surviving_versions_and_detaches_old_batch(client):
    project, origin, source, scene = await setup_scene(client, "原名称")
    base = f"{API}/{project['id']}"
    target_chapter = await create_chapter(client, base, 2, "独立的目标稿")
    target = f"{base}/chapters/{target_chapter['id']}"
    change = {"query": "原名称", "replacement": "改后名称"}
    suggestion = await client.post(f"{base}/book-search/preview", json=change)
    item, = suggestion.json()["items"]
    hit = {k: item[k] for k in SelectedHit.model_fields}
    revised = await client.post(f"{base}/book-search/apply", json={**change, "request_id": str(uuid4()), "hits": [hit]})
    assert revised.status_code == 200, revised.text

    moved = await _move(client, source, scene["id"], target_chapter)
    version_list = (await client.get(f"{source}/versions")).json()
    before = f"{source}/versions/{version_list[1]['id']}"
    historic = await client.get(before)
    assert historic.status_code == 200 and "改后名称" in historic.json()["content"]
    denied = await client.post(f"{before}/restore", json={
        "expected_content_revision": moved["source_content_revision"],
    })
    assert denied.status_code == 409 and denied.json()["code"] == "scene_move_restore_conflict"
    version_list = (await client.get(f"{target}/versions")).json()
    target_old = f"{target}/versions/{version_list[1]['id']}/restore-preview"
    denied = await client.get(target_old, params={"expected_content_revision": moved["target_content_revision"]})
    assert denied.status_code == 409 and denied.json()["code"] == "scene_move_restore_conflict"

    retained = await _backup(client, project["id"])
    detail, = retained["tables"]["revision_batch_scenes"]
    assert (detail["chapter_id"], detail["scene_id"], detail["matches_revision"]) == (origin["id"], scene["id"], False)
    copy = (await _import(client, retained))["project"]["id"]
    history = (await _backup(client, copy))["tables"]["revision_batches"][0]
    denied = await client.post(f"{API}/{copy}/book-search/batches/{history['id']}/undo")
    assert denied.status_code == 409

    failure = await client.delete(target)
    assert failure.status_code == 409 and failure.json()["code"] == "scene_move_history_in_use"
    removed = await client.delete(source)
    assert removed.status_code == 204, removed.text
    assert (await client.get(f"{target}/scenes/{scene['id']}")).json()["content"] == "改后名称"
    backup = await _backup(client, project["id"])
    detail, = backup["tables"]["revision_batch_scenes"]
    assert (detail["chapter_id"], detail["scene_id"], detail["matches_revision"]) == (None, None, False)
    assert detail["before_document"]["content"] == "原名称"
    assert backup["tables"]["scene_moves"][0]["from_chapter_id"] is None
    await _import(client, backup)


async def test_chapter_retains_identity_after_deleting_last_native_scene_with_no_live_history(client, app):
    project, origin, source, native = await setup_scene(client, "原生稿")
    base = f"{API}/{project['id']}"
    target_chapter = await create_chapter(client, base, 2, "目标稿")
    other = await create_chapter(client, base, 3, "移入稿")
    other_url = f"{base}/chapters/{other['id']}"
    borrowed = await _first_scene(client, other_url)
    serialization = f"{base}/serialization/chapters/{origin['id']}"
    await _move(client, other_url, borrowed, origin)
    await _move(client, source, native["id"], target_chapter)
    old_token = (await client.get(serialization)).json()["scene_id"]
    assert old_token == native["id"]
    versions = (await client.get(f"{source}/versions")).json()
    for old in versions[1:]:
        removed = await client.delete(f"{source}/versions/{old['id']}")
        assert removed.status_code == 204, removed.text
    removed = await client.delete(f"{base}/chapters/{target_chapter['id']}")
    assert removed.status_code == 204, removed.text

    view = (await client.get(serialization)).json()
    assert view["scene_id"] not in {old_token, borrowed}
    assert view["draft"]["content"] == "移入稿"
    async with app.state.sessionmaker() as db:
        anchor = await db.get(Scene, view["scene_id"])
        assert anchor is not None and anchor.chapter_id == origin["id"] and not anchor.active
        active = await db.scalars(select(Scene.id).where(Scene.chapter_id == origin["id"], Scene.active))
        assert list(active) == [borrowed]
    for export_versions in (0, 1):
        backup = await _backup(client, project["id"], versions=export_versions)
        if export_versions == 0:
            assert all(row["to_version_seq"] is None for row in backup["tables"]["scene_moves"])
        await _import(client, backup)


async def test_historic_planning_remains_readable_after_move_and_same_chapter_rebinding(client):
    project, _origin, source, scene = await setup_scene(client, "源章留存的规划")
    base = f"{API}/{project['id']}"
    target_chapter = await create_chapter(client, base, 2, "可重挂的新场景")
    target = f"{base}/chapters/{target_chapter['id']}"
    receiver = await _first_scene(client, target)
    change = await planning_batch(client, base, f"{source}/scenes", [{"kind": "scene", "id": scene["id"]}],
                                  pov={"op": "clear"})
    assert change.is_success, change.text
    unit_id = (await client.get(f"{source}/scenes/{scene['id']}")).json()["planning"]["unit_id"]
    moved = await _move(client, source, scene["id"], target_chapter)
    rebound = await client.post(f"{base}/planning/units/{unit_id}/binding", json={
        "target": {"kind": "scene", "id": receiver},
        "expected_revision": moved["planning_revision"],
        "expected_content_revisions": [{"chapter_id": target_chapter["id"],
                                        "content_revision": moved["target_content_revision"]}],
    })
    assert rebound.status_code == 200, rebound.text
    versions = (await client.get(f"{source}/versions")).json()
    historical = await client.get(f"{source}/versions/{versions[1]['id']}")
    assert historical.status_code == 200, historical.text
    assert historical.json()["scene_snapshot"]["scenes"][0]["planning"]["unit_id"] == unit_id
    backup = await _backup(client, project["id"])
    await _import(client, backup)


async def test_legacy_plain_version_after_move_can_be_explicitly_extracted(client, app):
    project, origin, source, scene = await setup_scene(client, "目前正文")
    async with app.state.sessionmaker() as db:
        old = ChapterVersion(chapter_id=origin["id"], seq=5, source="manual", content="旧纯文本", word_count=4)
        db.add(old)
        await db.commit()
        legacy_version = old.id
    target = await create_chapter(client, f"{API}/{project['id']}", 2, "目标章原稿")
    moved = await _move(client, source, scene["id"], target)
    version_url = f"{source}/versions/{legacy_version}"
    restore = await client.get(f"{version_url}/restore-preview", params={
        "expected_content_revision": moved["source_content_revision"],
    })
    assert restore.status_code == 409 and restore.json()["code"] == "scene_move_restore_conflict"
    selection = {"scene_id": await _first_scene(client, source),
                 "expected_content_revision": moved["source_content_revision"]}
    preview = await client.get(f"{version_url}/extract-preview", params=selection)
    assert preview.status_code == 200, preview.text
    extracted = await client.post(f"{version_url}/extract", json={
        **selection, "signature": preview.json()["signature"],
    })
    assert extracted.status_code == 200 and extracted.json()["content"] == "旧纯文本"


async def test_old_scene_cannot_anchor_recreated_chapter_even_after_moving_it_back(client):
    project, other, other_path, _ = await setup_scene(client, "留存")
    base = f"{API}/{project['id']}"
    origin = await create_chapter(client, base, 2, "旧章")
    origin_path = f"{base}/chapters/{origin['id']}"
    old_scene_id = await _first_scene(client, origin_path)
    await _move(client, origin_path, old_scene_id, other)
    assert (await client.delete(origin_path)).status_code == 204
    replacement = await create_chapter(client, base, 2, "新章")
    await _move(client, other_path, old_scene_id, replacement)
    workbench = f"{base}/serialization/chapters/{replacement['id']}"
    response = await client.patch(workbench, json={"expected_revision": 0, "expected_scene_id": old_scene_id,
                                                   "planned_date": "2027-01-01"})
    assert response.status_code == 409 and response.json()["code"] == "stale_chapter_identity"
    valid = await client.get(workbench)
    assert valid.json()["scene_id"] != old_scene_id

    backup = await _backup(client, project["id"])
    records = backup["tables"]["scene_moves"]
    assert [row["first_move"] for row in records] == [True, False]
    backup["tables"]["scene_moves"] = list(reversed(records))
    await _import(client, backup)
    broken = deepcopy(backup)
    broken["tables"]["scene_moves"][0]["first_move"] = True
    assert (await _preview_raw(client, json.dumps(broken).encode())).status_code == 400
    broken = deepcopy(backup)
    broken["version"] = 15
    assert (await _preview_raw(client, json.dumps(broken).encode())).status_code == 400
