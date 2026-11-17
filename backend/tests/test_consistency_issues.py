"""跨章比对与问题合并：佩剑示例、范围口径、三种状态的重复检查、问题指认、证据校验、伏笔记录、派生字段。"""

from sqlalchemy import func, select

from app.models import ConsistencyIssue, Foreshadow
from app.schemas.consistency import IssueEvidence
from app.services.consistency import issues as svc
from app.services.consistency import ledger as ledger_svc
from app.services.mentions.matcher import text_hash
from tests.consistency_helpers import API, add_ledger, comparison, extraction, fact, new_run, seed, target

BROKEN = "😀 林远的佩剑在山门前断成两截。"
USED = "林远拔出佩剑，一剑逼退来敌。"
LATER_A = "多年以后，佩剑仍挂在祠堂。"
LATER_B = "祠堂里从来没有挂过佩剑。"


def _ev(chapter: dict, fact_id: str = "1") -> dict:
    return {"chapter_id": chapter["id"], "fact_id": fact_id}


def _issue(*evidence: dict, **fields) -> dict:
    return {"existing_issue_id": None, "category": "item", "title": "佩剑去向存在潜在冲突",
            "description": "前文佩剑已毁，后文仍在使用。", "suggestion": "补写重铸，或改用其他兵器。",
            "severity": "high", "foreshadow_id": None, "evidence": list(evidence), **fields}


async def _sword_world(app, client, extra: dict[int, str] | None = None):
    user, project, chapters = await seed(client, {5: BROKEN, 12: USED, **(extra or {})})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[5], [fact("1", BROKEN, "佩剑在山门前断成两截")])
        await add_ledger(db, project["id"], chapters[12], [fact("1", USED, "拔出佩剑")])
        await db.commit()
    return user, project, chapters


async def _check(app, fake_llm, user, project, scope: list[dict], *responses: str, budget: int = 12_000):
    for response in responses:
        fake_llm.push(response)
    run_id = await new_run(app, project["id"], [chapter["id"] for chapter in scope])
    outcome = await svc.compare_and_merge(
        app.state.sessionmaker, target(fake_llm), user_id=user["id"], project_id=project["id"], run_id=run_id,
        scope_chapter_ids=[chapter["id"] for chapter in scope], char_budget=budget,
    )
    return run_id, outcome


async def _issues(app, project_id: int) -> list[ConsistencyIssue]:
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(ConsistencyIssue).where(ConsistencyIssue.project_id == project_id)
                                     .order_by(ConsistencyIssue.id)))


async def _set_status(app, issue_id: int, status: str) -> None:
    async with app.state.sessionmaker() as db:
        row = await db.get(ConsistencyIssue, issue_id)
        row.status, row.note, row.revision = status, "作者备注", row.revision + 1
        await db.commit()


async def test_broken_sword_end_to_end(app, client, fake_llm):
    """AC1：第 5 章佩剑已毁、第 12 章仍使用 → 一个物品去向类问题，两处证据带服务端偏移。"""
    user, project, chapters = await seed(client, {5: BROKEN, 12: USED})
    fake_llm.push(extraction({"category": "item", "subject": "佩剑", "statement": "林远的佩剑断成两截",
                              "quote": "佩剑在山门前断成两截"}))
    fake_llm.push(extraction({"category": "item", "subject": " 佩剑 ", "statement": "林远拔出佩剑迎敌",
                              "quote": "拔出佩剑"}))
    for number in (5, 12):
        result = await ledger_svc.extract_chapter(app.state.sessionmaker, target(fake_llm), user_id=user["id"],
                                                  project_id=project["id"], chapter_id=chapters[number]["id"])
        assert result.state == "extracted"
    run_id, outcome = await _check(app, fake_llm, user, project, list(chapters.values()),
                                   comparison(_issue(_ev(chapters[12]), _ev(chapters[5]))))
    assert (outcome.batches, outcome.failed_batches, outcome.warnings) == (1, 0, [])
    assert (outcome.stats.created, outcome.stats.updated) == (1, 0)
    sent = fake_llm.calls[-1]["messages"][1]["content"]
    assert f"[chapter_id={chapters[5]['id']} fact_id=1] 第5章｜物品去向｜佩剑｜林远的佩剑断成两截｜" in sent
    assert f"[chapter_id={chapters[12]['id']} fact_id=1] 第12章｜物品去向｜佩剑｜林远拔出佩剑迎敌｜" in sent
    # 比对只发送台账事实：不含章节正文全文与作品信息
    assert BROKEN not in sent and USED not in sent and "星海纪元" not in str(fake_llm.calls[-1]["messages"])
    (issue,) = await _issues(app, project["id"])
    assert (issue.category, issue.status, issue.severity, issue.revision) == ("item", "open", "high", 1)
    assert issue.reappeared is False and issue.note == ""
    assert (issue.first_run_id, issue.last_seen_run_id, issue.last_checked_run_id) == (run_id, run_id, run_id)
    assert issue.identity_key == svc.identity_key("item", "佩剑", [chapters[5]["id"], chapters[12]["id"]])
    first, second = (IssueEvidence.model_validate(item) for item in issue.evidence)
    # 证据按章节顺序，引句与偏移取自台账
    assert (first.chapter_id, first.quote, first.start, first.end) == (chapters[5]["id"], "佩剑在山门前断成两截", 5, 15)
    assert (second.chapter_id, second.quote, second.start, second.end) == (chapters[12]["id"], "拔出佩剑", 2, 6)
    assert BROKEN[first.start:first.end] == first.quote and USED[second.start:second.end] == second.quote
    assert (first.text_hash, second.text_hash) == (text_hash(BROKEN), text_hash(USED))
    async with app.state.sessionmaker() as db:
        flags = await svc.issue_flags(db, project["id"], [issue])
    assert flags[issue.id] == svc.IssueFlags(needs_recheck=False, not_reproduced=False, stale=(False, False))


