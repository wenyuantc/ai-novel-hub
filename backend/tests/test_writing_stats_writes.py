"""以真实正文保存和整理入口核对个人每日累计。"""

import asyncio

from sqlalchemy import func, select

from app.models.writing_stats import WritingDailyStat
from tests.helpers import register
from tests.test_batch_revision import application, apply_batch, preview, undo_url
from tests.test_collaboration_story_access import _invite
from tests.test_rich_document_api import KEY, add_section, save_payload
from tests.test_scene_move import preview as move_preview
from tests.test_scenes import setup_scene


async def assert_total(app, project, manual):
    async with app.state.sessionmaker() as db:
        totals = (await db.execute(select(
            func.sum(WritingDailyStat.manual_words), func.sum(WritingDailyStat.ai_words),
        ).where(WritingDailyStat.project_id == project["id"]))).one()
    assert tuple(totals) == (manual, 0)


async def test_handwritten_chapter_scene_and_version_conflict(app, client):
    project, _, url, first = await setup_scene(client, "甲" * 100)
    await assert_total(app, project, 100)
    rewritten = await client.patch(url, json={"expected_content_revision": 0, "content": "乙" * 100})
    assert rewritten.is_success, rewritten.text
    await assert_total(app, project, 200)
    scene_url = f"{url}/scenes/{first['id']}"
    body = {"expected_content_revision": 1, "content": "乙" * 80}
    assert (await client.patch(scene_url, json=body)).is_success
    assert (await client.patch(scene_url, json=body)).status_code == 409
    created = await client.post(f"{url}/scenes", json={
        "expected_content_revision": 2, "content": "海 边", "summary": "作者的构思",
    })
    assert created.is_success, created.text
    saved = await client.patch(scene_url, json={
        "expected_content_revision": 3, "content": "云" + "乙" * 79 + "海", "title": "初稿",
    })
    assert saved.is_success, saved.text
    await assert_total(app, project, 204)
    assert (await client.delete(url)).is_success
    await assert_total(app, project, 204)


async def test_document_autosave_retry_private_section_and_conversion(app, client):
    project, _, url, first = await setup_scene(client, "海边")
    endpoint = f"{url}/scenes/{first['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["content"] = "在海边。"
    saved = await client.put(endpoint, json=body)
    assert saved.is_success, saved.text
    assert (await client.put(endpoint, json=body)).status_code == 409
    unchanged = save_payload(saved.json())
    assert (await client.put(endpoint, json=unchanged)).is_success
    await assert_total(app, project, 4)
    body = save_payload(saved.json())
    add_section(body)
    body["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 0, "end": 1}]
    private = await client.put(endpoint, json=body)
    assert private.is_success, private.text
    converted = await client.post(f"{endpoint}/sections/{KEY}/convert", json={
        "expected_content_revision": private.json()["content_revision"],
    })
    assert converted.is_success, converted.text
    assert "作者🙂" in converted.json()["document"]["content"]
    await assert_total(app, project, 4)


async def test_organizing_existing_text_preserves_writing_total(app, client):
    project, _, url, first = await setup_scene(client, "青石镇的春天")
    base = f"/api/v1/projects/{project['id']}"
    batch = await apply_batch(client, base, application((await preview(client, base))["items"]))
    assert (await client.post(undo_url(base, batch))).is_success
    split = await client.post(f"{url}/scenes/{first['id']}/split", json={
        "expected_content_revision": 2, "offset": 3,
    })
    assert split.is_success, split.text
    split_version = (await client.get(f"{url}/versions")).json()[0]
    scene_ids = [s["id"] for s in split.json()["items"]]
    merge = (await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 3, "scene_ids": scene_ids,
    })).json()
    merged = await client.post(f"{url}/scenes/merge", json={
        "expected_content_revision": 3, "scene_ids": scene_ids, "metadata_source": "left",
        "expected_planning_revision": merge["planning_revision"], "signature": merge["signature"],
    })
    assert merged.is_success, merged.text
    restored = await client.post(f"{url}/versions/{split_version['id']}/restore", json={
        "expected_content_revision": 4,
    })
    assert restored.is_success, restored.text
    target = (await client.post(f"{base}/chapters", json={"title": "目的章"})).json()
    _, move = await move_preview(client, f"{url}/scenes", scene_ids[1], target)
    moved = await client.post(f"{url}/scenes/{scene_ids[1]}/move", json=move)
    assert moved.is_success, moved.text
    await assert_total(app, project, 6)


async def test_concurrent_editor_saves_count_only_committed_body_for_actor(app, client, make_client):
    project, _, first, _ = await setup_scene(client, "作者")
    chapters = f"/api/v1/projects/{project['id']}/chapters"
    other = (await client.post(chapters, json={"title": "另一章"})).json()
    second = f"{chapters}/{other['id']}"
    editor = make_client()
    actor = await register(editor, "writing-editor")
    await _invite(client, editor, project["id"], "writing-editor", "editor")

    saved = await asyncio.gather(
        editor.patch(first, json={"content": "作者" + "甲" * 40, "expected_content_revision": 0}),
        editor.patch(second, json={"content": "乙" * 60, "expected_content_revision": 0}),
    )
    assert [r.status_code for r in saved] == [200, 200]
    competitors = await asyncio.gather(*(
        editor.patch(second, json={"content": "乙" * 60 + word, "expected_content_revision": 1})
        for word in ("春", "秋")
    ))
    assert sorted(r.status_code for r in competitors) == [200, 409]
    async with app.state.sessionmaker() as db:
        rows = (await db.execute(select(
            WritingDailyStat.user_id, func.sum(WritingDailyStat.manual_words),
        ).where(WritingDailyStat.project_id == project["id"]).group_by(WritingDailyStat.user_id))).all()
    assert dict(rows) == {project["owner"]["id"]: 2, actor["id"]: 101}
