"""修改影响资料的作品备份：v13 往返、章节与设定变化引用的重映射与缺失、v12 升级、旧版本夹带与非法值拒绝。"""

import json
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.models import Chapter, ConsistencyImpact, ConsistencyLedger, SettingProgression, SettingTimeline
from app.services.mentions.matcher import text_hash
from app.services.project_transfer import exporter, importer
from tests.test_consistency_transfer import (
    API,
    DELETED_CHAPTER_ID,
    FIRST,
    SECOND,
    _backup,
    _fact,
    _import,
    _preview,
    _seed,
)
from tests.test_writeback import _analyze, _world

PREV_HASH = "e" * 64
WARNING = "修改影响记录中有 3 处引用在备份中找不到，已置空或去掉"


async def _seed_impact(client, app) -> tuple[dict, list[int]]:
    project, (first, second) = await _seed(client, app)
    async with app.state.sessionmaker() as db:
        await db.execute(update(ConsistencyLedger).where(ConsistencyLedger.chapter_id == first).values(
            prev_facts=[_fact("1", FIRST, "佩剑")], prev_source_hash=PREV_HASH, prev_text_hash=PREV_HASH))
        fact = _fact("1", SECOND, "剑身完好无损")
        evidence = {"fact_id": "1", "quote": fact["quote"], "start": fact["start"], "end": fact["end"],
                    "text_hash": text_hash(SECOND)}
        db.add(ConsistencyImpact(
            project_id=project["id"], chapter_id=first, source_hash="c" * 64, text_hash=text_hash(FIRST),
            base_source_hash=PREV_HASH,
            changes=[{"kind": "changed", "category": "item", "subject": "佩剑", "subject_key": "佩剑",
                      "before": "佩剑完好", "after": "佩剑断成两截"}],
            items=[
                {"chapter_id": second, "subjects": ["佩剑"], "evidence": [evidence], "fact_count": 1,
                 "status": "reviewed"},
                # 被列章节在导出前已删除：导入时整项去掉
                {"chapter_id": DELETED_CHAPTER_ID, "subjects": ["佩剑"], "evidence": [], "fact_count": 0,
                 "status": "pending"},
            ],
            uncovered=[second, DELETED_CHAPTER_ID], acknowledged=[424242], revision=5, model="fake-model",
            assessed_at=datetime(2026, 11, 2, tzinfo=UTC),
        ))
        await db.commit()
    return project, [first, second]


async def test_v13_roundtrip_remaps_impact_references(client, app):
    project, (first, second) = await _seed_impact(client, app)
    data = await _backup(client, project["id"])
    assert (data["version"], data["app_schema"]) == (exporter.VERSION, exporter.APP_SCHEMA) == (18, "0075")
    (row,) = data["tables"]["consistency_impacts"]
    assert not {"project_id", "actor_id", "task_id"} & set(row) and row["chapter_id"] == first
    ledger_row = next(r for r in data["tables"]["consistency_ledgers"] if r["chapter_id"] == first)
    assert (ledger_row["prev_source_hash"], ledger_row["prev_facts"][0]["quote"]) == (PREV_HASH, "佩剑")

    imported = await _import(client, data)
    new_pid = imported["project"]["id"]
    assert imported["counts"]["consistency_impacts"] == 1
    assert WARNING in imported["preview"]["warnings"] and WARNING in imported["warnings"]
    async with app.state.sessionmaker() as db:
        chapters = {c.chapter_no: c for c in await db.scalars(select(Chapter).where(Chapter.project_id == new_pid))}
        new_first, new_second = chapters[1].id, chapters[2].id
        impact = await db.scalar(select(ConsistencyImpact).where(ConsistencyImpact.project_id == new_pid))
        assert (impact.chapter_id, impact.revision, impact.model, impact.actor_id, impact.task_id) == (
            new_first, 5, "fake-model", None, None)
        assert (impact.source_hash, impact.text_hash, impact.base_source_hash) == (
            "c" * 64, text_hash(FIRST), PREV_HASH)
        assert impact.assessed_at is not None and impact.changes == row["changes"]
        # 影响项与未覆盖章节改写为新章节 id；对应不上的去掉；确认状态与证据锚点保留
        (item,) = impact.items
        assert (item["chapter_id"], item["status"], item["subjects"], item["fact_count"]) == (
            new_second, "reviewed", ["佩剑"], 1)
        evidence = item["evidence"][0]
        assert chapters[2].content[evidence["start"]:evidence["end"]] == evidence["quote"]
        assert evidence["text_hash"] == text_hash(chapters[2].content)
        assert impact.uncovered == [new_second] and impact.acknowledged == []
        ledger = await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == new_first))
        assert (ledger.prev_facts, ledger.prev_source_hash, ledger.prev_text_hash) == (
            [_fact("1", FIRST, "佩剑")], PREV_HASH, PREV_HASH)
        other = await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == new_second))
        assert (other.prev_facts, other.prev_source_hash, other.prev_text_hash) == (None, None, None)
        # 源作品不受影响
        source = await db.scalar(select(ConsistencyImpact).where(ConsistencyImpact.project_id == project["id"]))
        assert source.uncovered == [second, DELETED_CHAPTER_ID] and len(source.items) == 2
    # 导入后的读取接口可用：确认状态保持
    detail = (await client.get(f"{API}/{new_pid}/consistency/impacts/{new_first}")).json()
    assert detail["revision"] == 5 and detail["assessment"]["items"][0]["status"] == "reviewed"
    assert detail["assessment"]["items"][0]["changed"] is False

    # 再导出：除 id 与章节引用外逐行一致
    again = await _backup(client, new_pid)
    (copied,) = again["tables"]["consistency_impacts"]
    expected = deepcopy(row)
    expected.update(chapter_id=new_first, uncovered=[new_second], acknowledged=[],
                    items=[{**row["items"][0], "chapter_id": new_second}])
    copied.pop("id")
    expected.pop("id")
    assert copied == expected


