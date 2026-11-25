"""批注与回复随单书往返，展示署名、次序和定位不会被重新解释。"""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import BadRequestError, ConflictError
from app.models import AnnotationMessage, AnnotationThread, Project
from app.schemas.transfer import BackupOptions
from app.services.project_transfer import exporter, importer
from app.services.project_transfer.annotations import ANNOTATION_TABLES
from app.services.scene_service import content_fingerprint
from tests.test_collaboration_content_access import _content_setup
from tests.test_project_transfer import API, _backup, _import
from tests.test_scenes import setup_scene
from tests.test_serialization_concurrency import successful


async def _thread(client, url, sid, start, selected, comment):
    revision = successful(await client.get(url))["content_revision"]
    return successful(await client.post(url + "/annotations", json={
        "scene_id": sid, "expected_content_revision": revision, "start": start, "end": start + len(selected),
        "selection_hash": content_fingerprint(selected), "body": comment, "operation_id": str(uuid4()),
    }))


async def _reply(client, url, tid, comment):
    return successful(await client.post(f"{url}/annotations/{tid}/messages", json={
        "body": comment, "operation_id": str(uuid4()),
    }))


async def test_annotation_backup_moves_and_restores_review_across_accounts(app, client, make_client):
    owner, _, _, project, _, scene, base, url, viewer, editor, _ = await _content_setup(client, make_client)
    stale = await _thread(viewer, url, scene["id"], 0, "原始", "失效的意见")
    await _reply(editor, url, stale["id"], "上一稿的说明")
    revision = successful(await client.get(url))["content_revision"]
    successful(await client.patch(url, json={"content": "新稿🙂\r\n原句\n原句", "expected_content_revision": revision}))
    resolved = await _thread(viewer, url, scene["id"], 5, "原句", "处理过的意见")
    successful(await client.post(f"{url}/annotations/{resolved['id']}/resolve"))
    current = await _thread(viewer, url, scene["id"], 8, "原句", "第二处的疑问")
    await _reply(editor, url, current["id"], "第一个回复")
    await _reply(client, url, current["id"], "第二个回复")

    target = successful(await client.post(base + "/chapters", json={"title": "第二章", "content": "引言"}))
    move = f"{url}/scenes/{scene['id']}/move"
    position = {"target_chapter_id": target["id"], "insert_before_scene_id": None}
    preview = successful(await client.post(move + "-preview", json=position))
    successful(await client.post(move, json={**position, "signature": preview["signature"]}))

    backup = await _backup(client, project["id"], versions=0)
    default_backup = await _backup(client, project["id"])
    assert all(default_backup["tables"][key] == backup["tables"][key] for key in ANNOTATION_TABLES)
    threads, messages = (backup["tables"][key] for key in ANNOTATION_TABLES)
    assert (len(threads), len(messages)) == (3, 6)
    assert all(row["chapter_id"] == target["id"] for row in threads)
    assert not any({"author_id", "operation_id"}.intersection(row) for row in messages)
    expected = [(m["body"], m["author_name"], m["created_at"]) for m in messages]
    # 文件内乱序和同名本地账号都不能替新书中的历史发言重新分配身份。
    messages.reverse()
    for msg in messages:
        msg.update(author_id=owner["id"], operation_id=str(uuid4()))
    restored = await _import(editor, backup)
    copied = restored["project"]["id"]
    assert [restored["preview"]["counts"][key] for key in ANNOTATION_TABLES] == [3, 6]
    recovered = (await _backup(editor, copied, versions=0))["tables"]
    new_cid = next(c["id"] for c in recovered["chapters"] if c["title"] == "第二章")
    new_sid = next(s["id"] for s in recovered["scenes"] if s["content"] == "新稿🙂\r\n原句\n原句")
    assert new_sid != scene["id"] and new_cid != target["id"]
    for old, new in zip(threads, recovered["annotation_threads"], strict=True):
        assert new["chapter_id"] == new_cid and new["scene_id"] == new_sid
        assert all(new[k] == v for k, v in old.items() if k not in {"id", "chapter_id", "scene_id"})
    assert [(m["body"], m["author_name"], m["created_at"]) for m in recovered["annotation_messages"]] == expected
    async with app.state.sessionmaker() as db:
        copies = (await db.scalars(select(AnnotationMessage).join(AnnotationThread)
                  .where(AnnotationThread.project_id == copied))).all()
        assert all((m.author_id, m.operation_id) == (None, None) for m in copies)
    new_url = f"{API}/{copied}/chapters/{new_cid}"
    listing = successful(await editor.get(new_url + "/annotations"))["items"]
    by_comment = {item["first_message"]["body"]: item for item in listing}
    assert by_comment["失效的意见"]["anchor_valid"] is False
    assert by_comment["处理过的意见"]["resolved"] is True
    active = by_comment["第二处的疑问"]
    assert (active["start"], active["end"], active["resolved"]) == (8, 10, False)
    await _reply(editor, new_url, active["id"], "副本回复")
    assert successful(await editor.post(f"{new_url}/annotations/{active['id']}/resolve"))["resolved"]
    assert (await viewer.get(new_url + "/annotations")).status_code == 404


