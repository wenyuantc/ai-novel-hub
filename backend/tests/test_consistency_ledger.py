"""事实台账：抽取落库、未变章复用、指纹变化不保存、AI 可见区块边界、畸形输出容错、新鲜度。"""

import asyncio
import hashlib

from sqlalchemy import select

from app.ai.providers.base import AIProviderError
from app.models import Chapter, ConsistencyLedger
from app.models.consistency import LEDGER_SCHEMA_VERSION
from app.schemas.consistency import LedgerFact
from app.services.consistency import ledger as svc
from app.services.mentions.matcher import text_hash
from tests.consistency_helpers import API, extraction, seed, target
from tests.helpers import Gate
from tests.test_rich_document_ai_context import _add_visibility_examples

BROKEN = "😀 林远的佩剑在山门前断成两截。他把断剑埋在松树下。"
RAW = {"category": "item", "subject": "佩剑", "statement": "林远的佩剑断成两截", "quote": "佩剑在山门前断成两截"}


async def _ledger(app, chapter_id: int) -> ConsistencyLedger | None:
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == chapter_id))


async def _extract(app, fake_llm, user, project, chapter, **kwargs) -> svc.ExtractResult:
    return await svc.extract_chapter(app.state.sessionmaker, target(fake_llm), user_id=user["id"],
                                     project_id=project["id"], chapter_id=chapter["id"], **kwargs)


async def test_extract_saves_ledger_with_server_side_offsets(app, client, fake_llm):
    user, project, chapters = await seed(client, {5: BROKEN})
    secret = await client.post(f"{API}/{project['id']}/characters", json={"name": "林远", "profile": "绝密设定档案"})
    assert secret.status_code == 201, secret.text
    fake_llm.push(extraction({**RAW, "start": 900, "end": 901}, {**RAW, "category": "timeline", "subject": "",
                                                                 "statement": "事发在清晨", "quote": "原文里没有这句"}))
    result = await _extract(app, fake_llm, user, project, chapters[5], task_id=None)
    assert (result.state, result.facts, result.warnings, result.error) == ("extracted", 2, (), "")
    row = await _ledger(app, chapters[5]["id"])
    assert row.id == result.ledger_id and row.project_id == project["id"]
    assert row.source_hash == hashlib.sha256(BROKEN.encode()).hexdigest() == row.text_hash == text_hash(BROKEN)
    assert row.model == "fake-model" and row.schema_version == LEDGER_SCHEMA_VERSION and row.task_id is None
    first, second = (LedgerFact.model_validate(item) for item in row.facts)
    assert (first.id, first.subject_key, first.start, first.end) == ("1", "佩剑", 5, 15)
    assert BROKEN[first.start:first.end] == first.quote
    assert (second.id, second.category, second.start, second.end) == ("2", "timeline", None, None)
    # 出站只有章节正文：不含作品信息与结构化设定
    call = fake_llm.calls[-1]
    sent = str(call["messages"])
    assert call["model"] == "fake-model" and BROKEN in sent and "第5章 章5" in sent
    assert "星海纪元" not in sent and "绝密设定档案" not in sent and "人类首次接触外星文明" not in sent


