"""伏笔管理：CRUD 与历史、关联角色（跨项目 400、删角色只解除关联）、快捷操作必填校验、
派生属性、筛选排序、统计、章节提醒。"""

from app.models import Foreshadow
from app.services.foreshadow_service import is_long_term, overdue_by, select_reminders
from tests.helpers import create_project, register


async def _setup(client):
    await register(client)
    p = await create_project(client, title="剑道")
    base = f"/api/v1/projects/{p['id']}"

    async def char(name):
        resp = await client.post(f"{base}/characters", json={"name": name})
        assert resp.status_code == 201, resp.text
        return resp.json()

    return p, base, await char("林远"), await char("张三")


async def _create(client, base, **body):
    resp = await client.post(f"{base}/foreshadows", json={"title": "玉佩", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _chapter(client, base, no, content="正文"):
    resp = await client.post(f"{base}/chapters", json={"title": f"章{no}", "chapter_no": no, "content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_crud_history_and_characters(client, make_client):
    """AC1：第 3 章埋、第 8 章回收，关联两个角色；编辑、改状态、删除；删角色只解除关联；跨项目角色 400。"""
    p, base, lin, zhang = await _setup(client)
    fs = await _create(
        client,
        base,
        content="林远拾到的玉佩",
        plant_hint="打斗间隙瞥见",
        reveal_hint="揭示来自师门",
        planned_plant_chapter=3,
        planned_resolve_chapter=8,
        importance=4,
        character_ids=[zhang["id"], lin["id"]],
    )
    assert (fs["status"], fs["planned_plant_chapter"], fs["planned_resolve_chapter"], fs["importance"]) == (
        "pending",
        3,
        8,
        4,
    )
    assert fs["include_in_context"] is True and fs["source"] == "manual"
    assert fs["character_ids"] == [lin["id"], zhang["id"]] and fs["character_names"] == ["林远", "张三"]
    assert fs["is_long_term"] is False and fs["overdue_by"] == 0

    # 编辑：标题 / 状态 / 角色整体替换，历史记录章节号与备注
    resp = await client.patch(
        f"{base}/foreshadows/{fs['id']}",
        json={
            "title": " 神秘玉佩 ",
            "status": "planted",
            "actual_plant_chapter": 3,
            "character_ids": [lin["id"]],
            "chapter_no": 3,
            "history_note": "手动标记",
        },
    )
    assert resp.status_code == 200, resp.text
    fs = resp.json()
    assert (fs["title"], fs["status"], fs["actual_plant_chapter"], fs["character_names"]) == (
        "神秘玉佩",
        "planted",
        3,
        ["林远"],
    )
    history = (await client.get(f"{base}/foreshadows/{fs['id']}/history")).json()
    assert len(history) == 2
    assert (history[0]["source"], history[0]["chapter_no"], history[0]["note"]) == ("manual", 3, "手动标记")
    assert history[0]["changes"] == {
        "title": ["玉佩", "神秘玉佩"],
        "status": ["pending", "planted"],
        "actual_plant_chapter": [None, 3],
        "characters": [["林远", "张三"], ["林远"]],
    }
    created = history[1]["changes"]
    assert created["title"] == [None, "玉佩"] and created["characters"] == [None, ["林远", "张三"]]
    # 无变化不写历史
    await client.patch(f"{base}/foreshadows/{fs['id']}", json={"title": "神秘玉佩"})
    assert len((await client.get(f"{base}/foreshadows/{fs['id']}/history")).json()) == 2
    # 章节字段显式 null = 清空
    fs = (await client.patch(f"{base}/foreshadows/{fs['id']}", json={"planned_resolve_chapter": None})).json()
    assert fs["planned_resolve_chapter"] is None and fs["is_long_term"] is True

    # 删除角色：伏笔保留，只解除关联
    await client.patch(f"{base}/foreshadows/{fs['id']}", json={"character_ids": [lin["id"], zhang["id"]]})
    assert (await client.delete(f"{base}/characters/{zhang['id']}")).status_code == 204
    fs = (await client.get(f"{base}/foreshadows/{fs['id']}")).json()
    assert fs["character_ids"] == [lin["id"]]

    # 跨项目 / 不存在的角色 → 400
    other = await create_project(client, title="别的书")
    stranger = (await client.post(f"/api/v1/projects/{other['id']}/characters", json={"name": "外人"})).json()
    for ids in ([stranger["id"]], [99999]):
        resp = await client.post(f"{base}/foreshadows", json={"title": "x", "character_ids": ids})
        assert resp.status_code == 400 and resp.json()["detail"] == "关联角色不存在或不属于该项目"
        resp = await client.patch(f"{base}/foreshadows/{fs['id']}", json={"character_ids": ids})
        assert resp.status_code == 400 and resp.json()["detail"] == "关联角色不存在或不属于该项目"
    # 他项目路径下访问本伏笔 → 404
    resp = await client.get(f"/api/v1/projects/{other['id']}/foreshadows/{fs['id']}")
    assert resp.status_code == 404 and resp.json()["detail"] == "伏笔不存在"

    # 删除
    assert (await client.delete(f"{base}/foreshadows/{fs['id']}")).status_code == 204
    assert (await client.get(f"{base}/foreshadows/{fs['id']}")).status_code == 404


async def test_validation_and_quick_actions(client):
    """状态必填校验、快捷操作（标记已埋 / 回收 / 部分回收 / 废弃）、任意状态可手动改回。"""
    p, base, lin, _ = await _setup(client)
    resp = await client.post(f"{base}/foreshadows", json={"title": "x", "status": "planted"})
    assert resp.status_code == 400 and resp.json()["detail"] == "请填写实际埋设章节"
    resp = await client.post(
        f"{base}/foreshadows", json={"title": "x", "status": "resolved", "actual_plant_chapter": 1}
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "请填写实际回收章节"
    resp = await client.post(f"{base}/foreshadows", json={"title": "   "})
    assert resp.status_code == 400 and resp.json()["detail"] == "伏笔标题不能为空"
    resp = await client.post(f"{base}/foreshadows", json={"title": "x", "importance": 6})
    assert resp.status_code == 422
    # 章节号可以指向尚未创建的章节
    fs = await _create(client, base, planned_plant_chapter=50, planned_resolve_chapter=120)
    url = f"{base}/foreshadows/{fs['id']}"

    resp = await client.post(f"{url}/plant")
    assert resp.status_code == 400 and resp.json()["detail"] == "请填写实际埋设章节"
    resp = await client.post(f"{url}/plant", json={"chapter_no": 3})
    assert resp.status_code == 200 and (resp.json()["status"], resp.json()["actual_plant_chapter"]) == ("planted", 3)
    resp = await client.post(f"{url}/resolve", json={})
    assert resp.status_code == 400 and resp.json()["detail"] == "请填写实际回收章节"
    resp = await client.post(f"{url}/resolve", json={"chapter_no": 6, "partial": True})
    assert (resp.json()["status"], resp.json()["actual_resolve_chapter"]) == ("partial", 6)
    # 已有实际回收章时可以省略
    resp = await client.post(f"{url}/resolve")
    assert (resp.json()["status"], resp.json()["actual_resolve_chapter"]) == ("resolved", 6)
    resp = await client.post(f"{url}/abandon")
    assert resp.status_code == 200 and resp.json()["status"] == "abandoned"
    # 手动改回待埋
    resp = await client.patch(url, json={"status": "pending"})
    assert resp.status_code == 200 and resp.json()["status"] == "pending"
    history = (await client.get(f"{url}/history")).json()
    assert [h["changes"].get("status") for h in history[:3]] == [
        ["abandoned", "pending"],
        ["resolved", "abandoned"],
        ["partial", "resolved"],
    ]
    assert history[-2]["chapter_no"] == 3  # 标记已埋时记录章节号
    for action in ("plant", "resolve", "abandon", "history"):
        method = "GET" if action == "history" else "POST"
        resp = await client.request(method, f"{base}/foreshadows/99999/{action}")
        assert resp.status_code == 404 and resp.json()["detail"] == "伏笔不存在"


def _fs(id_, status="planted", **kw):
    data = {
        "include_in_context": True,
        "importance": 3,
        "planned_plant_chapter": None,
        "planned_resolve_chapter": None,
    }
    data.update(kw)
    return Foreshadow(id=id_, project_id=1, title=f"伏笔{id_}", status=status, **data)


def test_derived_properties():
    assert is_long_term(_fs(1)) and is_long_term(_fs(1, "partial"))
    assert not is_long_term(_fs(1, "pending")) and not is_long_term(_fs(1, planned_resolve_chapter=5))
    assert overdue_by(_fs(1, planned_resolve_chapter=5), 8) == 3
    assert overdue_by(_fs(1, planned_resolve_chapter=8), 8) == 0
    assert overdue_by(_fs(1, "resolved", planned_resolve_chapter=5), 8) == 0
    assert overdue_by(_fs(1, "pending", planned_resolve_chapter=5), 8) == 0


def test_select_reminders_tiers_limits_and_filters():
    """AC4（选取）：五档规则与上限，关闭注入 / 已回收 / 已废弃的不出现。"""
    items = [
        _fs(1, "pending", planned_plant_chapter=8),
        _fs(2, "pending", planned_plant_chapter=8, importance=5),
        _fs(3, "pending", planned_plant_chapter=7),
        _fs(4, planned_resolve_chapter=8),
        _fs(5, "partial", planned_resolve_chapter=8),
        *[_fs(10 + i, planned_resolve_chapter=1 + i) for i in range(5)],  # 超期 7..3 章
        *[_fs(20 + i, planned_resolve_chapter=9 + i) for i in range(4)],  # 9..12
        *[_fs(30 + i, importance=1 + i) for i in range(5)],  # 长线，重要度 1..5
        _fs(40, planned_resolve_chapter=8, include_in_context=False),
        _fs(41, "resolved", planned_resolve_chapter=8),
        _fs(42, "abandoned", planned_plant_chapter=8),
    ]
    rem = select_reminders(items, 8)
    assert [f.id for f in rem.to_plant] == [2, 1]
    assert [f.id for f in rem.must_resolve] == [4, 5]
    assert [f.id for f in rem.overdue] == [10, 11, 12]
    assert [f.id for f in rem.upcoming] == [20, 21, 22]
    assert [f.id for f in rem.long_term] == [34, 33, 32]


async def test_list_filters_sort_stats_and_reminders(client):
    """AC5（后端）：统计、筛选（状态 / 超期 / 长线 / 角色）、排序、章节提醒接口。"""
    p, base, lin, zhang = await _setup(client)
    for no in range(1, 9):
        await _chapter(client, base, no)
    await _chapter(client, base, 9, content="   ")  # 空白正文不计入当前章
    await _create(client, base, title="甲", status="planted", actual_plant_chapter=1, planned_resolve_chapter=5)
    b = await _create(
        client,
        base,
        title="乙",
        status="planted",
        actual_plant_chapter=2,
        planned_resolve_chapter=10,
        importance=5,
        character_ids=[lin["id"]],
    )
    await _create(client, base, title="丙", status="planted", actual_plant_chapter=2, importance=4)
    await _create(client, base, title="丁", planned_plant_chapter=8, plant_hint="雨夜埋下")
    e = await _create(client, base, title="戊", status="abandoned")
    await _create(client, base, title="己", status="planted", actual_plant_chapter=1, planned_resolve_chapter=8)
    await client.patch(f"{base}/foreshadows/{b['id']}", json={"include_in_context": False})

    stats = (await client.get(f"{base}/foreshadows/stats")).json()
    assert stats == {
        "pending": 1,
        "planted": 4,
        "resolved": 0,
        "partial": 0,
        "abandoned": 1,
        "overdue": 1,
        "long_term": 1,
        "current_chapter": 8,
    }

    async def ids(**params):
        resp = await client.get(f"{base}/foreshadows", params=params)
        assert resp.status_code == 200, resp.text
        return [f["title"] for f in resp.json()]

    assert await ids() == ["甲", "己", "乙", "丙", "丁", "戊"]  # 计划回收章升序，空值在后
    assert await ids(sort="importance") == ["乙", "丙", "甲", "己", "丁", "戊"]
    assert await ids(status="planted") == ["甲", "己", "乙", "丙"]
    assert await ids(overdue="true") == ["甲"]
    assert await ids(long_term="true") == ["丙"]
    assert await ids(character_id=lin["id"]) == ["乙"]
    assert await ids(character_id=zhang["id"]) == []
    listed = {f["title"]: f for f in (await client.get(f"{base}/foreshadows")).json()}
    assert listed["甲"]["overdue_by"] == 3 and listed["丙"]["is_long_term"] is True
    assert (await client.get(f"{base}/foreshadows", params={"status": "bad"})).status_code == 422

    ch8 = (await client.get(f"{base}/chapters")).json()[7]
    rem = (await client.get(f"{base}/chapters/{ch8['id']}/foreshadow-reminders")).json()
    assert rem["chapter_no"] == 8
    assert [f["title"] for f in rem["to_plant"]] == ["丁"]
    assert [f["title"] for f in rem["must_resolve"]] == ["己"]
    assert [(f["title"], f["overdue_by"]) for f in rem["overdue"]] == [("甲", 3)]
    assert rem["upcoming"] == []  # 乙关闭了注入上下文
    assert [f["title"] for f in rem["long_term"]] == ["丙"]
    assert all(f["id"] not in (b["id"], e["id"]) for tier in rem.values() if isinstance(tier, list) for f in tier)
    resp = await client.get(f"{base}/chapters/99999/foreshadow-reminders")
    assert resp.status_code == 404
