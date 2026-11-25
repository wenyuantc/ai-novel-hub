"""一致性检查资料的作品备份：v12 往返、证据章节重映射与缺失、运行记录不导出、v11 升级与校验。"""

import json
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import Chapter, ConsistencyIssue, ConsistencyLedger, ConsistencyRun
from app.services.mentions.matcher import text_hash
from app.services.project_transfer import exporter, importer
from tests.helpers import create_project, register

API = "/api/v1/projects"
FIRST = "😀 林远的佩剑在山门前断成两截。"
SECOND = "林远拔出佩剑，剑身完好无损。"
DELETED_CHAPTER_ID = 987654


async def _backup(client, pid: int) -> dict:
    resp = await client.get(f"{API}/{pid}/backup")
    assert resp.status_code == 200, resp.text
    return json.loads(resp.content)


async def _preview(client, data: dict):
    raw = json.dumps(data, ensure_ascii=False).encode()
    return await client.post(f"{API}/import/preview", files={"file": ("b.ainovel.json", raw, "application/json")})


async def _import(client, data: dict) -> dict:
    resp = await _preview(client, data)
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    resp = await client.post(f"{API}/import", json={"token": preview["token"]})
    assert resp.status_code == 201, resp.text
    return {"preview": preview, **resp.json()}


def _fact(fact_id: str, content: str, quote: str) -> dict:
    start = content.index(quote)
    return {"id": fact_id, "category": "item", "subject": "佩剑", "subject_key": "佩剑", "statement": quote,
            "time_hint": "", "quote": quote, "start": start, "end": start + len(quote)}


async def _seed(client, app) -> tuple[dict, list[int]]:
    user = await register(client)
    project = await create_project(client)
    chapter_ids = []
    for number, content in ((1, FIRST), (2, SECOND)):
        resp = await client.post(f"{API}/{project['id']}/chapters",
                                 json={"title": f"第{number}章", "chapter_no": number, "content": content})
        assert resp.status_code == 201, resp.text
        chapter_ids.append(resp.json()["id"])
    first, second = chapter_ids
    async with app.state.sessionmaker() as db:
        run = ConsistencyRun(project_id=project["id"], scope={"mode": "all", "chapter_ids": chapter_ids},
                             status="succeeded", stats={"extracted": 2})
        db.add(run)
        await db.flush()
        facts = {first: [_fact("1", FIRST, "佩剑在山门前断成两截")], second: [_fact("1", SECOND, "剑身完好无损")]}
        for chapter_id, content in ((first, FIRST), (second, SECOND)):
            db.add(ConsistencyLedger(project_id=project["id"], chapter_id=chapter_id, source_hash="c" * 64,
                                     text_hash=text_hash(content), facts=facts[chapter_id], model="fake-model"))

        def evidence(chapter_id, content):
            fact = facts[chapter_id][0]
            return {"chapter_id": chapter_id, "fact_id": fact["id"], "quote": fact["quote"], "start": fact["start"],
                    "end": fact["end"], "text_hash": text_hash(content)}

        db.add_all([
            ConsistencyIssue(
                project_id=project["id"], category="item", title="佩剑去向前后不一", description="第 1 章已毁",
                suggestion="补写重铸", severity="high", status="ignored", note="有意为之", revision=3,
                status_changed_by=user["id"], status_changed_at=datetime(2026, 11, 1, tzinfo=UTC),
                identity_key="1" * 64, evidence=[evidence(first, FIRST), evidence(second, SECOND)],
                first_run_id=run.id, last_seen_run_id=run.id, last_checked_run_id=run.id, reappeared=True,
            ),
            # 一处证据所在章节已被删除：导出仍带原 id，导入时置空并保留引句。
            ConsistencyIssue(
                project_id=project["id"], category="timeline", title="时间顺序", severity="low",
                identity_key="2" * 64, evidence=[
                    {"chapter_id": DELETED_CHAPTER_ID, "fact_id": "4", "quote": "三日之后", "start": None,
                     "end": None, "text_hash": "d" * 64},
                    evidence(second, SECOND),
                ], first_run_id=run.id,
            ),
        ])
        await db.commit()
    return project, chapter_ids