async def test_unchanged_chapter_reuses_ledger_without_calling_model(app, client, fake_llm):
    user, project, chapters = await seed(client, {1: BROKEN, 2: "林远拔出佩剑。"})
    fake_llm.push(extraction(RAW))
    fake_llm.push(extraction())
    assert (await _extract(app, fake_llm, user, project, chapters[1])).state == "extracted"
    assert (await _extract(app, fake_llm, user, project, chapters[2])).state == "extracted"
    reused = await _extract(app, fake_llm, user, project, chapters[1])
    assert (reused.state, reused.facts) == ("reused", 1) and len(fake_llm.calls) == 2
    # 只改第 2 章：仅该章重新抽取，原位覆盖同一行
    before = await _ledger(app, chapters[2]["id"])
    edited = await client.patch(f"{API}/{project['id']}/chapters/{chapters[2]['id']}",
                                json={"content": "林远收剑入鞘。"})
    assert edited.status_code == 200, edited.text
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(Chapter).where(Chapter.project_id == project["id"])
                                     .order_by(Chapter.chapter_no)))
        assert await svc.ledger_states(db, project["id"], rows) == {chapters[1]["id"]: "fresh",
                                                                   chapters[2]["id"]: "stale"}
    fake_llm.push(extraction({"category": "item", "subject": "佩剑", "statement": "收剑", "quote": "收剑入鞘"}))
    assert (await _extract(app, fake_llm, user, project, chapters[1])).state == "reused"
    again = await _extract(app, fake_llm, user, project, chapters[2])
    after = await _ledger(app, chapters[2]["id"])
    assert again.state == "extracted" and len(fake_llm.calls) == 3
    assert after.id == before.id and after.text_hash == text_hash("林远收剑入鞘。") and len(after.facts) == 1
    # 结构版本不符视为过期；force 也会重新调用模型
    async with app.state.sessionmaker() as db:
        row = await db.get(ConsistencyLedger, after.id)
        assert svc.is_fresh(row, row.source_hash) and not svc.is_fresh(None, row.source_hash)
        row.schema_version = LEDGER_SCHEMA_VERSION + 1
        assert not svc.is_fresh(row, row.source_hash)
    fake_llm.push(extraction())
    assert (await _extract(app, fake_llm, user, project, chapters[1], force=True)).state == "extracted"
    assert len(fake_llm.calls) == 4


async def test_changed_or_deleted_chapter_during_call_is_not_saved(app, client, fake_llm):
    user, project, chapters = await seed(client, {1: BROKEN, 2: "林远拔出佩剑。", 3: "第三章正文。"})
    fake_llm.push(extraction(RAW))
    assert (await _extract(app, fake_llm, user, project, chapters[1])).state == "extracted"
    old = await _ledger(app, chapters[1]["id"])
    changed = await client.patch(f"{API}/{project['id']}/chapters/{chapters[1]['id']}", json={"content": BROKEN + "甲"})
    assert changed.status_code == 200, changed.text

    async def run(chapter, mutate):
        gate = Gate()
        fake_llm.push([extraction(RAW), gate])
        task = asyncio.create_task(_extract(app, fake_llm, user, project, chapter))
        await gate.wait_reached()
        await mutate()
        gate.release()
        return await task

    async def edit():
        resp = await client.patch(f"{API}/{project['id']}/chapters/{chapters[1]['id']}", json={"content": "全新正文"})
        assert resp.status_code == 200, resp.text

    async def delete():
        resp = await client.delete(f"{API}/{project['id']}/chapters/{chapters[2]['id']}")
        assert resp.status_code == 204, resp.text

    # 检查期间正文被改：旧结果不保存，原有（已过期的）台账原样保留
    result = await run(chapters[1], edit)
    assert (result.state, result.error) == ("skipped_changed", svc.CHANGED)
    kept = await _ledger(app, chapters[1]["id"])
    assert (kept.source_hash, kept.facts, kept.updated_at) == (old.source_hash, old.facts, old.updated_at)
    # 检查期间章节被删：同样不保存
    assert (await run(chapters[2], delete)).state == "skipped_changed"
    assert await _ledger(app, chapters[2]["id"]) is None
    calls = len(fake_llm.calls)
    assert (await _extract(app, fake_llm, user, project, chapters[2])).state == "skipped_missing"
    # 其他作品的章节按不存在处理
    other = await svc.extract_chapter(app.state.sessionmaker, target(fake_llm), user_id=user["id"],
                                      project_id=project["id"] + 999, chapter_id=chapters[3]["id"])
    assert other.state == "skipped_missing" and len(fake_llm.calls) == calls


