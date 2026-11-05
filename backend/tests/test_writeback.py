"""分析回写设定：自动写入与待确认分流、名字对不上只提示、撤销与冲突、更晚章节优先、单条失败。"""

from sqlalchemy import select

from app.models import SettingProgression, SettingTimeline
from tests.helpers import analysis_json, create_ai_config, create_project, register, run_task


async def _world(client):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, title="剑道", genre="仙侠")
    base = f"/api/v1/projects/{p['id']}"

    async def char(name, **kw):
        resp = await client.post(f"{base}/characters", json={"name": name, **kw})
        assert resp.status_code == 201, resp.text
        return resp.json()

    c = {
        "林远": await char("林远", role_type="主角", current_state="初入山门", aliases=["小远"]),
        "张三": await char("张三"),
        "苏晚": await char("苏晚"),
        "王五": await char("王五"),
        "李四": await char("李四"),
    }
    rel = (
        await client.post(
            f"{base}/relationships",
            json={"a_id": c["林远"]["id"], "b_id": c["张三"]["id"], "role_a": "朋友", "intimacy": 60},
        )
    ).json()
    career = (
        await client.post(
            f"{base}/careers", json={"name": "剑修", "stages": [{"name": n} for n in ("练气", "筑基", "金丹")]}
        )
    ).json()
    stages = {s["name"]: s["id"] for s in career["stages"]}
    await client.post(
        f"{base}/characters/{c['林远']['id']}/careers", json={"career_id": career["id"], "stage_id": stages["筑基"]}
    )
    org = (await client.post(f"{base}/organizations", json={"name": "青云门"})).json()
    chapters = {}
    for no in (3, 5):
        chapters[no] = (
            await client.post(
                f"{base}/chapters",
                json={"title": f"章{no}", "chapter_no": no, "content": f"第{no}章：林远、张三、苏晚、王五、李四登场。"},
            )
        ).json()
    return base, c, rel, career, stages, org, chapters


CHANGES = {
    "character_changes": [
        {"name": "小远", "current_state": "重伤"},
        {"name": "苏晚", "life_status": "dead"},
        {"name": "赵六", "current_state": "路过"},
    ],
    "relationship_changes": [
        {"a": "林远", "b": "张三", "intimacy": 20, "status": "broken"},
        {"a": "林远", "b": "王五", "new_relation": {"role_a": "宿敌", "role_b": "宿敌", "category": "hostile"}},
    ],
    "career_changes": [{"name": "林远", "career": "剑修", "stage": "金丹"}],
    "organization_changes": [{"name": "李四", "organization": "青云门", "action": "join", "position": "外门弟子"}],
}


async def _analyze(client, fake_llm, chapter, **changes):
    # 伏笔回写另见 test_foreshadow_analysis.py；这里只关注设定变化
    changes.setdefault("foreshadows", [])
    fake_llm.push(analysis_json(**changes))
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/analyze"))
    assert task["status"] == "succeeded", task
    aid = (await client.get(f"/api/v1/chapters/{chapter['id']}/analyses")).json()[0]["id"]
    return (await client.get(f"/api/v1/analyses/{aid}")).json()["changes"]


def _by(changes, kind, **match):
    return [c for c in changes if c["kind"] == kind and all(c["target"].get(k) == v for k, v in match.items())]


async def _edit_source_event(app, client, base, change, value):
    async with app.state.sessionmaker() as db:
        event = await db.scalar(select(SettingProgression).where(SettingProgression.source_change_id == change["id"]))
        timeline = await db.get(SettingTimeline, event.timeline_id)
        event_id, timeline_id, revision = event.id, timeline.id, timeline.revision
    response = await client.patch(
        f"{base}/setting-timelines/{timeline_id}/progressions/{event_id}",
        json={"expected_revision": revision, "value": value},
    )
    assert response.status_code == 200, response.text


