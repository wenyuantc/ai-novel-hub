"""协作批注的作者权限、幂等、定位校验和迁移保护。"""

from hashlib import sha256
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select

from app.models import AnnotationMessage, AnnotationThread, Base, Chapter, Project, Scene, User
from tests.test_collaboration_content_access import _content_setup
from tests.test_consistency_migration import _engine
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scenes import setup_scene
from tests.test_setting_fields_migration import _config, _schema_contract


def _selection(scene_id, revision, content, start, end):
    return {
        "scene_id": scene_id, "expected_content_revision": revision, "start": start, "end": end,
        "selection_hash": sha256(content[start:end].encode()).hexdigest(),
        "body": "请核对这句话", "operation_id": str(uuid4()),
    }


async def test_discussion_roles_retries_and_anonymous_history(client, make_client, system_admin):
    (_, who, _, _, _, scene, base, url, viewer, editor, stranger) = await _content_setup(client, make_client)
    path = f"{url}/annotations"
    chapter = (await client.get(url)).json()
    versions = (await client.get(f"{url}/versions")).json()
    body = _selection(scene["id"], chapter["content_revision"], chapter["content"], 1, 3)
    for blocked in (system_admin, stranger):
        assert (await blocked.get(path)).status_code == 404
        assert (await blocked.post(path, json=body)).status_code == 404
    assert (await client.post(f"{base}/collaborators", json={
        "username": "content-stranger", "role": "viewer",
    })).status_code == 201
    assert (await stranger.get(path)).status_code == 404

    response = await viewer.post(path, json=body)
    assert response.status_code == 201, response.text
    thread = response.json()
    tid = thread["id"]
    assert thread["first_message"]["author_name"] == who["username"] and thread["anchor_valid"]
    assert thread["excerpt"] == "始正" and (thread["start"], thread["end"]) == (1, 3)
    assert (await viewer.post(path, json=body)).json() == thread
    assert (await viewer.post(path, json={**body, "body": "尝试替换"})).status_code == 409
    assert (await editor.get(path)).json() == {"total": 1, "items": [thread]}
    messages = f"{path}/{tid}/messages"
    reply = {"operation_id": str(uuid4()), "body": "已经核对"}
    posted = await editor.post(messages, json=reply)
    assert posted.status_code == 201, posted.text
    assert (await editor.post(messages, json=reply)).json() == posted.json()
    assert (await editor.post(messages, json={**reply, "body": "不同的留言"})).status_code == 409
    assert (await viewer.post(messages, json={k: body[k] for k in ("body", "operation_id")})).status_code == 409
    page = (await viewer.get(messages, params={"offset": 1, "limit": 1})).json()
    assert page == {"total": 2, "items": [posted.json()]}
    for actor in (viewer, editor):
        assert (await actor.post(f"{path}/{tid}/resolve")).status_code == 403
    for _ in range(2):
        assert (await client.post(f"{path}/{tid}/resolve")).json()["resolved"]
    assert (await client.get(url)).json() == chapter
    assert (await client.get(f"{url}/versions")).json() == versions
    revoked = await client.delete(f"{base}/collaborators/{who['id']}", params={"expected_revision": 2})
    assert revoked.status_code == 204, revoked.text
    assert (await viewer.get(messages)).status_code == (await viewer.post(messages, json=reply)).status_code == 404
    assert (await viewer.get(path)).status_code == (await viewer.post(f"{path}/{tid}/resolve")).status_code == 404
    assert (await editor.get(messages)).json()["items"][0]["author_name"] == who["username"]
    assert (await system_admin.delete(f"/api/v1/admin/users/{who['id']}")).status_code == 204
    assert (await editor.get(messages)).json()["items"][0]["author_name"] == "已注销用户"


async def test_range_validation_and_chapter_cascade(app, client):
    text = "甲🙂\r\n乙🙂"
    project, chapter, url, scene = await setup_scene(client, text)
    document = f"{url}/scenes/{scene['id']}/document"
    payload = save_payload((await client.get(document)).json())
    add_section(payload)
    saved = await client.put(document, json=payload)
    assert saved.status_code == 200, saved.text
    rev = saved.json()["content_revision"]
    path = f"{url}/annotations"
    for start, end in [(0, 2), (2, 3), (2, 4), (4, 4), (4, 7)]:
        assert (await client.post(path, json=_selection(scene["id"], rev, text, start, end))).status_code == 422
    body = _selection(scene["id"], rev, text, 4, 6)
    for msg in ("  ", "x" * 4001, "含\0空字符"):
        assert (await client.post(path, json={**body, "body": msg})).status_code == 422
    for conflict in ({"expected_content_revision": rev - 1}, {"selection_hash": "0" * 64}):
        assert (await client.post(path, json={**body, **conflict})).status_code == 409
    assert (await client.get(path)).json() == {"items": [], "total": 0}
    created = await client.post(path, json=body)
    assert created.status_code == 201 and created.json()["excerpt"] == "乙🙂"
    other = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "另一章"})
    other_url = f"/api/v1/projects/{project['id']}/chapters/{other.json()['id']}/annotations"
    assert (await client.get(f"{other_url}/{created.json()['id']}/messages")).status_code == 404
    assert (await client.post(other_url, json=body | {"operation_id": str(uuid4()), "expected_content_revision": 0})
            ).status_code == 404
    deleted = await client.delete(url)
    assert deleted.status_code == 204, deleted.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AnnotationThread.id)) is None
        assert await db.scalar(select(AnnotationMessage.id)) is None


def test_migration_freezes_schema_and_guards_downgrade(tmp_path):
    path = tmp_path / "annotation.db"
    cfg = _config(path)
    command.upgrade(cfg, "0073")
    current, reference = _engine(path), create_engine("sqlite://")
    try:
        with current.begin() as db:
            for model, values in ((User, {"username": "旧作者", "password_hash": "-"}),
                                  (Project, {"title": "旧书", "user_id": 1}),
                                  (Chapter, {"title": "旧章", "project_id": 1}),
                                  (Scene, {"chapter_id": 1, "content": "原句"})):
                db.execute(model.__table__.insert().values(id=1, **values))
        command.upgrade(cfg, "0074")
        Base.metadata.create_all(reference)
        for table in ("annotation_threads", "annotation_messages"):
            assert _schema_contract(inspect(current), table) == _schema_contract(inspect(reference), table)
        with current.begin() as db:
            assert db.scalar(select(Scene.content)) == "原句" and db.scalar(select(AnnotationThread.id)) is None
            db.execute(AnnotationThread.__table__.insert().values(
                project_id=1, chapter_id=1, scene_id=1, original_start=0, original_end=2, start_offset=0, end_offset=2,
                excerpt="原句", selection_hash="a"*64, scene_hash="a"*64,
            ))
        with pytest.raises(RuntimeError, match="存在协作批注"):
            command.downgrade(cfg, "0073")
        with current.begin() as db:
            assert db.scalar(select(AnnotationThread.excerpt)) == "原句"
            db.execute(AnnotationThread.__table__.delete())
        command.downgrade(cfg, "0073")
        assert "annotation_threads" not in inspect(current).get_table_names()
    finally:
        current.dispose()
        reference.dispose()