async def test_disabled_author_sections_are_not_sent(app, client, fake_llm):
    user, project, chapters = await seed(client, {1: "正式正文甲乙"})
    url = f"{API}/{project['id']}/chapters/{chapters[1]['id']}/scenes"
    scene = (await client.get(url)).json()["items"][0]
    await _add_visibility_examples(client, f"{url}/{scene['id']}/document")
    fake_llm.push(extraction(
        {"category": "character_state", "subject": "甲", "statement": "正文事实", "quote": "正式正文"},
        {"category": "item", "subject": "区块", "statement": "区块事实", "quote": "开启区块内容"},
    ))
    result = await _extract(app, fake_llm, user, project, chapters[1])
    assert result.state == "extracted"
    sent = str(fake_llm.calls[-1]["messages"])
    assert "正式正文甲乙" in sent and "开启区块名称" in sent and "开启区块内容" in sent
    assert "关闭区块名称" not in sent and "关闭区块内容" not in sent
    row = await _ledger(app, chapters[1]["id"])
    # 指纹与章节分析同口径：开启的区块计入来源指纹，正文指纹仍只看 Chapter.content
    assert row.source_hash != hashlib.sha256("正式正文甲乙".encode()).hexdigest()
    assert row.text_hash == text_hash("正式正文甲乙")
    body, section = row.facts
    assert (body["start"], body["end"]) == (0, 4)
    # 引自作者区块的引句不在正文坐标系内：不定位
    assert section["quote"] == "开启区块内容" and section["start"] is None and section["end"] is None
    assert (await _extract(app, fake_llm, user, project, chapters[1])).state == "reused"


async def test_malformed_output_is_tolerated(app, client, fake_llm):
    user, project, chapters = await seed(client, {1: BROKEN, 2: "  \n"})
    fake_llm.push("这不是 JSON")
    failed = await _extract(app, fake_llm, user, project, chapters[1])
    assert failed.state == "failed" and failed.error.startswith("AI 输出解析失败：")
    assert await _ledger(app, chapters[1]["id"]) is None
    fake_llm.push(AIProviderError("模型调用失败：超时"))
    assert (await _extract(app, fake_llm, user, project, chapters[1])).error == "模型调用失败：超时"
    assert await _ledger(app, chapters[1]["id"]) is None
    # 带围栏、夹杂畸形条目：保留合法事实并给出 warning
    noisy = extraction(RAW, {"category": "nope", "statement": "x"}, {"statement": "缺类别"})
    fake_llm.push("```json\n" + noisy + "\n```")
    partial = await _extract(app, fake_llm, user, project, chapters[1])
    assert (partial.state, partial.facts, partial.warnings) == ("extracted", 1, ("有 2 条事实格式不正确，已忽略",))
    # 没有事实列表也落库为空台账（该章已检查过，下次可复用）
    fake_llm.push('{"note": "没有事实"}')
    empty = await _extract(app, fake_llm, user, project, chapters[1], force=True)
    assert (empty.state, empty.facts, empty.warnings) == ("extracted", 0, ("模型输出中没有事实列表",))
    assert (await _ledger(app, chapters[1]["id"])).facts == []
    # 空章不调用模型
    calls = len(fake_llm.calls)
    blank = await _extract(app, fake_llm, user, project, chapters[2])
    assert (blank.state, blank.error) == ("skipped_changed", svc.EMPTY_CONTENT) and len(fake_llm.calls) == calls


async def test_steps_are_independently_callable(app, client, fake_llm):
    """任务处理器的用法：组装、调用、落库分三步，落库不自行提交。"""
    from app.services.consistency.model_call import complete

    _user, project, chapters = await seed(client, {1: BROKEN})
    async with app.state.sessionmaker() as db:
        source = await svc.load_source(db, project["id"], chapters[1]["id"])
        assert await svc.load_source(db, project["id"], 987654) is None
    messages = svc.build_extract_messages(source)
    fake_llm.push(extraction(RAW))
    facts, warnings = svc.parse_extraction(await complete(target(fake_llm), messages), source)
    assert warnings == [] and fake_llm.calls[-1]["messages"] == messages
    async with app.state.sessionmaker() as db:
        saved = await svc.save_ledger(db, source, facts, model="fake-model")
        assert saved is not None and saved.facts == facts
        await db.rollback()
    assert await _ledger(app, chapters[1]["id"]) is None