async def test_annotation_backup_detects_new_reply_and_resolution(app, client):
    project, _, url, scene = await setup_scene(client, "原句")
    thread = await _thread(client, url, scene["id"], 0, "原句", "问题")
    for action in ("reply", "resolve"):
        async with app.state.sessionmaker() as db:
            owner = (await db.get(Project, project["id"])).user_id
            plan = await exporter.prepare_export(db, owner, project["id"], BackupOptions())
        if action == "reply":
            await _reply(client, url, thread["id"], "答复")
        else:
            successful(await client.post(f"{url}/annotations/{thread['id']}/resolve"))
        async with app.state.sessionmaker() as db:
            with pytest.raises(ConflictError, match="协作批注已变化"):
                await exporter.validate_export(db, plan)

    # 旧写入路径漏记位置失效时，备份同样保留可恢复的原句与讨论。
    async with app.state.sessionmaker() as db:
        (await db.get(AnnotationThread, thread["id"])).scene_hash = "f" * 64
        await db.commit()
    backup = await _backup(client, project["id"])
    record, = backup["tables"]["annotation_threads"]
    assert record["anchor_valid"] is False and record["excerpt"] == "原句"
    assert len(importer.parse_backup(json.dumps(backup).encode()).tables["annotation_messages"]) == 2


async def test_annotation_backup_checks_versions_and_graph(client):
    project, _, url, scene = await setup_scene(client, "原句")
    await _thread(client, url, scene["id"], 0, "原句", "问题")
    data = await _backup(client, project["id"])

    def parse(value):
        return importer.parse_backup(json.dumps(value).encode())

    for field, value in (("selection_hash", "f" * 64), ("scene_hash", "a" * 64), ("excerpt", "伪造"),
                         ("chapter_id", 2_000_000), ("end_offset", 1), ("resolved", None)):
        invalid = deepcopy(data)
        invalid["tables"]["annotation_threads"][0][field] = value
        with pytest.raises(BadRequestError):
            parse(invalid)
    for char in ("\x00", "\ud800"):
        for table, field in (("annotation_threads", "excerpt"), ("annotation_messages", "author_name"),
                             ("annotation_messages", "body")):
            invalid = deepcopy(data)
            invalid["tables"]["annotation_threads"][0]["anchor_valid"] = False
            invalid["tables"][table][0][field] = "甲" + char
            with pytest.raises(BadRequestError):
                parse(invalid)
    invalid = deepcopy(data)
    invalid["tables"]["annotation_messages"][0]["thread_id"] += 1
    with pytest.raises(BadRequestError):
        parse(invalid)
    invalid["tables"]["annotation_messages"] = []
    with pytest.raises(BadRequestError):
        parse(invalid)
    for name in ANNOTATION_TABLES:
        with pytest.raises(BadRequestError):
            parse({**data, "tables": {k: v for k, v in data["tables"].items() if k != name}})
    old = {**data, "version": 16}
    with pytest.raises(BadRequestError, match="旧版备份不支持协作批注"):
        parse(old)
    for name in ANNOTATION_TABLES:
        del old["tables"][name]
    copied = (await _import(client, old))["project"]["id"]
    recovered = await _backup(client, copied)
    assert all(recovered["tables"][name] == [] for name in ANNOTATION_TABLES)