async def test_acknowledged_progressions_are_remapped(client, app, fake_llm):
    base, _characters, _rel, _career, _stages, _org, chapters = await _world(client)
    third = chapters[3]
    await _analyze(client, fake_llm, third, character_changes=[{"name": "林远", "current_state": "重伤"}])
    edited = await client.patch(f"{base}/chapters/{third['id']}", json={"content": third["content"] + "改动。"})
    assert edited.status_code == 200, edited.text
    detail = (await client.get(f"{base}/consistency/impacts/{third['id']}")).json()
    (progression,) = detail["stale_progressions"]
    saved = await client.patch(f"{base}/consistency/impacts/{third['id']}",
                               json={"revision": 0, "acknowledged": [progression["id"]]})
    assert saved.status_code == 200, saved.text
    project_id = int(base.rsplit("/", 1)[1])
    imported = await _import(client, await _backup(client, project_id))
    new_pid = imported["project"]["id"]
    async with app.state.sessionmaker() as db:
        impact = await db.scalar(select(ConsistencyImpact).where(ConsistencyImpact.project_id == new_pid))
        new_ids = set(await db.scalars(
            select(SettingProgression.id)
            .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
            .where(SettingTimeline.project_id == new_pid, SettingProgression.source == "analysis")))
        assert impact.assessed_at is None and len(impact.acknowledged) == 1
        assert set(impact.acknowledged) <= new_ids and impact.acknowledged != [progression["id"]]
        new_chapter = impact.chapter_id
    copied = (await client.get(f"{API}/{new_pid}/consistency/impacts/{new_chapter}")).json()
    assert [item["acknowledged"] for item in copied["stale_progressions"]] == [True]
    assert copied["stale_summary"] is not None


async def test_backup_forged_actor_and_task_references_are_ignored(client, app):
    """文件里伪造的评估人 / 任务引用不落库（这两列不导出，导入时同样不采信）。"""
    project, _ = await _seed_impact(client, app)
    data = await _backup(client, project["id"])
    for row in data["tables"]["consistency_impacts"]:
        row.update(actor_id=1, task_id=1, project_id=project["id"])
    imported = await _import(client, data)
    new_pid = imported["project"]["id"]
    assert new_pid != project["id"]
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(ConsistencyImpact).where(ConsistencyImpact.project_id == new_pid))
        assert (row.actor_id, row.task_id) == (None, None)


