"""伏笔与章节分析联动：提示词清单（无伏笔时逐字不变）、按编号回收 / 埋设（自动、可撤销、更晚章节优先）、
新伏笔待确认（overrides、同名 409、撤销即删除）、无效编号只提示。"""

import hashlib

from app.ai.prompts import build_analysis_messages
from app.ai.prompts.common import ProjectInfo
from app.services.analysis_service import normalize_analysis
from tests.helpers import analysis_json, create_ai_config, create_project, register, run_task

# 引入伏笔管理之前（HEAD）的分析提示词摘要：没有伏笔时提示词必须逐字一致
NO_FORESHADOW_PROMPT_SHA = "bab263553390fa56bb41eea82337c1c1b7379b8ceb562f692eb8b35b69ac33bd"


async def _setup(client, chapter_nos=(3, 5, 8)):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, title="剑道", genre="仙侠")
    base = f"/api/v1/projects/{p['id']}"
    lin = (await client.post(f"{base}/characters", json={"name": "林远", "role_type": "主角"})).json()
    chapters = {}
    for no in chapter_nos:
        chapters[no] = (
            await client.post(
                f"{base}/chapters", json={"title": f"章{no}", "chapter_no": no, "content": f"第{no}章：林远登场。"}
            )
        ).json()
    return base, lin, chapters


