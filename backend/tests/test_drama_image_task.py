"""通过真正的短剧任务受理入口验证图片模型、历史与取消。"""

from io import BytesIO

import pytest
from PIL import Image

from app.ai.providers.base import GeneratedImage, ImageCapabilityError
from app.plugins.drama.models import DramaCharacter, DramaLook, DramaSeries
from tests.helpers import Gate, create_ai_config, create_project, register, task_params, wait_task


@pytest.fixture
async def target(app, client, system_admin):
    await register(client, "image-task-author")
    await create_ai_config(client, name="图片配置", model="image-test", model_type="image")
    project = await create_project(client)
    switched = await system_admin.patch("/api/v1/admin/plugins/drama", json={"enabled": True, "revision": 0})
    assert switched.is_success
    async with app.state.sessionmaker() as db:
        series = DramaSeries(project_id=project["id"], title="夜山")
        db.add(series)
        await db.flush()
        char = DramaCharacter(series_id=series.id, name="小溪", appearance="银发")
        db.add(char)
        await db.flush()
        look = DramaLook(character_id=char.id, name="雨衣", styling="红披风", is_default=True)
        db.add(look)
        await db.commit()
        return series.id, look.id


def picture():
    output = BytesIO()
    Image.new("RGB", (160, 90), "gold").save(output, "PNG")
    return GeneratedImage(output.getvalue(), "image/png")


async def start(client, target):
    sid, look = target
    return await client.post(
        f"/api/v1/plugins/drama/series/{sid}/tasks",
        json={"kind": "drama.image", "targets": [{"type": "look", "id": look}]},
    )


async def test_failed_generation_leaves_history_then_retry_uses_image_model(client, target, fake_llm):
    fake_llm.images.append(ImageCapabilityError("本模型不支持参考图"))
    first = await start(client, target)
    assert first.status_code == 202, first.text
    failed = await wait_task(client, first.json(), within=20)
    assert failed["status"] == "failed" and "不支持参考图" in failed["error"], failed
    fake_llm.images.append(picture())
    retry = await client.post(f"/api/v1/tasks/{failed['id']}/retry", params=task_params(failed))
    assert retry.status_code == 202, retry.text
    success = await wait_task(client, retry.json(), within=20)
    assert success["status"] == "succeeded", success
    apply = await client.post(
        f"/api/v1/plugins/drama/series/{target[0]}/tasks/{success['id']}/apply",
        json={"task_key": success["task_key"]},
    )
    assert apply.status_code == 404, apply.text
    history = await client.get(f"/api/v1/plugins/drama/assets/look/{target[1]}/images")
    assert history.is_success, history.text
    rows = history.json()
    assert [(r["status"], r["is_selected"]) for r in rows] == [("ready", True), ("failed", False)]
    assert rows[1]["media_object_id"] is None and "不支持参考图" in rows[1]["error_message"]
    assert len(fake_llm.image_calls) == 2
    assert "红披风" in fake_llm.image_calls[-1]["prompt"]


async def test_concurrent_generation_and_cancellation(app, client, target, fake_llm):
    from sqlalchemy import select

    from app.plugins.drama.models import DramaAssetImage

    gate = Gate()
    fake_llm.image_after_gates.append(gate)
    fake_llm.images.append(picture())
    first = await start(client, target)
    assert first.status_code == 202, first.text
    try:
        await gate.wait_reached(15)
        duplicate = await start(client, target)
        assert duplicate.status_code == 409, duplicate.text
        cancelled = await client.post(f"/api/v1/tasks/{first.json()['id']}/cancel", params=task_params(first.json()))
        assert cancelled.is_success
    finally:
        gate.release()
    assert (await wait_task(client, first.json(), within=20))["status"] == "cancelled"
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(DramaAssetImage)))
        assert len(rows) == 1 and rows[0].status == "failed" and not rows[0].media_object_id