async def test_range_scope(app, client, fake_llm):
    """AC3：范围外新鲜台账参与比对；完全在范围外的问题不报告；范围外过期台账不使用。"""
    stale_text = "佩剑早已赠予师妹。"
    user, project, chapters = await _sword_world(app, client, {20: LATER_A, 21: LATER_B, 30: stale_text,
                                                              31: "玉佩碎了。", 32: "玉佩完好。"})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[20], [fact("1", LATER_A, "佩剑仍挂在祠堂")])
        await add_ledger(db, project["id"], chapters[21], [fact("1", LATER_B, "从来没有挂过佩剑")])
        await add_ledger(db, project["id"], chapters[30], [fact("1", stale_text, "赠予师妹")], fresh=False)
        # 另一主体的事实完全在范围外：该组不参与比对
        await add_ledger(db, project["id"], chapters[31], [fact("1", "玉佩碎了。", "玉佩碎了", subject="玉佩")])
        await add_ledger(db, project["id"], chapters[32], [fact("1", "玉佩完好。", "玉佩完好", subject="玉佩")])
        await db.commit()
    _run, outcome = await _check(app, fake_llm, user, project, [chapters[12]], comparison(
        _issue(_ev(chapters[5]), _ev(chapters[12])),
        _issue(_ev(chapters[20]), _ev(chapters[21]), title="祠堂佩剑前后不一"),
        _issue(_ev(chapters[31]), _ev(chapters[32]), title="玉佩前后不一"),
    ))
    assert len(fake_llm.calls) == 1 and outcome.stale_outside == 1
    sent = fake_llm.calls[-1]["messages"][1]["content"]
    assert "断成两截" in sent and "佩剑仍挂在祠堂" in sent
    assert "赠予师妹" not in sent and "玉佩" not in sent
    (issue,) = await _issues(app, project["id"])
    assert {item["chapter_id"] for item in issue.evidence} == {chapters[5]["id"], chapters[12]["id"]}
    assert outcome.warnings == ["有 2 处证据引用了不存在的事实，已丢弃", "有 1 个问题的证据不足两个章节，已忽略"]
    async with app.state.sessionmaker() as db:
        data = await svc.load_compare_input(db, project["id"], [chapters[12]["id"]])
    assert chapters[30]["id"] not in data.covered_ids and len(data.covered_ids) == 6
    assert [group.key for group in svc.group_facts(data)] == ["subject:佩剑"]


