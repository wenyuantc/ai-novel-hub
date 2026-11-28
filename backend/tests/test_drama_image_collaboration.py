"""编辑者在造型生图入口沿用授权的图片模型，撤权后的结果不能发布。"""

from sqlalchemy import select

from app.models import AiUsageCall, Project
from app.plugins.drama.models import DramaSeries
from tests.helpers import Gate, create_ai_config, wait_task
from tests.test_drama_image_task import picture, start
from tests.test_drama_image_task import target as target
from tests.test_drama_task_guards import grant, invite_editor, terminal_row


async def test_editor_image_model_billing_and_revocation(app, client, make_client, target, fake_llm):
    series_id, look_id = target
    async with app.state.sessionmaker() as db:
        project = await db.scalar(select(Project).join(DramaSeries).where(DramaSeries.id == series_id))
    editor, user = await invite_editor(client, make_client, project.id, allow_owner_ai=False)
    history_url = f"/api/v1/plugins/drama/assets/look/{look_id}/images"

    denied = await start(editor, target)
    assert 400 <= denied.status_code < 500 and not fake_llm.image_calls
    assert (await client.get(history_url)).json() == []
    await grant(client, app, project.id, user["id"], True)

    async def generate(model, payer_id):
        fake_llm.images.append(picture())
        submitted = await start(editor, target)
        assert submitted.status_code == 202, submitted.text
        task = await wait_task(editor, submitted.json(), within=20)
        assert task["status"] == "succeeded", task
        assert fake_llm.image_calls[-1]["model"] == model
        async with app.state.sessionmaker() as db:
            usage = (await db.scalars(select(AiUsageCall).where(AiUsageCall.task_id == task["id"]))).one()
            assert (usage.kind, usage.model, usage.actor_id, usage.payer_id, usage.image_count) == (
                "drama.image", model, user["id"], payer_id, 1
            )

    await generate("image-test", project.user_id)
    gate = Gate()
    fake_llm.image_after_gates.append(gate)
    fake_llm.images.append(picture())
    running = await start(editor, target)
    assert running.status_code == 202, running.text
    try:
        await gate.wait_reached(20)
        await grant(client, app, project.id, user["id"], False)
    finally:
        gate.release()
    stopped = await terminal_row(app, running.json())
    assert stopped.status in {"cancelled", "failed"} and stopped.result is None
    history = (await client.get(history_url)).json()
    assert len(history) == 2
    assert len([row for row in history if row["status"] == "ready" and row["media_object_id"]]) == 1
    assert len([row for row in history if row["status"] == "failed" and row["media_object_id"] is None]) == 1
    calls = len(fake_llm.image_calls)
    denied = await start(editor, target)
    assert 400 <= denied.status_code < 500 and len(fake_llm.image_calls) == calls

    await create_ai_config(editor, model_type="image", model="editor-image")
    await generate("editor-image", user["id"])