async def test_v12_roundtrip_remaps_evidence_and_drops_runs(client, app):
    project, (first, second) = await _seed(client, app)
    data = await _backup(client, project["id"])
    assert (data["version"], data["app_schema"]) == (exporter.VERSION, exporter.APP_SCHEMA) == (16, "0073")
    tables = data["tables"]
    assert "consistency_runs" not in tables
    assert [row["chapter_id"] for row in tables["consistency_ledgers"]] == [first, second]
    excluded = {"project_id", "status_changed_by", "first_run_id", "last_seen_run_id", "last_checked_run_id"}
    assert all(not excluded & set(row) for row in tables["consistency_issues"])
    assert all(not {"project_id", "task_id"} & set(row) for row in tables["consistency_ledgers"])

    imported = await _import(client, data)
    new_pid = imported["project"]["id"]
    assert imported["counts"]["consistency_ledgers"] == 2 and imported["counts"]["consistency_issues"] == 2
    warning = "一致性问题证据中有 1 处引用在备份中找不到，已置空或去掉"
    assert warning in imported["preview"]["warnings"] and warning in imported["warnings"]

    async with app.state.sessionmaker() as db:
        chapters = {c.chapter_no: c for c in await db.scalars(select(Chapter).where(Chapter.project_id == new_pid))}
        new_first, new_second = chapters[1].id, chapters[2].id
        assert {new_first, new_second}.isdisjoint({first, second})
        ledgers = list(await db.scalars(select(ConsistencyLedger).where(ConsistencyLedger.project_id == new_pid)
                                        .order_by(ConsistencyLedger.id)))
        assert [ledger.chapter_id for ledger in ledgers] == [new_first, new_second]
        assert all(ledger.task_id is None and ledger.model == "fake-model" for ledger in ledgers)
        # 台账复用与证据定位依赖的指纹、事实和码点偏移原样保留。
        assert [ledger.text_hash for ledger in ledgers] == [text_hash(FIRST), text_hash(SECOND)]
        assert [ledger.source_hash for ledger in ledgers] == ["c" * 64] * 2
        fact = ledgers[0].facts[0]
        assert chapters[1].content[fact["start"]:fact["end"]] == fact["quote"] == "佩剑在山门前断成两截"
        issues = list(await db.scalars(select(ConsistencyIssue).where(ConsistencyIssue.project_id == new_pid)
                                       .order_by(ConsistencyIssue.id)))
        sword, timeline = issues
        assert (sword.status, sword.note, sword.revision, sword.reappeared) == ("ignored", "有意为之", 3, True)
        assert (sword.category, sword.severity, sword.identity_key) == ("item", "high", "1" * 64)
        assert sword.status_changed_at is not None and sword.status_changed_by is None
        assert [e["chapter_id"] for e in sword.evidence] == [new_first, new_second]
        for item, chapter in zip(sword.evidence, (chapters[1], chapters[2]), strict=True):
            assert chapter.content[item["start"]:item["end"]] == item["quote"]
            assert item["text_hash"] == text_hash(chapter.content)
        # 映射不到的证据：chapter_id 置空，引句与指纹保留。
        assert timeline.evidence == [
            {"chapter_id": None, "fact_id": "4", "quote": "三日之后", "start": None, "end": None,
             "text_hash": "d" * 64},
            {**sword.evidence[1]},
        ]
        for issue in issues:
            assert (issue.first_run_id, issue.last_seen_run_id, issue.last_checked_run_id) == (None, None, None)
        assert await db.scalar(select(ConsistencyRun.id).where(ConsistencyRun.project_id == new_pid)) is None
        # 源作品不受影响。
        source = await db.scalar(select(ConsistencyIssue).where(ConsistencyIssue.identity_key == "1" * 64,
                                                                ConsistencyIssue.project_id == project["id"]))
        assert source.first_run_id is not None and [e["chapter_id"] for e in source.evidence] == [first, second]

    # 再导出：除 id 与章节引用外逐行一致（含时间戳）。
    again = await _backup(client, new_pid)

    def canonical(source: dict, chapter_map: dict) -> dict:
        out = {}
        for name in importer.CONSISTENCY_TABLES:
            rows = deepcopy(source["tables"][name])
            for row in rows:
                row.pop("id")
                if "chapter_id" in row:
                    row["chapter_id"] = chapter_map[row["chapter_id"]]
                for item in row.get("evidence", []):
                    item["chapter_id"] = chapter_map.get(item["chapter_id"])
            out[name] = rows
        return out

    assert canonical(data, {first: 1, second: 2}) == canonical(again, {new_first: 1, new_second: 2})