async def test_repeat_checks_follow_issue_status(app, client, fake_llm):
    """C8：忽略保持；已解决再现 → 待处理并标注；待处理未检出 → 只记「本次未复现」。都不产生重复问题。"""
    user, project, chapters = await _sword_world(app, client)
    scope = list(chapters.values())
    found = comparison(_issue(_ev(chapters[5]), _ev(chapters[12])))
    first_run, _ = await _check(app, fake_llm, user, project, scope, found)
    (issue,) = await _issues(app, project["id"])

    await _set_status(app, issue.id, "ignored")
    second_run, outcome = await _check(app, fake_llm, user, project, scope, comparison(
        _issue(_ev(chapters[5]), _ev(chapters[12]), description="新的说明", suggestion="新的建议")))
    (issue,) = await _issues(app, project["id"])
    assert (outcome.stats.created, outcome.stats.updated, outcome.stats.reappeared) == (0, 1, 0)
    assert (issue.status, issue.reappeared, issue.note, issue.revision) == ("ignored", False, "作者备注", 2)
    assert (issue.description, issue.suggestion) == ("新的说明", "新的建议")
    assert (issue.first_run_id, issue.last_seen_run_id, issue.last_checked_run_id) == (first_run, second_run,
                                                                                         second_run)

    await _set_status(app, issue.id, "resolved")
    third_run, outcome = await _check(app, fake_llm, user, project, scope, found)
    (issue,) = await _issues(app, project["id"])
    assert (outcome.stats.updated, outcome.stats.reappeared) == (1, 1)
    # 检查更新不递增 revision，也不动备注
    assert (issue.status, issue.reappeared, issue.note, issue.revision) == ("open", True, "作者备注", 3)
    assert svc.not_reproduced(issue) is False

    fourth_run, outcome = await _check(app, fake_llm, user, project, scope, comparison())
    (issue,) = await _issues(app, project["id"])
    assert (outcome.stats.created, outcome.stats.updated, outcome.stats.not_reproduced) == (0, 0, 1)
    assert (issue.status, issue.last_seen_run_id, issue.last_checked_run_id, issue.revision) == (
        "open", third_run, fourth_run, 3)
    assert svc.not_reproduced(issue) is True

    # 已忽略、已解决的问题未检出时不记录「本次未复现」
    await _set_status(app, issue.id, "resolved")
    _run, outcome = await _check(app, fake_llm, user, project, scope, comparison())
    (issue,) = await _issues(app, project["id"])
    assert outcome.stats.not_reproduced == 0 and issue.last_checked_run_id == fourth_run
    assert (issue.status, svc.not_reproduced(issue)) == ("resolved", False)


async def test_not_reproduced_requires_full_coverage_scope_and_complete_run(app, client, fake_llm):
    user, project, chapters = await _sword_world(app, client, {20: LATER_A})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[20], [fact("1", LATER_A, "佩剑仍挂在祠堂")])
        await db.commit()
    scope = list(chapters.values())
    first_run, _ = await _check(app, fake_llm, user, project, scope,
                                comparison(_issue(_ev(chapters[5]), _ev(chapters[12]))))

    async def checked() -> int:
        return (await _issues(app, project["id"]))[0].last_checked_run_id

    # 比对批次失败：覆盖不完整，不能说「未复现」
    _run, outcome = await _check(app, fake_llm, user, project, scope, "不是 JSON")
    assert outcome.failed_batches == 1 and outcome.stats.not_reproduced == 0 and await checked() == first_run
    assert outcome.warnings == ["第 1 批比对失败：AI 输出解析失败：模型输出不是合法的 JSON，无法解析"]
    # 问题完全在本次范围之外：不更新
    _run, outcome = await _check(app, fake_llm, user, project, [chapters[20]], comparison())
    assert outcome.stats.not_reproduced == 0 and await checked() == first_run
    # 证据章节的台账已过期（正文改过）：不在覆盖内，不更新
    edited = await client.patch(f"{API}/{project['id']}/chapters/{chapters[12]['id']}", json={"content": USED + "乙"})
    assert edited.status_code == 200, edited.text
    _run, outcome = await _check(app, fake_llm, user, project, scope, comparison())
    assert outcome.stats.not_reproduced == 0 and await checked() == first_run
    assert len(await _issues(app, project["id"])) == 1