async def test_writeback_auto_pending_and_unmatched(app, client, fake_llm):
    """AC4：常规变化自动写入（来源 analysis + 章节号），重大变化待确认，名字对不上只提示。"""
    base, c, rel, career, stages, org, chapters = await _world(client)
    changes = await _analyze(client, fake_llm, chapters[3], **CHANGES)
    statuses = sorted((x["kind"], x.get("target", {}).get("field"), x["status"]) for x in changes)
    assert statuses == sorted(
        [
            ("character_state", None, "applied"),
            ("character_state", None, "skipped"),
            ("life_status", None, "pending"),
            ("relationship_update", "intimacy", "applied"),
            ("relationship_update", "status", "applied"),
            ("relationship_create", None, "pending"),
            ("career_stage", None, "applied"),
            ("org_membership", None, "applied"),
        ]
    )
    skipped = next(x for x in changes if x["status"] == "skipped")
    assert skipped["reason"] == "未找到名为「赵六」的角色" and skipped["field_key"] == ""
    state = _by(changes, "character_state", character_id=c["林远"]["id"])[0]
    assert (state["before"], state["after"], state["field_key"]) == (
        "初入山门",
        "重伤",
        f"char:{c['林远']['id']}:current_state",
    )

    # 角色状态
    assert (await client.get(f"{base}/characters/{c['林远']['id']}")).json()["current_state"] == "重伤"
    assert (await client.get(f"{base}/characters/{c['苏晚']['id']}")).json()["life_status"] == "alive"
    # 关系：历史来源 analysis + 章节号，不改类型
    history = (await client.get(f"{base}/relationships/{rel['id']}/history")).json()
    assert [(h["source"], h["chapter_no"], h["changes"]) for h in history[:2]] == [
        ("analysis", 3, {"status": ["normal", "broken"]}),
        ("analysis", 3, {"intimacy": [60, 20]}),
    ]
    current = next(r for r in (await client.get(f"{base}/relationships")).json() if r["id"] == rel["id"])
    assert (current["intimacy"], current["status"], current["type_id"]) == (20, "broken", rel["type_id"])
    # 境界
    hist = (await client.get(f"{base}/characters/{c['林远']['id']}/career-history")).json()[0]
    assert (hist["source"], hist["chapter_no"]) == ("analysis", 3)
    assert hist["changes"] == {"stage": ["第2阶·筑基", "第3阶·金丹"]}
    # 组织成员
    members = (await client.get(f"{base}/characters/{c['李四']['id']}/memberships")).json()
    assert [(m["organization_id"], m["position"], m["status"]) for m in members] == [(org["id"], "外门弟子", "active")]
    org_change = _by(changes, "org_membership")[0]
    assert org_change["before"] is None and org_change["after"] == {"position": "外门弟子", "status": "active"}
    # 王五与林远之间没有关系：待确认前不写入
    rels = (await client.get(f"{base}/relationships", params={"character_id": c["王五"]["id"]})).json()
    assert rels == []

    # 确认：苏晚死亡、新建宿敌关系
    death = _by(changes, "life_status")[0]
    resp = await client.post(f"/api/v1/analysis-changes/{death['id']}/confirm")
    assert resp.status_code == 200 and resp.json()["status"] == "applied" and resp.json()["applied_at"]
    assert (await client.get(f"{base}/characters/{c['苏晚']['id']}")).json()["life_status"] == "dead"
    create = _by(changes, "relationship_create")[0]
    resp = await client.post(f"/api/v1/analysis-changes/{create['id']}/confirm")
    assert resp.status_code == 200, resp.text
    new_rel_id = resp.json()["target"]["relationship_id"]
    history = (await client.get(f"{base}/relationships/{new_rel_id}/history")).json()
    assert (history[0]["source"], history[0]["chapter_no"]) == ("analysis", 3)
    # 已确认的条目不能再确认 / 拒绝
    resp = await client.post(f"/api/v1/analysis-changes/{create['id']}/confirm")
    assert resp.status_code == 409 and resp.json()["detail"] == "只能确认待确认的条目"
    resp = await client.post(f"/api/v1/analysis-changes/{create['id']}/reject")
    assert resp.status_code == 409 and resp.json()["detail"] == "只能拒绝待确认的条目"
    # 撤销新建的关系：关系被删除
    resp = await client.post(f"/api/v1/analysis-changes/{create['id']}/revert")
    assert resp.status_code == 200 and resp.json()["status"] == "reverted"
    assert (await client.get(f"{base}/relationships", params={"character_id": c["王五"]["id"]})).json() == []

    # 重跑同一章需转移来源；旧来源不能撤销新结果。
    again = await _analyze(client, fake_llm, chapters[3], **CHANGES)
    kinds = sorted((x["kind"], x["status"]) for x in again)
    assert ("character_state", "applied") in kinds and ("career_stage", "applied") in kinds
    assert (await client.post(f"/api/v1/analysis-changes/{state['id']}/revert")).status_code == 409
    assert ("relationship_create", "pending") in kinds  # 关系已被撤销，重新进入待确认


