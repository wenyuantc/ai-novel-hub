"""由真实 AI 入口提交正文，按最终变动及实际操作者累计。"""

from sqlalchemy import func, select

from app.models.writing_stats import WritingDailyStat
from tests.helpers import create_ai_config, parse_sse, run_task
from tests.test_collaboration_batch_entry_e4g2 import _start, _terminal, _world
from tests.test_rewrite import _setup
from tests.test_rich_document_api import KEY
from tests.test_rich_document_proposals import _pending
from tests.test_scene_planning import scene_setup


async def _totals(app, project_id, user_id=None):
    async with app.state.sessionmaker() as db:
        query = select(
            func.coalesce(func.sum(WritingDailyStat.manual_words), 0),
            func.coalesce(func.sum(WritingDailyStat.ai_words), 0),
        ).where(WritingDailyStat.project_id == project_id)
        if user_id is not None:
            query = query.where(WritingDailyStat.user_id == user_id)
        return (await db.execute(query)).one()


async def test_partial_rewrite_records_only_accepted_text_and_ignores_legacy_apply(app, client, fake_llm):
    chapter, url, rewrite = await _setup(client, "甲")
    project_id = chapter["project_id"]
    manual, ai = await _totals(app, project_id)
    assert ai == 0
    fake_llm.push("乙" * 5000)
    preview = await client.post(rewrite, json={"op": "rewrite", "start": 0, "end": 1})
    event, done = parse_sse(preview.text)[-1]
    assert event == "done" and len(done["text"]) == 5000
    assert await _totals(app, project_id) == (manual, 0)

    body = {"start": 0, "end": 1, "original": "甲", "replacement": done["text"][:2000], "log_id": done["log_id"]}
    applied = await client.post(f"{url}/rewrite/apply", json=body)
    assert applied.status_code == 200 and applied.json()["content"] == "乙" * 2000
    assert (await client.post(f"{url}/rewrite/apply", json=body)).status_code == 409
    assert await _totals(app, project_id) == (manual, 2000)

    legacy = await client.post(f"{url}/rewrite/apply", json={
        "start": 0, "end": 1, "original": "乙", "replacement": "旧客户端",
    })
    assert legacy.status_code == 200, legacy.text
    assert await _totals(app, project_id) == (manual, 2000)


async def test_whole_chapter_ai_counts_each_changed_scene(app, client, fake_llm):
    project, _, chapter, _, _ = await scene_setup(client, content="春风吹过山岗", count=3)
    await create_ai_config(client)
    manual, _ = await _totals(app, project["id"])
    for text in ("暖风吹过山坡", "第二段", "第二段有结尾"):
        fake_llm.push(text)
    task, events = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/generate",
                                                             json={"target_words": 900}))
    assert task["status"] == "succeeded", events[-1]
    assert await _totals(app, project["id"]) == (manual, 5)


async def test_review_records_only_confirmed_proposal_once(app, client, fake_llm):
    project, _, _, scene, _, _, task, identity = await _pending(app, client, fake_llm)
    manual, ai = await _totals(app, project["id"])
    assert ai == 0
    accept = f"/api/v1/tasks/{task['id']}/rich-proposals/{identity['token']}/accept"
    body = {"task_key": identity["task_key"], "run_key": identity["run_key"], "confirm_format_loss": True,
            "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}]}
    for _ in range(2):
        result = await client.post(accept, json=body)
        assert result.status_code == 200, result.text
        assert await _totals(app, project["id"]) == (manual, len("完全不同的候选"))  # 原稿、候选稿共用「稿」。


async def test_batch_using_owner_model_credits_editor(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    project_id = world.project["id"]
    before = await _totals(app, project_id, world.owner["id"])
    fake_llm.push("霜落")
    fake_llm.push("寒山")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    task = await _terminal(app, created.json()["id"])
    assert task.status == "succeeded", task.error
    assert await _totals(app, project_id, world.owner["id"]) == before
    assert await _totals(app, project_id, world.guest["id"]) == (0, 4)