async def test_existing_issue_id_merges_and_foreign_ids_are_ignored(app, client, make_client, fake_llm):
    user, project, chapters = await _sword_world(app, client, {20: LATER_A})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[20], [fact("1", LATER_A, "佩剑仍挂在祠堂")])
        await db.commit()
    scope = list(chapters.values())
    await _check(app, fake_llm, user, project, scope, comparison(_issue(_ev(chapters[5]), _ev(chapters[12]))))
    (issue,) = await _issues(app, project["id"])
    old_key = issue.identity_key
    # 已有问题的摘要随比对输入给出
    _run, outcome = await _check(app, fake_llm, user, project, scope, comparison(_issue(
        _ev(chapters[5]), _ev(chapters[12]), _ev(chapters[20]), existing_issue_id=issue.id, description="三处出入")))
    sent = fake_llm.calls[-1]["messages"][1]["content"]
    assert f"#{issue.id} 物品去向｜佩剑去向存在潜在冲突｜" in sent and "existing_issue_id 填写" in sent
    (merged,) = await _issues(app, project["id"])
    assert (outcome.stats.created, outcome.stats.updated, outcome.stats.ignored_refs) == (0, 1, 0)
    assert merged.id == issue.id and merged.description == "三处出入" and len(merged.evidence) == 3
    # 身份键跟随新证据：之后不靠模型指认也能对上，不产生重复
    assert merged.identity_key != old_key
    _run, outcome = await _check(app, fake_llm, user, project, scope, comparison(_issue(
        _ev(chapters[5]), _ev(chapters[12]), _ev(chapters[20]))))
    assert (outcome.stats.created, outcome.stats.updated) == (0, 1) and len(await _issues(app, project["id"])) == 1

    # 另一部作品的问题 id（越权）与不存在的 id 都被忽略，按身份键处理
    other = make_client()
    other_user, other_project, other_chapters = await seed(other, {1: BROKEN, 2: USED}, username="bob")
    async with app.state.sessionmaker() as db:
        await add_ledger(db, other_project["id"], other_chapters[1], [fact("1", BROKEN, "佩剑在山门前断成两截")])
        await add_ledger(db, other_project["id"], other_chapters[2], [fact("1", USED, "拔出佩剑")])
        await db.commit()
    foreign = comparison(
        _issue(_ev(other_chapters[1]), _ev(other_chapters[2]), existing_issue_id=merged.id, description="越权覆盖"),
        _issue(_ev(other_chapters[1]), _ev(other_chapters[2]), existing_issue_id=987654, title="重复"),
    )
    created_run, outcome = await _check(app, fake_llm, other_user, other_project, list(other_chapters.values()),
                                        foreign)
    # 未随本批给出的编号在输出校验阶段就被拿掉，不会走到按 id 合并
    assert (outcome.stats.created, outcome.stats.ignored_refs, outcome.stats.duplicates) == (1, 0, 1)
    assert outcome.warnings == ["有 2 个问题指认了未提供的已有问题编号，已按证据章节去重"]
    # 别的作品的问题不会出现在比对输入里
    assert f"#{merged.id} " not in fake_llm.calls[-1]["messages"][1]["content"]
    (untouched,) = await _issues(app, project["id"])
    assert untouched.description == "前文佩剑已毁，后文仍在使用。" and len(untouched.evidence) == 3
    assert untouched.id == merged.id and untouched.last_seen_run_id != created_run
    (created,) = await _issues(app, other_project["id"])
    assert created.description == "越权覆盖" and created.project_id == other_project["id"]


async def test_unoffered_issue_id_cannot_overwrite_another_issue(app, client, fake_llm):
    """同作品内未随本批给出的问题编号（模型编造）不得覆盖无关问题；合并层对越权 id 仍有兜底。"""
    user, project, chapters = await _sword_world(app, client, {20: LATER_A, 21: LATER_B})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[20], [fact("1", LATER_A, "佩剑仍挂在祠堂", subject="祠堂")])
        await add_ledger(db, project["id"], chapters[21], [fact("1", LATER_B, "从来没有挂过佩剑", subject="祠堂")])
        await db.commit()
    scope = list(chapters.values())
    await _check(app, fake_llm, user, project, scope, comparison(
        _issue(_ev(chapters[20]), _ev(chapters[21]), title="祠堂", description="祠堂原说明")))
    (shrine,) = await _issues(app, project["id"])
    await _set_status(app, shrine.id, "ignored")
    # 只检查第 5、12 章：祠堂问题的证据章节不在本批，摘要不会给出；模型却指认了它的编号
    _run, outcome = await _check(app, fake_llm, user, project, [chapters[5], chapters[12]], comparison(
        _issue(_ev(chapters[5]), _ev(chapters[12]), existing_issue_id=shrine.id, description="佩剑新说明")))
    assert f"#{shrine.id} " not in fake_llm.calls[-1]["messages"][1]["content"]
    assert (outcome.stats.created, outcome.stats.updated, outcome.stats.ignored_refs) == (1, 0, 0)
    assert outcome.warnings == ["有 1 个问题指认了未提供的已有问题编号，已按证据章节去重"]
    untouched, created = await _issues(app, project["id"])
    assert (untouched.id, untouched.description, untouched.status, untouched.note) == (
        shrine.id, "祠堂原说明", "ignored", "作者备注")
    assert untouched.identity_key == shrine.identity_key and untouched.evidence == shrine.evidence
    assert created.description == "佩剑新说明"

    # 合并层兜底：即使上游放过了不属于本作品的编号，也只按身份键处理
    run_id = await new_run(app, project["id"], [chapters[5]["id"]])
    finding = svc.Finding(
        category="item", title="直接合并", description="越权", suggestion="", severity="low",
        evidence=({"chapter_id": chapters[5]["id"], "fact_id": "1", "quote": "", "start": None, "end": None,
                   "text_hash": text_hash(BROKEN)},),
        identity_key="b" * 64, existing_issue_id=987654,
    )
    async with app.state.sessionmaker() as db:
        stats = await svc.merge_findings(db, project_id=project["id"], run_id=run_id, findings=[finding],
                                         covered_chapter_ids=frozenset(), scope_chapter_ids=frozenset())
        await db.commit()
    assert (stats.created, stats.ignored_refs) == (1, 1)