async def test_reject_and_confirm_rechecks(app, client, fake_llm):
    base, c, rel, *_, chapters = await _world(client)
    changes = await _analyze(client, fake_llm, chapters[3], **CHANGES)
    death = _by(changes, "life_status")[0]
    create = _by(changes, "relationship_create")[0]
    resp = await client.post(f"/api/v1/analysis-changes/{death['id']}/reject")
    assert resp.status_code == 200 and resp.json()["status"] == "rejected" and resp.json()["resolved_at"]
    assert (await client.get(f"{base}/characters/{c['苏晚']['id']}")).json()["life_status"] == "alive"
    # 确认前关系已被手动建立 → 409
    manual = await client.post(
        f"{base}/relationships", json={"a_id": c["林远"]["id"], "b_id": c["王五"]["id"], "role_a": "朋友"}
    )
    assert manual.status_code == 201, manual.text
    resp = await client.post(f"/api/v1/analysis-changes/{create['id']}/confirm")
    assert resp.status_code == 409 and resp.json()["detail"] == "该项对应的设定已被修改，无法确认"
    detail = (await client.get(f"/api/v1/analyses/{create['analysis_id']}")).json()
    assert next(x for x in detail["changes"] if x["id"] == create["id"])["status"] == "pending"

    # 待确认期间存活状态被手动修改 → 确认被拒绝
    changes = await _analyze(
        client, fake_llm, chapters[5], character_changes=[{"name": "苏晚", "life_status": "missing"}]
    )
    missing = _by(changes, "life_status")[0]
    await client.patch(f"{base}/characters/{c['苏晚']['id']}", json={"life_status": "other"})
    resp = await client.post(f"/api/v1/analysis-changes/{missing['id']}/confirm")
    assert resp.status_code == 409 and resp.json()["detail"] == "该项对应的设定已被修改，无法确认"


async def test_revert_and_conflict(app, client, fake_llm):
    """AC5：撤销恢复原值并新增历史；已被手动改过 → 409。"""
    base, c, rel, career, stages, org, chapters = await _world(client)
    changes = await _analyze(client, fake_llm, chapters[3], **CHANGES)
    intimacy = _by(changes, "relationship_update", field="intimacy")[0]
    status = _by(changes, "relationship_update", field="status")[0]

    resp = await client.post(f"/api/v1/analysis-changes/{intimacy['id']}/revert")
    assert resp.status_code == 200 and resp.json()["status"] == "reverted"
    current = next(r for r in (await client.get(f"{base}/relationships")).json() if r["id"] == rel["id"])
    assert current["intimacy"] == 60
    history = (await client.get(f"{base}/relationships/{rel['id']}/history")).json()
    assert (history[0]["changes"], history[0]["source"], history[0]["note"]) == (
        {"intimacy": [20, 60]},
        "analysis",
        "撤销第 3 章分析",
    )
    resp = await client.post(f"/api/v1/analysis-changes/{intimacy['id']}/revert")
    assert resp.status_code == 409 and resp.json()["detail"] == "只能撤销已写入的条目"

    # 状态已被手动修改 → 撤销被拒绝
    await _edit_source_event(app, client, base, status, "complicated")
    resp = await client.post(f"/api/v1/analysis-changes/{status['id']}/revert")
    assert resp.status_code == 409 and resp.json()["detail"] == "该项已被后续修改，无法撤销"

    # 境界、组织、角色状态撤销
    for kind in ("career_stage", "org_membership", "character_state"):
        item = next(x for x in changes if x["kind"] == kind and x["status"] == "applied")
        resp = await client.post(f"/api/v1/analysis-changes/{item['id']}/revert")
        assert resp.status_code == 200, (kind, resp.text)
    assignments = (await client.get(f"{base}/characters/{c['林远']['id']}/careers")).json()
    assert assignments[0]["stage_id"] == stages["筑基"]
    assert (await client.get(f"{base}/characters/{c['李四']['id']}/memberships")).json() == []
    assert (await client.get(f"{base}/characters/{c['林远']['id']}")).json()["current_state"] == "初入山门"
    hist = (await client.get(f"{base}/characters/{c['林远']['id']}/career-history")).json()[0]
    assert hist["changes"] == {"stage": ["第3阶·金丹", "第2阶·筑基"]} and hist["note"] == "撤销第 3 章分析"