async def test_backup_forged_run_and_actor_references_are_ignored(client, app):
    project, _ = await _seed(client, app)
    data = await _backup(client, project["id"])
    for row in data["tables"]["consistency_issues"]:
        row.update(first_run_id=1, last_seen_run_id=1, last_checked_run_id=1, status_changed_by=1)
    for row in data["tables"]["consistency_ledgers"]:
        row["task_id"] = 1
    data["tables"]["consistency_runs"] = [{"id": 1, "status": "succeeded", "scope": {}, "stats": {}}]
    imported = await _import(client, data)
    assert any("consistency_runs" in warning for warning in imported["preview"]["warnings"])
    new_pid = imported["project"]["id"]
    async with app.state.sessionmaker() as db:
        for issue in await db.scalars(select(ConsistencyIssue).where(ConsistencyIssue.project_id == new_pid)):
            assert (issue.first_run_id, issue.last_seen_run_id, issue.last_checked_run_id,
                    issue.status_changed_by) == (None, None, None, None)
        for ledger in await db.scalars(select(ConsistencyLedger).where(ConsistencyLedger.project_id == new_pid)):
            assert ledger.task_id is None
        assert await db.scalar(select(ConsistencyRun.id).where(ConsistencyRun.project_id == new_pid)) is None


async def test_v11_backup_imports_with_empty_consistency_tables(client, app):
    project, _ = await _seed(client, app)
    current = await _backup(client, project["id"])
    # 真实 v11 文件没有这两张表；带规范空数组同样合法。
    for shape in ("absent", "empty"):
        old = deepcopy(current)
        old["version"] = 11
        old["app_schema"] = "0057"
        for name in importer.CONSISTENCY_TABLES:
            old["tables"].pop(name)
            if shape == "empty":
                old["tables"][name] = []
        imported = await _import(client, old)
        assert imported["preview"]["version"] == 11
        exported = await _backup(client, imported["project"]["id"])
        assert exported["version"] == 16
        assert exported["tables"]["consistency_ledgers"] == [] == exported["tables"]["consistency_issues"]
        assert len(exported["tables"]["chapters"]) == 2
    # 旧版本不能夹带一致性检查资料。
    for name in importer.CONSISTENCY_TABLES:
        forged = deepcopy(current)
        forged["version"] = 11
        other = next(table for table in importer.CONSISTENCY_TABLES if table != name)
        forged["tables"][other] = []  # 只保留 name 这一张非空表
        resp = await _preview(client, forged)
        assert resp.status_code == 400, resp.text
        assert "旧版备份不支持一致性检查资料" in resp.json()["detail"]
    forged = deepcopy(current)
    forged["version"] = 11
    forged["tables"]["consistency_issues"] = None
    assert (await _preview(client, forged)).status_code == 400


async def test_v12_preview_rejects_values_that_break_reads(client, app):
    project, (first, _) = await _seed(client, app)
    data = await _backup(client, project["id"])

    def issue(d):
        return d["tables"]["consistency_issues"][0]

    def ledger(d):
        return d["tables"]["consistency_ledgers"][0]

    cases = (
        (lambda d: issue(d).update(category="weird"), "category"),
        (lambda d: issue(d).update(severity="urgent"), "severity"),
        (lambda d: issue(d).update(status="closed"), "status"),
        (lambda d: issue(d).update(revision=0), "revision"),
        (lambda d: issue(d).update(title="  "), "title"),
        (lambda d: issue(d).update(evidence={"chapter_id": first}), "evidence"),
        (lambda d: issue(d)["evidence"][0].pop("quote"), "evidence"),
        (lambda d: issue(d)["evidence"][0].update(start=5, end=None), "evidence"),
        (lambda d: issue(d)["evidence"][0].update(start=9, end=3), "evidence"),
        (lambda d: issue(d)["evidence"][0].update(text_hash="short"), "evidence"),
        (lambda d: issue(d)["evidence"][0].update(chapter_id="1"), "evidence"),
        (lambda d: d["tables"]["consistency_issues"][1].update(identity_key=issue(d)["identity_key"]),
         "identity_key"),
        (lambda d: ledger(d).update(chapter_id=999999), "chapter_id"),
        (lambda d: d["tables"]["consistency_ledgers"][1].update(chapter_id=first), "已有台账"),
        (lambda d: ledger(d).update(facts="x"), "facts"),
        (lambda d: ledger(d)["facts"][0].update(category="weather"), "facts"),
        (lambda d: ledger(d)["facts"][0].update(start=-1), "facts"),
        (lambda d: ledger(d)["facts"].append(deepcopy(ledger(d)["facts"][0])), "facts"),
        (lambda d: ledger(d).update(schema_version=0), "schema_version"),
        (lambda d: ledger(d).update(text_hash="h" * 65), "text_hash"),
    )
    for mutate, expected in cases:
        forged = deepcopy(data)
        mutate(forged)
        resp = await _preview(client, forged)
        assert resp.status_code == 400, (expected, resp.text)
        assert expected in json.dumps(resp.json(), ensure_ascii=False), (expected, resp.text)
    assert (await _preview(client, data)).status_code == 200