async def test_split_group_is_reported_and_never_marks_not_reproduced(app, client, fake_llm):
    """大组被拆成多批时跨批冲突无法检出：必须进入 warnings，且不能把旧问题标成「本次未复现」。"""
    contents = {number: f"第{number}天，林远仍在赶路。" for number in range(1, 5)}
    user, project, chapters = await seed(client, contents)
    async with app.state.sessionmaker() as db:
        for number, content in contents.items():
            await add_ledger(db, project["id"], chapters[number], [
                fact("1", content, content[:4], category="timeline", subject="时", statement="长" * 400)])
        await db.commit()
    first_run = await new_run(app, project["id"], [chapter["id"] for chapter in chapters.values()])
    async with app.state.sessionmaker() as db:
        db.add(ConsistencyIssue(
            project_id=project["id"], category="timeline", title="旧问题", severity="medium", status="open",
            identity_key="c" * 64, first_run_id=first_run, last_seen_run_id=first_run, last_checked_run_id=first_run,
            evidence=[{"chapter_id": chapters[number]["id"], "fact_id": "1", "quote": "", "start": None, "end": None,
                       "text_hash": text_hash(contents[number])} for number in (1, 4)],
        ))
        await db.commit()
    scope = list(chapters.values())
    # 预算 1000：四条约 430 字的时间线事实被拆成两批，第 1 章与第 4 章不在同一批
    _run, outcome = await _check(app, fake_llm, user, project, scope, comparison(), comparison(), budget=1000)
    assert (outcome.batches, outcome.failed_batches, outcome.stats.not_reproduced) == (2, 0, 0)
    assert outcome.warnings == [
        "部分分组的事实超过单批上限，已拆成多批比对：跨批的冲突可能漏检，本次不标记「本次未复现」"]
    (issue,) = await _issues(app, project["id"])
    assert issue.last_checked_run_id == first_run and svc.not_reproduced(issue) is False
    # 预算足够、整组一批比对时才标记
    second_run, outcome = await _check(app, fake_llm, user, project, scope, comparison())
    assert (outcome.batches, outcome.stats.not_reproduced, outcome.warnings) == (1, 1, [])
    (issue,) = await _issues(app, project["id"])
    assert issue.last_checked_run_id == second_run and svc.not_reproduced(issue) is True