async def test_later_chapter_wins(app, client, fake_llm):
    """先分析第 5 章再补录第 3 章，两处均保留且最新状态遵循故事顺序。"""
    base, c, rel, career, stages, org, chapters = await _world(client)
    ch5 = await _analyze(
        client,
        fake_llm,
        chapters[5],
        character_changes=[{"name": "林远", "current_state": "金丹大成"}],
        relationship_changes=[{"a": "张三", "b": "林远", "intimacy": 90}],
    )
    assert {x["status"] for x in ch5} == {"applied"}
    ch3 = await _analyze(
        client,
        fake_llm,
        chapters[3],
        character_changes=[{"name": "林远", "current_state": "重伤"}],
        relationship_changes=[{"a": "林远", "b": "张三", "intimacy": 20, "description": "心生嫌隙"}],
    )
    by_field = {(x["kind"], x["target"].get("field")): x for x in ch3}
    assert by_field[("character_state", None)]["status"] == "applied"
    assert by_field[("character_state", None)]["before"] == "初入山门"
    assert by_field[("relationship_update", "intimacy")]["status"] == "applied"
    assert by_field[("relationship_update", "intimacy")]["before"] == 60
    # 第 5 章没写过的字段照常写入
    assert by_field[("relationship_update", "description")]["status"] == "applied"
    assert (await client.get(f"{base}/characters/{c['林远']['id']}")).json()["current_state"] == "金丹大成"
    current = next(r for r in (await client.get(f"{base}/relationships")).json() if r["id"] == rel["id"])
    assert (current["intimacy"], current["description"]) == (90, "心生嫌隙")

    # 撤销第 5 章的写入后，第 3 章先前补录的变化自然成为最新值。
    state5 = next(x for x in ch5 if x["kind"] == "character_state")
    assert (await client.post(f"/api/v1/analysis-changes/{state5['id']}/revert")).status_code == 200
    assert (await client.get(f"{base}/characters/{c['林远']['id']}")).json()["current_state"] == "重伤"
    ch3b = await _analyze(client, fake_llm, chapters[3], character_changes=[{"name": "林远", "current_state": "重伤"}])
    assert [x["status"] for x in ch3b] == ["applied"]


async def test_career_and_org_skips(app, client, fake_llm):
    """境界只在已挂接职业内变化；职业 / 阶段 / 组织对不上只提示；未是成员的离开不生成条目。"""
    base, c, rel, career, stages, org, chapters = await _world(client)
    changes = await _analyze(
        client,
        fake_llm,
        chapters[3],
        career_changes=[
            {"name": "张三", "career": "剑修", "stage": "金丹"},
            {"name": "林远", "career": "剑修", "stage": "元婴"},
            {"name": "林远", "career": "丹师", "stage": "一品"},
            {"name": "林远", "career": "剑修", "stage": "筑基"},
        ],
        organization_changes=[
            {"name": "李四", "organization": "天魔宗", "action": "join"},
            {"name": "张三", "organization": "青云门", "action": "leave"},
        ],
    )
    reasons = sorted(x["reason"] for x in changes)
    assert reasons == sorted(
        [
            "「张三」未挂接职业「剑修」",
            "职业「剑修」没有名为「元婴」的阶段",
            "未找到名为「丹师」的职业",
            "未找到名为「天魔宗」的组织",
        ]
    )
    assert {x["status"] for x in changes} == {"skipped"}


async def test_savepoint_failure_marks_failed(app, client, fake_llm, monkeypatch):
    base, c, rel, career, stages, org, chapters = await _world(client)
    from app.core.errors import BadRequestError
    from app.services import setting_progression_service

    original = setting_progression_service.materialize_timeline

    async def boom(db, timeline):
        if timeline.subject_kind == "membership":
            raise BadRequestError("成员写入失败")
        await original(db, timeline)

    monkeypatch.setattr(setting_progression_service, "materialize_timeline", boom)
    changes = await _analyze(
        client,
        fake_llm,
        chapters[3],
        character_changes=[{"name": "林远", "current_state": "重伤"}],
        organization_changes=[{"name": "李四", "organization": "青云门", "action": "join"}],
        relationship_changes=[{"a": "林远", "b": "张三", "intimacy": 10}],
    )
    by_kind = {x["kind"]: x for x in changes}
    assert by_kind["org_membership"]["status"] == "failed" and by_kind["org_membership"]["reason"] == "成员写入失败"
    assert by_kind["character_state"]["status"] == "applied"
    assert by_kind["relationship_update"]["status"] == "applied"
    assert (await client.get(f"{base}/characters/{c['林远']['id']}")).json()["current_state"] == "重伤"


