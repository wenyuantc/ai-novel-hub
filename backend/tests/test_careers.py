from sqlalchemy import func, select

from app.models import Career, CareerHistory, CareerStage, CharacterCareer
from tests.helpers import create_project, purge_trashed_project, register

SWORD_STAGES = ["练气", "筑基", "金丹", "元婴", "化神", "炼虚", "合体", "大乘", "渡劫"]


async def _count(app, model) -> int:
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def _setup(client):
    await register(client)
    p = await create_project(client)
    return f"/api/v1/projects/{p['id']}"


async def _career(client, base, name, kind="main", stages=SWORD_STAGES, **kw):
    resp = await client.post(
        f"{base}/careers", json={"name": name, "kind": kind, "stages": [{"name": s} for s in stages], **kw}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _char(client, base, name):
    resp = await client.post(f"{base}/characters", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stage(career, name):
    return next(s for s in career["stages"] if s["name"] == name)


async def test_career_crud_and_stage_sync(client):
    """AC1：9 阶剑修；被引用的阶段删除 409；重排后角色仍在原阶段；同名 409；阶段校验。"""
    base = await _setup(client)
    sword = await _career(client, base, "剑修", rules="以剑入道", description="御剑")
    assert [(s["position"], s["name"]) for s in sword["stages"]] == list(enumerate(SWORD_STAGES, start=1))
    assert (sword["kind"], sword["holder_count"], sword["rules"]) == ("main", 0, "以剑入道")

    lin = await _char(client, base, "林远")
    resp = await client.post(
        f"{base}/characters/{lin['id']}/careers",
        json={"career_id": sword["id"], "stage_id": _stage(sword, "筑基")["id"]},
    )
    assert resp.status_code == 201, resp.text
    assert (resp.json()["stage_name"], resp.json()["stage_position"], resp.json()["stage_total"]) == ("筑基", 2, 9)

    # 删除被引用的「筑基」→ 409，什么都不变
    keep = [{"id": s["id"], "name": s["name"]} for s in sword["stages"] if s["name"] != "筑基"]
    resp = await client.patch(f"{base}/careers/{sword['id']}", json={"stages": keep})
    assert resp.status_code == 409 and resp.json()["detail"] == "该阶段仍有角色处于其中，无法删除"
    assert len((await client.get(f"{base}/careers/{sword['id']}")).json()["stages"]) == 9

    # 重排（倒序）+ 改名 + 删除未被引用的「渡劫」+ 新增一阶：角色仍处于「筑基」
    reordered = [{"id": s["id"], "name": s["name"]} for s in reversed(sword["stages"]) if s["name"] != "渡劫"]
    reordered[0]["name"] = "大乘期"
    reordered.append({"name": "飞升"})
    resp = await client.patch(f"{base}/careers/{sword['id']}", json={"stages": reordered})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert [s["name"] for s in updated["stages"]] == [
        "大乘期",
        "合体",
        "炼虚",
        "化神",
        "元婴",
        "金丹",
        "筑基",
        "练气",
        "飞升",
    ]
    assert [s["position"] for s in updated["stages"]] == list(range(1, 10))
    assert _stage(updated, "筑基")["id"] == _stage(sword, "筑基")["id"]
    assignment = (await client.get(f"{base}/characters/{lin['id']}/careers")).json()[0]
    assert (assignment["stage_name"], assignment["stage_position"]) == ("筑基", 7)
    assert updated["holder_count"] == 1

    holders = (await client.get(f"{base}/careers/{sword['id']}/holders")).json()
    assert [(h["character_name"], h["stage_name"], h["stage_position"], h["stage_total"]) for h in holders] == [
        ("林远", "筑基", 7, 9)
    ]

    # 同名 409（新建 / 改名）；阶段校验
    resp = await client.post(f"{base}/careers", json={"name": " 剑修 ", "stages": [{"name": "一"}]})
    assert resp.status_code == 409 and resp.json()["detail"] == "同名职业已存在"
    other = await _career(client, base, "法修", stages=["学徒"])
    resp = await client.patch(f"{base}/careers/{other['id']}", json={"name": "剑修"})
    assert resp.status_code == 409 and resp.json()["detail"] == "同名职业已存在"
    resp = await client.post(f"{base}/careers", json={"name": "空", "stages": []})
    assert resp.status_code == 400 and resp.json()["detail"] == "职业至少需要 1 个阶段"
    resp = await client.post(f"{base}/careers", json={"name": "重", "stages": [{"name": "一"}, {"name": " 一"}]})
    assert resp.status_code == 400 and resp.json()["detail"] == "同一职业内阶段名称不能重复"
    resp = await client.patch(
        f"{base}/careers/{other['id']}", json={"stages": [{"id": _stage(sword, "练气")["id"], "name": "x"}]}
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "阶段不属于该职业"
    resp = await client.patch(f"{base}/careers/{other['id']}", json={"stages": []})
    assert resp.status_code == 400 and resp.json()["detail"] == "职业至少需要 1 个阶段"

    listed = (await client.get(f"{base}/careers")).json()
    assert [c["name"] for c in listed] == ["剑修", "法修"]


async def test_assignment_limits_and_cascade(app, client):
    """AC2：主 1 副 3、重复挂、主副类别不符都被拒绝；删除职业后挂接与历史被清理。"""
    base = await _setup(client)
    sword = await _career(client, base, "剑修")
    magic = await _career(client, base, "法修", stages=["学徒", "法师"])
    subs = [await _career(client, base, f"副{i}", kind="sub", stages=["一品", "二品"]) for i in range(4)]
    lin = await _char(client, base, "林远")
    url = f"{base}/characters/{lin['id']}/careers"

    first = await client.post(url, json={"career_id": sword["id"], "note": "  瓶颈期 "})
    assert first.status_code == 201, first.text
    assert (first.json()["stage_name"], first.json()["note"], first.json()["role"]) == ("练气", "瓶颈期", "main")
    resp = await client.post(url, json={"career_id": magic["id"]})
    assert resp.status_code == 409 and resp.json()["detail"] == "该角色已有主职业"
    resp = await client.post(url, json={"career_id": sword["id"]})
    assert resp.status_code == 409 and resp.json()["detail"] == "该角色已挂该职业"
    for sub in subs[:3]:
        assert (await client.post(url, json={"career_id": sub["id"]})).status_code == 201
    resp = await client.post(url, json={"career_id": subs[3]["id"]})
    assert resp.status_code == 409 and resp.json()["detail"] == "副职业最多 3 个"

    zhang = await _char(client, base, "张三")
    url2 = f"{base}/characters/{zhang['id']}/careers"
    resp = await client.post(url2, json={"career_id": sword["id"], "role": "sub"})
    assert resp.status_code == 400 and resp.json()["detail"] == "主修职业只能作为主职业"
    resp = await client.post(url2, json={"career_id": subs[0]["id"], "role": "main"})
    assert resp.status_code == 400 and resp.json()["detail"] == "副修职业只能作为副职业"
    resp = await client.post(url2, json={"career_id": sword["id"], "stage_id": magic["stages"][0]["id"]})
    assert resp.status_code == 400 and resp.json()["detail"] == "阶段不属于该职业"
    assert (await client.get(url2)).json() == []

    listed = (await client.get(url)).json()
    assert [(a["career_name"], a["role"]) for a in listed] == [
        ("剑修", "main"),
        ("副0", "sub"),
        ("副1", "sub"),
        ("副2", "sub"),
    ]

    # 删除职业：挂接与该职业的历史级联删除，其他职业的历史保留
    assert await _count(app, CareerHistory) == 4
    assert (await client.delete(f"{base}/careers/{sword['id']}")).status_code == 204
    assert [a["career_name"] for a in (await client.get(url)).json()] == ["副0", "副1", "副2"]
    history = (await client.get(f"{base}/characters/{lin['id']}/career-history")).json()
    assert {h["career_name"] for h in history} == {"副0", "副1", "副2"}
    async with app.state.sessionmaker() as db:
        assert (
            await db.scalar(select(func.count()).select_from(CareerStage).where(CareerStage.career_id == sword["id"]))
            == 0
        )

    # 删除角色：其挂接与历史级联删除
    assert (await client.delete(f"{base}/characters/{lin['id']}")).status_code == 204
    assert await _count(app, CharacterCareer) == 0
    assert await _count(app, CareerHistory) == 0


async def test_assignment_history(client):
    """AC3：练气 → 金丹 → 筑基 两条换阶历史（倒序、名称正确）；只改备注也写历史；无变化不写；卸下写历史。"""
    base = await _setup(client)
    sword = await _career(client, base, "剑修")
    lin = await _char(client, base, "林远")
    url = f"{base}/characters/{lin['id']}/careers"
    hist_url = f"{base}/characters/{lin['id']}/career-history"
    aid = (await client.post(url, json={"career_id": sword["id"]})).json()["id"]

    resp = await client.patch(
        f"{url}/{aid}", json={"stage_id": _stage(sword, "金丹")["id"], "chapter_no": 3, "history_note": "奇遇"}
    )
    assert resp.status_code == 200 and resp.json()["stage_name"] == "金丹"
    await client.patch(f"{url}/{aid}", json={"stage_id": _stage(sword, "筑基")["id"], "chapter_no": 5})
    history = (await client.get(hist_url)).json()
    assert [h["changes"] for h in history[:2]] == [
        {"stage": ["第3阶·金丹", "第2阶·筑基"]},
        {"stage": ["第1阶·练气", "第3阶·金丹"]},
    ]
    assert [(h["chapter_no"], h["note"], h["source"], h["career_name"]) for h in history[:2]] == [
        (5, "", "manual", "剑修"),
        (3, "奇遇", "manual", "剑修"),
    ]
    assert history[2]["changes"] == {"role": [None, "main"], "stage": [None, "第1阶·练气"], "note": [None, ""]}

    # 只改备注
    await client.patch(f"{url}/{aid}", json={"note": "瓶颈期"})
    assert (await client.get(hist_url)).json()[0]["changes"] == {"note": ["", "瓶颈期"]}
    # 无变化：不写历史
    resp = await client.patch(f"{url}/{aid}", json={"stage_id": _stage(sword, "筑基")["id"], "note": "瓶颈期"})
    assert resp.status_code == 200
    assert len((await client.get(hist_url)).json()) == 4

    # 已有剧情记录后，先要求明确意图，再按章节卸下。
    ambiguous = await client.delete(f"{url}/{aid}")
    assert ambiguous.status_code == 409
    assert ambiguous.json()["code"] == "progression_intent_required"
    assert (await client.delete(f"{url}/{aid}", params={"chapter_no": 6})).status_code == 204
    assert (await client.get(url)).json() == []
    assert (await client.get(hist_url)).json()[0]["changes"] == {"removed": ["剑修", None]}
    assert (await client.patch(f"{url}/{aid}", json={"note": "x"})).status_code == 404


async def test_career_kind_change_syncs_holders(client):
    """职业类别变化：持有人的主 / 副随之变化并写历史；会超出上限时 409 且不改动。"""
    base = await _setup(client)
    dan = await _career(client, base, "丹师", kind="sub", stages=["一品", "二品"])
    sword = await _career(client, base, "剑修")
    lin = await _char(client, base, "林远")
    zhang = await _char(client, base, "张三")
    await client.post(f"{base}/characters/{lin['id']}/careers", json={"career_id": dan["id"]})
    await client.post(f"{base}/characters/{zhang['id']}/careers", json={"career_id": dan["id"]})
    await client.post(f"{base}/characters/{zhang['id']}/careers", json={"career_id": sword["id"]})

    resp = await client.patch(f"{base}/careers/{dan['id']}", json={"kind": "main"})
    assert resp.status_code == 409 and resp.json()["detail"] == "该角色已有主职业"
    assert (await client.get(f"{base}/careers/{dan['id']}")).json()["kind"] == "sub"
    # 整个 PATCH 回滚：先处理的林远也保持副职业，且没有多出历史
    assert (await client.get(f"{base}/characters/{lin['id']}/careers")).json()[0]["role"] == "sub"
    assert len((await client.get(f"{base}/characters/{lin['id']}/career-history")).json()) == 1

    await client.delete(f"{base}/characters/{zhang['id']}")
    resp = await client.patch(f"{base}/careers/{dan['id']}", json={"kind": "main"})
    assert resp.status_code == 200 and resp.json()["kind"] == "main"
    assert (await client.get(f"{base}/characters/{lin['id']}/careers")).json()[0]["role"] == "main"
    hist = (await client.get(f"{base}/characters/{lin['id']}/career-history")).json()
    assert hist[0]["changes"] == {"role": ["sub", "main"]}


async def test_career_batch_atomic(app, client):
    base = await _setup(client)
    await _career(client, base, "剑修")
    body = {"careers": [{"name": "法修", "stages": [{"name": "学徒"}]}, {"name": "剑修", "stages": [{"name": "一"}]}]}
    resp = await client.post(f"{base}/careers/batch", json=body)
    assert resp.status_code == 409 and resp.json()["detail"] == "同名职业已存在"
    assert await _count(app, Career) == 1
    body["careers"][1]["name"] = "丹师"
    body["careers"][1]["kind"] = "sub"
    resp = await client.post(f"{base}/careers/batch", json=body)
    assert resp.status_code == 201, resp.text
    assert [(c["name"], c["kind"], len(c["stages"])) for c in resp.json()] == [("法修", "main", 1), ("丹师", "sub", 1)]


async def test_cross_project_ids_rejected(app, client, recycle_admin):
    """其他项目ID仍隔离；软删保留职业/挂接，显式purge才级联清理。"""
    base = await _setup(client)
    p2 = await create_project(client, title="乙")
    base2 = f"/api/v1/projects/{p2['id']}"
    foreign = await _career(client, base2, "外人职业")
    own = await _career(client, base, "剑修")
    lin = await _char(client, base, "林远")
    url = f"{base}/characters/{lin['id']}/careers"
    resp = await client.post(url, json={"career_id": foreign["id"]})
    assert resp.status_code == 400 and resp.json()["detail"] == "职业不存在或不属于该项目"
    resp = await client.post(url, json={"career_id": own["id"], "stage_id": foreign["stages"][0]["id"]})
    assert resp.status_code == 400 and resp.json()["detail"] == "阶段不属于该职业"
    resp = await client.patch(
        f"{base}/careers/{own['id']}", json={"stages": [{"id": foreign["stages"][0]["id"], "name": "x"}]}
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "阶段不属于该职业"
    # 另一个项目的路径下访问本项目的职业 → 404
    assert (await client.get(f"{base2}/careers/{own['id']}")).status_code == 404
    resp = await client.post(
        f"{base}/characters/with-relations",
        json={"character": {"name": "新人"}, "careers": [{"career_id": foreign["id"]}]},
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "职业不存在或不属于该项目"

    assert (await client.post(url, json={"career_id": own["id"]})).status_code == 201
    assert (await client.delete(base)).status_code == 204
    assert (await client.get(f"{base}/careers/{own['id']}")).status_code == 404
    assert await _count(app, CharacterCareer) == 1
    assert await _count(app, Career) == 2
    await purge_trashed_project(recycle_admin, int(base.rsplit("/", 1)[1]))
    assert await _count(app, CharacterCareer) == 0
    assert await _count(app, Career) == 1


async def test_project_assignments(client):
    """项目级挂接列表：一次返回全部角色的挂接（按角色、主职业在前），不含其他项目。"""
    base = await _setup(client)
    p2 = await create_project(client, title="乙")
    base2 = f"/api/v1/projects/{p2['id']}"
    sword = await _career(client, base, "剑修")
    dan = await _career(client, base, "丹师", kind="sub", stages=["一品", "二品"])
    lin = await _char(client, base, "林远")
    zhang = await _char(client, base, "张三")
    await client.post(f"{base}/characters/{lin['id']}/careers", json={"career_id": dan["id"]})
    await client.post(
        f"{base}/characters/{lin['id']}/careers",
        json={"career_id": sword["id"], "stage_id": _stage(sword, "筑基")["id"], "note": "瓶颈期"},
    )
    await client.post(f"{base}/characters/{zhang['id']}/careers", json={"career_id": dan["id"]})
    other = await _career(client, base2, "外人职业")
    stranger = await _char(client, base2, "外人")
    await client.post(f"{base2}/characters/{stranger['id']}/careers", json={"career_id": other["id"]})

    resp = await client.get(f"{base}/character-careers")
    assert resp.status_code == 200, resp.text
    assert [
        (
            a["character_id"],
            a["career_name"],
            a["role"],
            a["stage_name"],
            a["stage_position"],
            a["stage_total"],
            a["note"],
        )
        for a in resp.json()
    ] == [
        (lin["id"], "剑修", "main", "筑基", 2, 9, "瓶颈期"),
        (lin["id"], "丹师", "sub", "一品", 1, 2, ""),
        (zhang["id"], "丹师", "sub", "一品", 1, 2, ""),
    ]
    assert len((await client.get(f"{base2}/character-careers")).json()) == 1