async def test_image_request_obeys_novel_visibility_and_series_scope(app, client, target, fake_llm):
    from app.models import Character

    sid, lid = target
    async with app.state.sessionmaker() as db:
        series = await db.get(DramaSeries, sid)
        look = await db.get(DramaLook, lid)
        character = await db.get(DramaCharacter, look.character_id)
        private = Character(
            project_id=series.project_id,
            name=character.name,
            appearance="隐秘识别记号",
            ai_mode="always",
            ai_hidden_builtin_fields=["appearance"],
        )
        elsewhere = DramaSeries(project_id=series.project_id, title="其他剧")
        db.add_all([private, elsewhere])
        await db.flush()
        character.novel_character_id = private.id
        character.appearance = private.appearance
        await db.commit()
        other_id = elsewhere.id
    rejected = await start(client, (other_id, lid))
    assert rejected.status_code in (400, 404) and not fake_llm.image_calls
    fake_llm.images.append(picture())
    accepted = await start(client, target)
    assert accepted.status_code == 202, accepted.text
    assert (await wait_task(client, accepted.json(), within=20))["status"] == "succeeded"
    prompt = fake_llm.image_calls[0]["prompt"]
    assert "红披风" in prompt and "隐秘识别记号" not in prompt


async def test_publication_failure_releases_media_and_image_history(app, client, target, fake_llm, monkeypatch):
    from sqlalchemy import select

    from app.core.errors import ConflictError
    from app.models.media import MediaObject
    from app.plugins.drama.services import images

    async def lose_asset(*args, **kwargs):
        raise ConflictError("造型在图片响应后已删除")

    monkeypatch.setattr(images, "add_generated", lose_asset)
    fake_llm.images.append(picture())
    accepted = await start(client, target)
    assert accepted.status_code == 202, accepted.text
    final = await wait_task(client, accepted.json(), within=20)
    assert final["status"] == "failed", final
    history = (await client.get(f"/api/v1/plugins/drama/assets/look/{target[1]}/images")).json()
    assert len(history) == 1 and history[0]["status"] == "failed" and history[0]["media_object_id"] is None
    async with app.state.sessionmaker() as db:
        states = list(await db.scalars(select(MediaObject.status).where(MediaObject.plugin_id == "drama")))
        # 后台清理可能已回收；仍存在的原图和缩略图必须已经进入删除队列。
        assert all(status == "deleting" for status in states)


async def test_cancel_before_claim_releases_image_slot(client, target, paused_task_dispatch, fake_llm):
    accepted = await start(client, target)
    assert accepted.status_code == 202, accepted.text
    task = accepted.json()
    cancelled = await client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task))
    assert cancelled.is_success, cancelled.text
    history = await client.get(f"/api/v1/plugins/drama/assets/look/{target[1]}/images")
    row, = history.json()
    assert row["status"] == "failed" and row["media_object_id"] is None
    assert not fake_llm.image_calls
    assert (await client.delete(f"/api/v1/plugins/drama/asset-images/{row['id']}")).is_success


async def test_publication_ack_failure_reclaims_original_and_thumbnail(app, client, target, fake_llm, monkeypatch):
    from sqlalchemy import or_, select

    from app.core.errors import ConflictError
    from app.media import service
    from app.models.media import MediaObject

    original_store = service.store
    published = []

    async def lose_ack(db, **kwargs):
        stored = await original_store(db, **kwargs)
        children = list(await db.scalars(select(MediaObject.id).where(MediaObject.parent_id == stored.id)))
        assert len(children) == 1
        published.append(stored.id)
        raise ConflictError("测试：媒体已保存，发布确认丢失")

    monkeypatch.setattr(service, "store", lose_ack)
    fake_llm.images.append(picture())
    accepted = await start(client, target)
    assert accepted.status_code == 202, accepted.text
    assert (await wait_task(client, accepted.json(), within=20))["status"] == "failed"
    object_id, = published
    history = await client.get(f"/api/v1/plugins/drama/assets/look/{target[1]}/images")
    row, = history.json()
    assert row["status"] == "failed" and row["media_object_id"] is None and not row["is_selected"]
    async with app.state.sessionmaker() as db:
        pending = (await db.scalars(select(MediaObject).where(
            or_(MediaObject.id == object_id, MediaObject.parent_id == object_id),
        ))).all()
        # 维护进程可能已完成物理回收，剩余的行必须都已标记待删除。
        assert all(obj.deleted_at is not None for obj in pending)
    assert (await client.get(f"/api/v1/media/{object_id}")).status_code == 404
