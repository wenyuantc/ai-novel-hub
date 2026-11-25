"""以真实 PostgreSQL 行锁验证回复、撤权及正文修改交错时的批注结果。"""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import select, text

from app.models import AnnotationMessage, AnnotationThread, Chapter, Project, Scene, User
from tests.schema_helpers import isolated_worker_database
from tests.test_annotation_anchors import (
    test_ai_rewrite_shifts_anchor_then_history_restore_invalidates_only_changed_scene,
    test_batch_revision_undo_cross_chapter_move_and_scene_retirement,
    test_known_steps_shift_second_occurrence_and_unknown_overwrite_stays_invalid_after_aba,
    test_scene_save_failure_rolls_back_proven_shift,
    test_section_insert_and_bad_replay_preserve_discussion_without_jump,
    test_split_preserves_only_ranges_that_stay_in_original_scene,
)
from tests.test_annotation_native_backup import selection
from tests.test_annotation_transfer import (
    test_annotation_backup_checks_versions_and_graph,
    test_annotation_backup_detects_new_reply_and_resolution,
    test_annotation_backup_moves_and_restores_review_across_accounts,
)
from tests.test_collaboration_content_access import _content_setup
from tests.test_task_worker_migration import migration_db as migration_db

__all__ = [
    "test_ai_rewrite_shifts_anchor_then_history_restore_invalidates_only_changed_scene",
    "test_batch_revision_undo_cross_chapter_move_and_scene_retirement",
    "test_known_steps_shift_second_occurrence_and_unknown_overwrite_stays_invalid_after_aba",
    "test_scene_save_failure_rolls_back_proven_shift",
    "test_section_insert_and_bad_replay_preserve_discussion_without_jump",
    "test_split_preserves_only_ranges_that_stay_in_original_scene",
    "test_annotation_backup_checks_versions_and_graph",
    "test_annotation_backup_detects_new_reply_and_resolution",
    "test_annotation_backup_moves_and_restores_review_across_accounts",
]


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch, required=True) as overrides:
        yield overrides


async def _race(*requests):
    barrier = asyncio.Barrier(len(requests))

    async def start(request):
        await barrier.wait()
        return await request

    return await asyncio.wait_for(asyncio.gather(*(start(request) for request in requests)), timeout=30)


async def test_duplicate_reply_then_revoke_racing_with_new_reply(client, make_client):
    _, viewer_user, _, _, _, scene, base, chapter, viewer, _, _ = await _content_setup(client, make_client)
    path = f"{chapter}/annotations"
    revision = (await viewer.get(f"{chapter}/scenes")).json()["content_revision"]
    thread = await viewer.post(path, json=selection(scene["id"], revision, "原始正文", "正文"))
    assert thread.is_success, thread.text
    messages = f"{path}/{thread.json()['id']}/messages"
    reply = {"body": "并行重复提交", "operation_id": str(uuid4())}
    first, retry = await _race(viewer.post(messages, json=reply), viewer.post(messages, json=reply))
    assert first.is_success and retry.is_success
    assert first.json() == retry.json()
    members = f"{base}/collaborators"
    viewer_revision = next(row["revision"] for row in (await client.get(members)).json()
                           if row["user_id"] == viewer_user["id"])
    racing = {"body": "撤权交错的回复", "operation_id": str(uuid4())}
    revoked, posted = await _race(
        client.delete(f"{members}/{viewer_user['id']}", params={"expected_revision": viewer_revision}),
        viewer.post(messages, json=racing),
    )
    assert revoked.status_code == 204, revoked.text
    assert posted.status_code == 404 or posted.is_success, posted.text
    assert (await viewer.post(messages, json=racing)).status_code == 404
    assert (await viewer.get(path)).status_code == 404
    rows = (await client.get(messages)).json()["items"]
    bodies = [row["body"] for row in rows]
    assert bodies.count(reply["body"]) == 1
    assert bodies.count(racing["body"]) == int(posted.is_success)


async def test_annotation_creation_racing_with_replaced_scene(client, make_client):
    _, _, _, _, _, scene, _, chapter, viewer, editor, _ = await _content_setup(client, make_client)
    revision = (await viewer.get(f"{chapter}/scenes")).json()["content_revision"]
    path = f"{chapter}/annotations"
    payload = selection(scene["id"], revision, "原始正文", "正文")
    created, saved = await _race(viewer.post(path, json=payload), editor.patch(f"{chapter}/scenes/{scene['id']}", json={
        "expected_content_revision": revision, "content": "改过的另一稿",
    }))
    assert saved.is_success, saved.text
    assert created.status_code == 409 or created.is_success, created.text
    rows = (await client.get(path)).json()["items"]
    assert len(rows) == int(created.is_success)
    if rows:
        assert rows[0]["excerpt"] == "正文" and not rows[0]["anchor_valid"]
    assert (await viewer.post(path, json={**payload, "operation_id": str(uuid4())})).status_code == 409


@pytest.mark.parametrize("migration_db", ["postgresql"], indirect=True)
async def test_postgres_upgrade_and_nonempty_downgrade_guard(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0073")
    async with engine.begin() as db:
        for model, values in (
            (User, {"username": "旧用户", "password_hash": "-"}),
            (Project, {"user_id": 1, "title": "旧书"}),
            (Chapter, {"project_id": 1, "title": "旧章"}),
            (Scene, {"chapter_id": 1, "content": "旧稿"}),
        ):
            await db.execute(model.__table__.insert().values(id=1, **values))
    await asyncio.to_thread(command.upgrade, cfg, "0074")
    async with engine.begin() as db:
        assert await db.scalar(select(Scene.content)) == "旧稿"
        assert await db.scalar(select(AnnotationThread.id)) is None
        await db.execute(AnnotationThread.__table__.insert().values(
            id=1, project_id=1, chapter_id=1, scene_id=1, original_start=0, original_end=2,
            start_offset=0, end_offset=2, excerpt="旧稿", selection_hash="a" * 64, scene_hash="a" * 64,
        ))
        await db.execute(AnnotationMessage.__table__.insert().values(
            thread_id=1, author_id=1, author_name="旧用户", body="保留意见",
        ))
    with pytest.raises(RuntimeError, match="存在协作批注"):
        await asyncio.to_thread(command.downgrade, cfg, "0073")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0074"
        assert await db.scalar(select(AnnotationThread.excerpt)) == "旧稿"
        assert await db.scalar(select(AnnotationMessage.body)) == "保留意见"
        await db.execute(AnnotationThread.__table__.delete())
    await asyncio.to_thread(command.downgrade, cfg, "0073")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0073"
        assert await db.scalar(text("SELECT to_regclass('annotation_threads')")) is None
        assert await db.scalar(text("SELECT to_regclass('annotation_messages')")) is None
        assert await db.scalar(select(Scene.content)) == "旧稿"