async def _fs(client, base, **body):
    resp = await client.post(f"{base}/foreshadows", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _analyze(client, fake_llm, chapter, foreshadows):
    fake_llm.push(analysis_json(foreshadows=foreshadows))
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/analyze"))
    assert task["status"] == "succeeded", task
    aid = (await client.get(f"/api/v1/chapters/{chapter['id']}/analyses")).json()[0]["id"]
    return (await client.get(f"/api/v1/analyses/{aid}")).json()["changes"]


def _kind(changes, kind, status=None):
    return [c for c in changes if c["kind"] == kind and (status is None or c["status"] == status)]


def _prompt(fake_llm) -> str:
    return fake_llm.calls[-1]["messages"][1]["content"]


def test_prompt_without_foreshadows_is_unchanged():
    messages = build_analysis_messages(ProjectInfo(title="T"), chapter_no=1, title="", content="X", cast_text="")
    digest = hashlib.sha256(messages[0]["content"].encode() + b"\x00" + messages[1]["content"].encode()).hexdigest()
    assert digest == NO_FORESHADOW_PROMPT_SHA
    assert messages == build_analysis_messages(
        ProjectInfo(title="T"), chapter_no=1, title="", content="X", cast_text="", foreshadows_text="  "
    )
    with_refs = build_analysis_messages(
        ProjectInfo(title="T"), chapter_no=1, title="", content="X", cast_text="", foreshadows_text="#1 玉佩｜已埋"
    )[1]["content"]
    assert "【已有伏笔】" in with_refs and '"ref_id"' in with_refs and '"partial"' in with_refs
    assert "8. foreshadows" in with_refs


def test_normalize_foreshadows():
    data, warnings = normalize_analysis(
        {
            "foreshadows": [
                {"action": "resolve", "ref_id": 5, "text": "旧伤", "partial": True},
                {"action": "plant", "ref_id": "#7", "text": "玉佩"},
                {"action": "埋下", "ref_id": "8", "keyword": "信"},
                {"action": "resolve", "ref_id": True, "text": "a"},
                {"action": "resolve", "ref_id": 1.5, "text": "b"},
                {"action": "resolve", "ref_id": "abc", "text": "c", "expected_resolve_chapter": "12"},
                {"action": "???", "text": "d"},
                {"action": "plant"},
            ]
        }
    )
    fs = data["foreshadows"]
    assert [(f["action"], f["ref_id"], f["partial"]) for f in fs] == [
        ("resolve", 5, True),
        ("plant", 7, False),
        ("plant", 8, False),
        ("resolve", None, False),
        ("resolve", None, False),
        ("resolve", None, False),
    ]
    assert fs[5]["expected_resolve_chapter"] == 12
    assert any("伏笔「d」的动作" in w for w in warnings)


async def test_prompt_lists_foreshadows(app, client, fake_llm):
    """R2：已埋 / 部分回收全部 + 计划埋设章 ≤ 本章（或未定）的待埋交给模型；已回收 / 废弃 / 将来才埋的不给。"""
    base, lin, chapters = await _setup(client)
    planted = await _fs(
        client,
        base,
        title="师父的旧伤",
        content="师父  左臂\n有旧伤",
        status="planted",
        actual_plant_chapter=1,
        planned_resolve_chapter=8,
    )
    pending = await _fs(client, base, title="玉佩", planned_plant_chapter=5)
    floating = await _fs(client, base, title="血脉", status="partial", actual_plant_chapter=1, actual_resolve_chapter=2)
    future = await _fs(client, base, title="将来", planned_plant_chapter=20)
    closed = await _fs(client, base, title="已收", status="resolved", actual_plant_chapter=1, actual_resolve_chapter=2)
    await _analyze(client, fake_llm, chapters[5], [])
    prompt = _prompt(fake_llm)
    section = prompt.split("【已有伏笔】")[1].split("【正文】")[0]
    assert f"#{planted['id']} 师父的旧伤｜师父 左臂 有旧伤｜已埋｜计划回收第8章" in section
    assert f"#{pending['id']} 玉佩｜待埋（计划第5章埋设）｜未定回收章" in section
    assert f"#{floating['id']} 血脉｜部分回收｜未定回收章" in section
    assert f"#{future['id']} " not in section and f"#{closed['id']} " not in section
    assert '"ref_id"' in prompt


async def test_prompt_unchanged_for_project_without_foreshadows(app, client, fake_llm):
    base, lin, chapters = await _setup(client)
    await _analyze(client, fake_llm, chapters[3], [])
    plain = _prompt(fake_llm)
    assert "【已有伏笔】" not in plain and "ref_id" not in plain and "8. foreshadows" not in plain
    # 只有不可引用的伏笔（已回收 / 将来才埋）时同样不变
    await _fs(client, base, title="已收", status="resolved", actual_plant_chapter=1, actual_resolve_chapter=2)
    await _fs(client, base, title="将来", planned_plant_chapter=20)
    await _analyze(client, fake_llm, chapters[3], [])
    assert _prompt(fake_llm) == plain


async def test_resolve_by_ref_and_revert(app, client, fake_llm):
    """AC2：#N 被第 8 章以编号回收 → 已回收、实际回收章 8，可撤销恢复为已埋；无效编号 / 无编号只提示。"""
    base, lin, chapters = await _setup(client)
    fs = await _fs(client, base, title="旧伤", status="planted", actual_plant_chapter=2, planned_resolve_chapter=8)
    changes = await _analyze(
        client,
        fake_llm,
        chapters[8],
        [
            {"action": "resolve", "ref_id": fs["id"], "text": "揭开旧伤", "keyword": "旧伤"},
            {"action": "resolve", "ref_id": 99999, "text": "不存在"},
            {"action": "resolve", "text": "神秘回收", "keyword": "神秘"},
        ],
    )
    applied = _kind(changes, "foreshadow_resolve", "applied")
    assert len(applied) == 1
    change = applied[0]
    assert change["field_key"] == f"fs:{fs['id']}:resolve"
    assert change["target"] == {"foreshadow_id": fs["id"], "title": "旧伤"}
    assert change["before"] == {"status": "planted", "actual_resolve_chapter": None}
    assert change["after"] == {"status": "resolved", "actual_resolve_chapter": 8}
    reasons = sorted(c["reason"] for c in _kind(changes, "foreshadow_resolve", "skipped"))
    assert reasons == ["伏笔 #99999 不存在或状态不符", "未引用伏笔编号，无法回收"]
    current = (await client.get(f"{base}/foreshadows/{fs['id']}")).json()
    assert (current["status"], current["actual_resolve_chapter"]) == ("resolved", 8)
    history = (await client.get(f"{base}/foreshadows/{fs['id']}/history")).json()
    assert (history[0]["source"], history[0]["chapter_no"], history[0]["note"]) == ("analysis", 8, "第 8 章分析")

    # 重复分析：已是该结果 → 不建条目
    again = await _analyze(client, fake_llm, chapters[8], [{"action": "resolve", "ref_id": fs["id"], "text": "x"}])
    assert _kind(again, "foreshadow_resolve") == []

    resp = await client.post(f"/api/v1/analysis-changes/{change['id']}/revert")
    assert resp.status_code == 200 and resp.json()["status"] == "reverted"
    current = (await client.get(f"{base}/foreshadows/{fs['id']}")).json()
    assert (current["status"], current["actual_resolve_chapter"]) == ("planted", None)
    history = (await client.get(f"{base}/foreshadows/{fs['id']}/history")).json()
    assert (history[0]["source"], history[0]["note"]) == ("analysis", "撤销第 8 章分析")

    # 部分回收；被后续修改后撤销 → 409
    changes = await _analyze(
        client, fake_llm, chapters[8], [{"action": "resolve", "ref_id": fs["id"], "text": "x", "partial": True}]
    )
    partial = _kind(changes, "foreshadow_resolve", "applied")[0]
    assert partial["after"] == {"status": "partial", "actual_resolve_chapter": 8}
    await client.post(f"{base}/foreshadows/{fs['id']}/resolve", json={"chapter_no": 9})
    resp = await client.post(f"/api/v1/analysis-changes/{partial['id']}/revert")
    assert resp.status_code == 409 and resp.json()["detail"] == "该项已被后续修改，无法撤销"


async def test_resolve_later_chapter_wins(app, client, fake_llm):
    base, lin, chapters = await _setup(client)
    fs = await _fs(client, base, title="旧伤", status="planted", actual_plant_chapter=2)
    await _analyze(client, fake_llm, chapters[8], [{"action": "resolve", "ref_id": fs["id"], "text": "x"}])
    changes = await _analyze(client, fake_llm, chapters[5], [{"action": "resolve", "ref_id": fs["id"], "text": "x"}])
    skipped = _kind(changes, "foreshadow_resolve", "skipped")
    assert [c["reason"] for c in skipped] == ["已有第 8 章的更新"]
    assert skipped[0]["field_key"] == f"fs:{fs['id']}:resolve"
    current = (await client.get(f"{base}/foreshadows/{fs['id']}")).json()
    assert current["actual_resolve_chapter"] == 8


async def test_plant_planned_and_new_foreshadow(app, client, fake_llm):
    """AC3：计划第 3 章埋的待埋伏笔被第 3 章以编号识别 → 已埋；新伏笔待确认，改标题后创建；同章同名 409。"""
    base, lin, chapters = await _setup(client)
    other_project = await create_project(client, title="别的书")
    foreign = await _fs(client, f"/api/v1/projects/{other_project['id']}", title="外", planned_plant_chapter=3)
    planned = await _fs(client, base, title="玉佩", planned_plant_chapter=3, planned_resolve_chapter=10)
    changes = await _analyze(
        client,
        fake_llm,
        chapters[3],
        [
            {"action": "plant", "ref_id": planned["id"], "text": "玉佩出现"},
            {"action": "plant", "ref_id": foreign["id"], "text": "跨项目"},
            {"action": "plant", "text": "神秘信件出现在桌上", "keyword": "神秘信件", "expected_resolve_chapter": 12},
        ],
    )
    plant = _kind(changes, "foreshadow_plant_planned", "applied")[0]
    assert plant["field_key"] == f"fs:{planned['id']}:plant"
    assert (plant["before"], plant["after"]) == (
        {"status": "pending", "actual_plant_chapter": None},
        {"status": "planted", "actual_plant_chapter": 3},
    )
    current = (await client.get(f"{base}/foreshadows/{planned['id']}")).json()
    assert (current["status"], current["actual_plant_chapter"]) == ("planted", 3)
    assert [c["reason"] for c in _kind(changes, "foreshadow_plant_planned", "skipped")] == [
        f"伏笔 #{foreign['id']} 不存在或状态不符"
    ]

    new = _kind(changes, "foreshadow_new", "pending")[0]
    assert new["field_key"] == "" and new["target"] == {"title": "神秘信件"}
    assert new["after"] == {
        "title": "神秘信件",
        "content": "神秘信件出现在桌上",
        "plant_hint": "",
        "planned_resolve_chapter": 12,
        "importance": 3,
    }
    # 确认前不创建
    assert len((await client.get(f"{base}/foreshadows")).json()) == 1
    resp = await client.post(
        f"/api/v1/analysis-changes/{new['id']}/confirm",
        json={"overrides": {"title": "师门密信", "importance": 5, "character_ids": [lin["id"]], "reveal_hint": "揭底"}},
    )
    assert resp.status_code == 200, resp.text
    confirmed = resp.json()
    assert confirmed["status"] == "applied" and confirmed["applied_at"]
    fid = confirmed["target"]["foreshadow_id"]
    created = (await client.get(f"{base}/foreshadows/{fid}")).json()
    assert (created["title"], created["status"], created["actual_plant_chapter"], created["source"]) == (
        "师门密信",
        "planted",
        3,
        "analysis",
    )
    assert (created["importance"], created["planned_resolve_chapter"], created["reveal_hint"]) == (5, 12, "揭底")
    assert created["character_ids"] == [lin["id"]]
    history = (await client.get(f"{base}/foreshadows/{fid}/history")).json()
    assert (history[0]["source"], history[0]["chapter_no"], history[0]["note"]) == ("analysis", 3, "确认第 3 章分析")

    # 重复分析出同一条新伏笔：确认时改成已有标题 → 409
    again = await _analyze(
        client, fake_llm, chapters[3], [{"action": "plant", "text": "神秘信件", "keyword": "神秘信件"}]
    )
    dup = _kind(again, "foreshadow_new", "pending")[0]
    resp = await client.post(
        f"/api/v1/analysis-changes/{dup['id']}/confirm", json={"overrides": {"title": " 师门密信 "}}
    )
    assert resp.status_code == 409 and resp.json()["detail"] == "本章已有同名伏笔"
    # 已有同名（标题 = keyword）伏笔时重复分析不再建条目
    resp = await client.post(f"/api/v1/analysis-changes/{dup['id']}/confirm")
    assert resp.status_code == 200
    third = await _analyze(
        client, fake_llm, chapters[3], [{"action": "plant", "text": "神秘信件", "keyword": "神秘信件"}]
    )
    assert _kind(third, "foreshadow_new") == []
    # 关联角色跨项目 → 400，条目仍待确认
    pending = (await _analyze(client, fake_llm, chapters[5], [{"action": "plant", "text": "新线索"}]))[0]
    resp = await client.post(
        f"/api/v1/analysis-changes/{pending['id']}/confirm", json={"overrides": {"character_ids": [99999]}}
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "关联角色不存在或不属于该项目"
    # overrides 对其他类型忽略
    resp = await client.post(f"/api/v1/analysis-changes/{plant['id']}/confirm", json={"overrides": {"title": "x"}})
    assert resp.status_code == 409 and resp.json()["detail"] == "只能确认待确认的条目"

    # 撤销已确认的新伏笔 = 删除；伏笔已被修改 → 409
    resp = await client.post(f"/api/v1/analysis-changes/{confirmed['id']}/revert")
    assert resp.status_code == 200 and resp.json()["status"] == "reverted"
    assert (await client.get(f"{base}/foreshadows/{fid}")).status_code == 404
    dup_id = (await client.get(f"/api/v1/analyses/{dup['analysis_id']}")).json()["changes"]
    dup_fid = next(c for c in dup_id if c["id"] == dup["id"])["target"]["foreshadow_id"]
    await client.patch(f"{base}/foreshadows/{dup_fid}", json={"note": "补充备注"})
    resp = await client.post(f"/api/v1/analysis-changes/{dup['id']}/revert")
    assert resp.status_code == 409 and resp.json()["detail"] == "该项已被后续修改，无法撤销"

    # 撤销自动埋设 → 恢复为待埋
    resp = await client.post(f"/api/v1/analysis-changes/{plant['id']}/revert")
    assert resp.status_code == 200
    current = (await client.get(f"{base}/foreshadows/{planned['id']}")).json()
    assert (current["status"], current["actual_plant_chapter"]) == ("pending", None)


async def test_reject_new_foreshadow(app, client, fake_llm):
    base, lin, chapters = await _setup(client)
    changes = await _analyze(
        client, fake_llm, chapters[3], [{"action": "plant", "text": "神秘玉佩", "keyword": "玉佩"}]
    )
    new = _kind(changes, "foreshadow_new", "pending")[0]
    resp = await client.post(f"/api/v1/analysis-changes/{new['id']}/reject")
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert (await client.get(f"{base}/foreshadows")).json() == []