def test_omitted_foreshadows_and_existing_issues_are_reported():
    """伏笔记录 200 条上限、已有问题每批 30 条上限与预算裁剪都不能静默：差额进入 warnings。"""
    def record(number: int) -> svc.ForeshadowRecord:
        return svc.ForeshadowRecord(id=number, title=f"伏笔{number}", content="内容", status="planted",
                                    actual_plant_chapter=None, planned_plant_chapter=None,
                                    actual_resolve_chapter=None, planned_resolve_chapter=None)

    facts = [_ref(1, 1, "1", "foreshadow", "预言"), _ref(2, 2, "1", "foreshadow", "预言")]
    existing = [svc.ExistingIssue(id=number, category="foreshadow", title=f"问题{number}", description="",
                                  chapter_ids=frozenset({1, 2})) for number in range(1, svc.MAX_EXISTING_PER_BATCH + 4)]
    data = svc.CompareInput(
        project_id=1, facts=tuple(facts), scope_ids=frozenset({1, 2}), covered_ids=frozenset({1, 2}), stale_outside=0,
        foreshadows=tuple(record(number) for number in range(1, svc.MAX_FORESHADOWS + 1)), existing=tuple(existing),
        foreshadow_total=svc.MAX_FORESHADOWS + 5,
    )
    (batch,) = svc.plan_batches(data, svc.group_facts(data), char_budget=200_000)
    assert (len(batch.foreshadows), batch.foreshadows_omitted) == (svc.MAX_FORESHADOWS, 5)
    assert len(batch.existing) == svc.MAX_EXISTING_PER_BATCH
    assert batch.existing_omitted == tuple(range(svc.MAX_EXISTING_PER_BATCH + 1, svc.MAX_EXISTING_PER_BATCH + 4))
    assert svc.batch_notices([batch]) == [
        "允许进入上下文的伏笔记录较多，有 5 条未能随比对发送，与它们相关的冲突可能漏检",
        "已有问题较多，有 3 条的摘要未能随比对发送；再次检出时只能按证据章节去重",
    ]
    # 预算裁剪同样计入：预算很小时伏笔记录只放得下一部分
    (tight,) = svc.plan_batches(data, svc.group_facts(data), char_budget=1000)
    assert 0 < len(tight.foreshadows) < svc.MAX_FORESHADOWS
    assert tight.foreshadows_omitted == svc.MAX_FORESHADOWS + 5 - len(tight.foreshadows)
    # 没有省略、没有拆批时不产生提示
    small = svc.CompareInput(project_id=1, facts=tuple(facts), scope_ids=frozenset({1, 2}),
                             covered_ids=frozenset({1, 2}), stale_outside=0, foreshadows=(record(1),),
                             existing=tuple(existing[:2]), foreshadow_total=1)
    assert svc.batch_notices(svc.plan_batches(small, svc.group_facts(small))) == []


async def test_invalid_fact_references_are_dropped(app, client, fake_llm):
    user, project, chapters = await _sword_world(app, client)
    _run, outcome = await _check(app, fake_llm, user, project, list(chapters.values()), comparison(
        # 一处有效、一处编造：剩下的证据不足两个章节，整条不保留
        _issue(_ev(chapters[5]), _ev(chapters[12], "99"), title="编造证据"),
        _issue(_ev(chapters[5]), {"chapter_id": 987654, "fact_id": "1"}, title="编造章节"),
        _issue(_ev(chapters[5]), _ev(chapters[5]), title="同一章"),
        # 两处有效加一处编造：保留有效的两处；类别与严重程度不合法时回退
        _issue(_ev(chapters[5]), _ev(chapters[12]), {"chapter_id": "x"}, "乱写", {"fact_id": "1"},
               title="", category="weapon", severity="致命", description="前后出入"),
        "不是对象",
        _issue(_ev(chapters[5]), _ev(chapters[12]), title="", description=""),
    ))
    assert outcome.warnings == ["有 5 处证据引用了不存在的事实，已丢弃", "有 3 个问题的证据不足两个章节，已忽略",
                                "有 2 个问题格式不正确，已忽略"]
    (issue,) = await _issues(app, project["id"])
    assert (issue.title, issue.category, issue.severity) == ("前后出入", "item", "medium")
    assert [item["chapter_id"] for item in issue.evidence] == [chapters[5]["id"], chapters[12]["id"]]
    # 没有问题列表的输出不算失败
    _run, outcome = await _check(app, fake_llm, user, project, list(chapters.values()), '{"result": "无"}')
    assert (outcome.failed_batches, outcome.warnings) == (0, ["模型输出中没有问题列表"])