async def test_v12_backup_imports_and_old_versions_cannot_carry_impact_data(client, app):
    project, _ = await _seed_impact(client, app)
    current = await _backup(client, project["id"])
    # 真实 v12 文件没有影响表，台账行没有上一版三列；带规范空数组 / 空值同样合法。
    for shape in ("absent", "empty"):
        old = deepcopy(current)
        old["version"] = 12
        old["app_schema"] = "0058"
        old["tables"].pop(importer.IMPACT_TABLE)
        if shape == "empty":
            old["tables"][importer.IMPACT_TABLE] = []
        for row in old["tables"]["consistency_ledgers"]:
            for column in importer.LEDGER_PREV_COLUMNS:
                row.pop(column)
                if shape == "empty":
                    row[column] = None
        imported = await _import(client, old)
        assert imported["preview"]["version"] == 12
        exported = await _backup(client, imported["project"]["id"])
        assert exported["version"] == 18 and exported["tables"]["consistency_impacts"] == []
        assert len(exported["tables"]["consistency_ledgers"]) == 2 and len(exported["tables"]["chapters"]) == 2
        assert all(row["prev_facts"] is None for row in exported["tables"]["consistency_ledgers"])
        assert len(exported["tables"]["consistency_issues"]) == 2
    # 旧版本不能夹带修改影响资料。
    forged = deepcopy(current)
    forged["version"] = 12
    for row in forged["tables"]["consistency_ledgers"]:
        row.update(dict.fromkeys(importer.LEDGER_PREV_COLUMNS))
    resp = await _preview(client, forged)
    assert resp.status_code == 400 and "旧版备份不支持修改影响资料" in resp.json()["detail"]
    forged = deepcopy(current)
    forged["version"] = 12
    forged["tables"][importer.IMPACT_TABLE] = []
    resp = await _preview(client, forged)
    assert resp.status_code == 400 and "旧版备份不支持修改影响资料" in resp.json()["detail"]


async def test_v13_preview_rejects_values_that_break_reads(client, app):
    project, (first, second) = await _seed_impact(client, app)
    data = await _backup(client, project["id"])

    def impact(d):
        return d["tables"]["consistency_impacts"][0]

    def ledger(d):
        return next(row for row in d["tables"]["consistency_ledgers"] if row["chapter_id"] == first)

    cases = (
        (lambda d: impact(d).update(chapter_id=999999), "chapter_id"),
        (lambda d: d["tables"]["consistency_impacts"].append({**impact(d), "id": 99}), "已有影响记录"),
        (lambda d: impact(d).update(revision=0), "revision"),
        (lambda d: impact(d).update(changes="x"), "changes"),
        (lambda d: impact(d)["changes"][0].update(kind="moved"), "changes"),
        (lambda d: impact(d)["changes"][0].update(category="weather"), "changes"),
        (lambda d: impact(d).update(items={"chapter_id": second}), "items"),
        (lambda d: impact(d)["items"][0].update(status="open"), "items"),
        (lambda d: impact(d)["items"][0].update(chapter_id="2"), "items"),
        (lambda d: impact(d)["items"][0]["evidence"][0].update(text_hash="short"), "items"),
        (lambda d: impact(d)["items"][0]["evidence"][0].update(start=9, end=3), "items"),
        (lambda d: impact(d)["items"].append(deepcopy(impact(d)["items"][0])), "items"),
        (lambda d: impact(d).update(uncovered=["2"]), "uncovered"),
        (lambda d: impact(d).update(uncovered=[second, second]), "uncovered"),
        (lambda d: impact(d).update(acknowledged=[0]), "acknowledged"),
        (lambda d: impact(d).update(source_hash="h" * 65), "source_hash"),
        (lambda d: ledger(d).update(prev_source_hash=None), "prev_"),
        (lambda d: ledger(d).update(prev_facts={"id": "1"}), "prev_facts"),
        (lambda d: ledger(d)["prev_facts"][0].update(category="weather"), "prev_facts"),
        (lambda d: ledger(d)["prev_facts"].append(deepcopy(ledger(d)["prev_facts"][0])), "prev_facts"),
        (lambda d: ledger(d).update(prev_text_hash="h" * 65), "prev_text_hash"),
    )
    for mutate, expected in cases:
        forged = deepcopy(data)
        mutate(forged)
        resp = await _preview(client, forged)
        assert resp.status_code == 400, (expected, resp.text)
        assert expected in json.dumps(resp.json(), ensure_ascii=False), (expected, resp.text)
    assert (await _preview(client, data)).status_code == 200