async def test_revert_conflicts_for_every_kind(app, client, fake_llm):
    """人工编辑原事件后，旧分析不得撤销覆盖角色、职业和组织成员事件。"""
    base, c, rel, career, stages, org, chapters = await _world(client)
    changes = await _analyze(client, fake_llm, chapters[3], **CHANGES)
    state = _by(changes, "character_state", character_id=c["林远"]["id"])[0]
    stage = _by(changes, "career_stage")[0]
    member = _by(changes, "org_membership")[0]

    await _edit_source_event(app, client, base, state, "伤愈")
    await _edit_source_event(app, client, base, stage, stages["练气"])
    await _edit_source_event(
        app, client, base, member,
        {"exists": True, "state": {"position": "内门弟子", "status": "active", "note": ""}},
    )

    for item in (state, stage, member):
        resp = await client.post(f"/api/v1/analysis-changes/{item['id']}/revert")
        assert resp.status_code == 409 and resp.json()["detail"] == "该项已被后续修改，无法撤销", item["kind"]
    assert (await client.get(f"{base}/characters/{c['林远']['id']}")).json()["current_state"] == "伤愈"
    assert (await client.get(f"{base}/characters/{c['林远']['id']}/careers")).json()[0]["stage_id"] == stages["练气"]
    members = (await client.get(f"{base}/characters/{c['李四']['id']}/memberships")).json()
    assert [mm["position"] for mm in members] == ["内门弟子"]


async def test_confirm_rejected_when_later_chapter_applied(app, client, fake_llm):
    """待确认期间主体版本改变时拒绝过期确认；重新分析后仍可补录早章。"""
    base, c, rel, career, stages, org, chapters = await _world(client)
    ch3 = await _analyze(client, fake_llm, chapters[3], character_changes=[{"name": "苏晚", "life_status": "dead"}])
    pending3 = _by(ch3, "life_status")[0]
    ch5 = await _analyze(client, fake_llm, chapters[5], character_changes=[{"name": "苏晚", "life_status": "missing"}])
    pending5 = _by(ch5, "life_status")[0]
    assert pending5["status"] == "pending"
    resp = await client.post(f"/api/v1/analysis-changes/{pending5['id']}/confirm")
    assert resp.status_code == 200
    resp = await client.post(f"/api/v1/analysis-changes/{pending3['id']}/confirm")
    assert resp.status_code == 409 and resp.json()["detail"] == "该项对应的设定已被修改，无法确认"
    assert (await client.get(f"{base}/characters/{c['苏晚']['id']}")).json()["life_status"] == "missing"
    detail = (await client.get(f"/api/v1/analyses/{pending3['analysis_id']}")).json()
    assert next(x for x in detail["changes"] if x["id"] == pending3["id"])["status"] == "pending"
    fresh = await _analyze(client, fake_llm, chapters[3], character_changes=[{"name": "苏晚", "life_status": "dead"}])
    fresh_id = _by(fresh, "life_status")[0]["id"]
    assert (await client.post(f"/api/v1/analysis-changes/{fresh_id}/confirm")).status_code == 200
    assert (await client.get(f"{base}/characters/{c['苏晚']['id']}")).json()["life_status"] == "missing"


async def test_savepoint_rolls_back_partial_write(app, client, fake_llm, monkeypatch):
    """单条写入在部分落库后失败：SAVEPOINT 回滚该条的全部写入，其他条目照常提交。"""
    base, c, rel, career, stages, org, chapters = await _world(client)
    from app.core.errors import BadRequestError
    from app.models import Character
    from app.services import setting_progression_service

    original = setting_progression_service.materialize_timeline

    async def half_then_fail(db, timeline):
        await original(db, timeline)
        if timeline.subject_kind != "membership":
            return
        lin = await db.get(Character, c["林远"]["id"])
        lin.current_state = "脏数据"
        await db.flush()
        raise BadRequestError("成员写入失败")

    monkeypatch.setattr(setting_progression_service, "materialize_timeline", half_then_fail)
    changes = await _analyze(
        client,
        fake_llm,
        chapters[3],
        character_changes=[{"name": "林远", "current_state": "重伤"}],
        organization_changes=[{"name": "李四", "organization": "青云门", "action": "join"}],
    )
    by_kind = {x["kind"]: x for x in changes}
    assert (by_kind["org_membership"]["status"], by_kind["character_state"]["status"]) == ("failed", "applied")
    assert (await client.get(f"{base}/characters/{c['林远']['id']}")).json()["current_state"] == "重伤"