async def test_foreshadow_group_carries_context_records_only(app, client, fake_llm):
    planted = "老者留下一句预言：十年后剑将重铸。"
    user, project, chapters = await seed(client, {3: planted})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[3], [
            fact("1", planted, "十年后剑将重铸", category="foreshadow", subject="预言")])
        shown = Foreshadow(project_id=project["id"], title="重铸预言", content="老者预言\n佩剑重铸", status="planted",
                           actual_plant_chapter=8, planned_resolve_chapter=40, include_in_context=True)
        hidden = Foreshadow(project_id=project["id"], title="不进上下文的伏笔", content="隐藏内容", status="pending",
                            include_in_context=False)
        db.add_all([shown, hidden])
        await db.commit()
        shown_id, hidden_id = shown.id, hidden.id
    _run, outcome = await _check(app, fake_llm, user, project, list(chapters.values()), comparison(
        _issue(_ev(chapters[3]), foreshadow_id=shown_id, category="item", title="预言早于伏笔记录的埋设章"),
        _issue(_ev(chapters[3]), foreshadow_id=hidden_id, title="指向未提供的伏笔"),
        _issue(_ev(chapters[3]), title="单章且没有伏笔记录"),
    ))
    sent = fake_llm.calls[-1]["messages"][1]["content"]
    assert f"#{shown_id} 重铸预言｜老者预言 佩剑重铸｜已埋设｜埋设章：第8章｜回收章：计划第40章" in sent
    assert "不进上下文的伏笔" not in sent and "隐藏内容" not in sent and "foreshadow_id 填写" in sent
    assert outcome.warnings == ["有 2 个问题的证据不足两个章节，已忽略"]
    (issue,) = await _issues(app, project["id"])
    # 章内与伏笔记录矛盾：允许只有一个章节的证据，类别归为伏笔
    assert (issue.category, issue.title, len(issue.evidence)) == ("foreshadow", "预言早于伏笔记录的埋设章", 1)
    assert issue.identity_key == svc.identity_key("foreshadow", f"预言#foreshadow:{shown_id}", [chapters[3]["id"]])


async def test_issue_flags_are_computed_in_batch(app, client, fake_llm):
    """AC4：证据章节正文变化或章节被删除 → 需重新核查，且不逐条查库。"""
    user, project, chapters = await _sword_world(app, client, {20: LATER_A, 21: LATER_B})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[20], [fact("1", LATER_A, "佩剑仍挂在祠堂")])
        await add_ledger(db, project["id"], chapters[21], [fact("1", LATER_B, "从来没有挂过佩剑")])
        await db.commit()
    await _check(app, fake_llm, user, project, list(chapters.values()), comparison(
        _issue(_ev(chapters[5]), _ev(chapters[12])),
        _issue(_ev(chapters[20]), _ev(chapters[21]), title="祠堂"),
        _issue(_ev(chapters[5]), _ev(chapters[21]), title="保持不变"),
    ))
    edited = await client.patch(f"{API}/{project['id']}/chapters/{chapters[12]['id']}", json={"content": USED + "丙"})
    assert edited.status_code == 200, edited.text
    assert (await client.delete(f"{API}/{project['id']}/chapters/{chapters[20]['id']}")).status_code == 204
    rows = await _issues(app, project["id"])
    assert len(rows) == 3
    statements = []
    async with app.state.sessionmaker() as db:
        from sqlalchemy import event

        def record(_conn, _cursor, statement, *_args):
            statements.append(statement)

        engine = app.state.engine.sync_engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            flags = await svc.issue_flags(db, project["id"], rows)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        orphan = [{**rows[2].evidence[0], "chapter_id": None}]
        assert svc.needs_recheck(orphan, await svc.chapter_text_hashes(db, project["id"], [])) is True
        # 其他作品查不到本作品的章节：全部视为失效
        assert (await svc.issue_flags(db, project["id"] + 999, rows))[rows[2].id].stale == (True, True)
    assert len(statements) == 1
    sword, hall, same = (flags[row.id] for row in rows)
    assert (sword.needs_recheck, sword.stale) == (True, (False, True))
    assert (hall.needs_recheck, hall.stale) == (True, (True, False))
    assert (same.needs_recheck, same.stale, same.not_reproduced) == (False, (False, False), False)
    assert svc.needs_recheck([], {}) is False


def _ref(chapter_id: int, chapter_no: int, fact_id: str, category: str, subject: str, *, in_scope=True,
         statement="陈述") -> svc.FactRef:
    return svc.FactRef(chapter_id=chapter_id, chapter_no=chapter_no, chapter_title="", text_hash="a" * 64,
                       in_scope=in_scope, fact={"id": fact_id, "category": category, "subject": subject,
                                                "subject_key": subject, "statement": statement, "time_hint": "",
                                                "quote": "", "start": None, "end": None})


def _input(facts, *, foreshadows=(), existing=()) -> svc.CompareInput:
    return svc.CompareInput(project_id=1, facts=tuple(facts), scope_ids=frozenset(), covered_ids=frozenset(),
                            stale_outside=0, foreshadows=tuple(foreshadows), existing=tuple(existing))


def test_grouping_and_batching():
    record = svc.ForeshadowRecord(id=7, title="预言", content="内容", status="planted", actual_plant_chapter=None,
                                  planned_plant_chapter=None, actual_resolve_chapter=None,
                                  planned_resolve_chapter=None)
    facts = [
        _ref(1, 1, "1", "item", "佩剑"), _ref(1, 1, "2", "character_state", "林远"), _ref(1, 1, "3", "timeline", "春"),
        _ref(1, 1, "4", "foreshadow", "预言"), _ref(1, 1, "5", "item", ""),
        _ref(2, 2, "1", "character_history", "林远"), _ref(2, 2, "2", "timeline", "秋"), _ref(2, 2, "3", "item", ""),
        _ref(2, 2, "4", "item", "长枪"), _ref(3, 9, "1", "item", "佩剑", in_scope=False),
        _ref(3, 9, "2", "item", "玉佩", in_scope=False), _ref(4, 10, "1", "item", "玉佩", in_scope=False),
    ]
    data = _input(facts, foreshadows=[record])
    groups = svc.group_facts(data)
    # 同一主体的经历与状态合并比对；时间线、伏笔各自成组；没有主体的按类别兜底；
    # 只有一个章节的组（长枪）与完全在范围外的组（玉佩）不参与
    assert {group.key: [ref.key for ref in group.facts] for group in groups} == {
        "subject:佩剑": [(1, "1"), (3, "1")], "subject:林远": [(1, "2"), (2, "1")],
        "timeline": [(1, "3"), (2, "2")], "foreshadow": [(1, "4")], "category:item": [(1, "5"), (2, "3")],
    }
    # 没有伏笔记录时，只有一个章节的伏笔组也不参与
    assert "foreshadow" not in {group.key for group in svc.group_facts(_input(facts))}

    issue_in = svc.ExistingIssue(id=4, category="item", title="佩剑", description="", chapter_ids=frozenset({1, 3}))
    issue_out = svc.ExistingIssue(id=5, category="item", title="别处", description="", chapter_ids=frozenset({1, 99}))
    issue_cat = svc.ExistingIssue(id=6, category="foreshadow", title="伏笔", description="", chapter_ids=frozenset({1}))
    orphan = svc.ExistingIssue(id=8, category="item", title="无章节", description="", chapter_ids=frozenset())
    data = _input(facts, foreshadows=[record], existing=[issue_in, issue_out, issue_cat, orphan])
    (whole,) = svc.plan_batches(data, groups, char_budget=12_000)
    assert len(whole.facts) == 9 and whole.foreshadows == (record,)
    assert [issue.id for issue in whole.existing] == [4, 6]
    messages = svc.build_compare_messages(whole)
    assert "【伏笔记录】" in messages[1]["content"] and "#4 物品去向｜佩剑｜｜1、3" in messages[1]["content"]

    # 预算内分批：小组整组不拆；超过预算的大组按章节顺序切成连续几批
    big = [_ref(chapter, chapter, "1", "timeline", "时", statement="长" * 400) for chapter in range(1, 8)]
    small = [_ref(1, 1, "9", "item", "剑"), _ref(2, 2, "9", "item", "剑")]
    data = _input(small + big, foreshadows=[record])
    batches = svc.plan_batches(data, svc.group_facts(data), char_budget=1000)
    assert [batch.index for batch in batches] == list(range(len(batches))) and len(batches) == 5
    assert batches[0].group_keys == ("subject:剑",) and [ref.key for ref in batches[0].facts] == [(1, "9"), (2, "9")]
    assert all(batch.group_keys == ("timeline",) and batch.foreshadows == () for batch in batches[1:])
    assert [ref.chapter_id for batch in batches[1:] for ref in batch.facts] == list(range(1, 8))
    assert all(sum(len(svc.fact_line(ref)) + 1 for ref in batch.facts) <= 1000 for batch in batches)
    assert svc.plan_batches(_input([]), [], char_budget=1000) == []


async def test_nothing_to_compare_makes_no_model_call(app, client, fake_llm):
    user, project, chapters = await seed(client, {1: BROKEN})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[1], [fact("1", BROKEN, "佩剑在山门前断成两截")])
        await db.commit()
    _run, outcome = await _check(app, fake_llm, user, project, list(chapters.values()))
    assert (outcome.batches, len(fake_llm.calls)) == (0, 0)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(ConsistencyIssue)) == 0
