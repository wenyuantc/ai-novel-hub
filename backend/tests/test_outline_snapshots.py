"""大纲快照：向导覆盖后恢复（AC6）、恢复本身可撤销、保留 20 个、原 id 被占用时重映射、章节号平移提示。"""

import json

from tests.helpers import create_ai_config, create_project, register, run_task

OUTLINE_A = {
    "volumes": [
        {
            "volume_no": 1,
            "volume_title": "启程",
            "chapters": [
                {"chapter_no": 1, "title": "出发", "summary": "离港"},
                {"chapter_no": 2, "title": "异象", "summary": "遭遇异常信号"},
            ],
        }
    ]
}
OUTLINE_B = [{"title": "全新一章", "summary": "重写"}]


async def _setup(client) -> tuple[dict, str]:
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    return p, f"/api/v1/projects/{p['id']}"


async def _wizard(client, fake_llm, base, data, **body) -> list:
    fake_llm.push(json.dumps(data, ensure_ascii=False))
    _, events = await run_task(client, await client.post(f"{base}/wizard/outline", json=body))
    assert events[-1][0] == "done", events[-1]
    return events[-1][1]["result"]["items"]


async def test_restore_after_wizard_replace(client, fake_llm):
    p, base = await _setup(client)
    items = await _wizard(client, fake_llm, base, OUTLINE_A)
    o1, o2 = items
    # 第一次覆盖前没有大纲 → 不存快照
    assert (await client.get(f"{base}/outline-snapshots")).json() == []
    chapters = (await client.post(f"{base}/chapters/from-outlines", json={})).json()
    ch1, ch2 = chapters
    await client.patch(f"{base}/chapters/{ch1['id']}", json={"content": "第一章正文"})
    # 第 2 条大纲展开为 2 章细纲并建章（细纲关联也要能恢复）
    plans = (
        await client.put(f"{base}/outlines/{o2['id']}/plans", json={"plans": [{"title": "甲"}, {"title": "乙"}]})
    ).json()
    built = (await client.post(f"{base}/outlines/{o2['id']}/plans/build")).json()
    ch3 = built["chapters"][1]
    assert built["chapters"][0]["id"] == ch2["id"]

    # 向导覆盖：先存快照，章节解除关联但保留
    await _wizard(client, fake_llm, base, OUTLINE_B)
    snaps = (await client.get(f"{base}/outline-snapshots")).json()
    assert [(s["reason"], s["outline_count"], s["plan_count"], s["link_count"]) for s in snaps] == [
        ("wizard_replace", 2, 2, 3)
    ]
    chs = {c["id"]: c for c in (await client.get(f"{base}/chapters")).json()}
    assert all(c["outline_id"] is None and c["plan_id"] is None for c in chs.values())
    # 覆盖后新建的章节
    new_ch = (await client.post(f"{base}/chapters/from-outlines", json={})).json()[0]

    detail = (await client.get(f"{base}/outline-snapshots/{snaps[0]['id']}")).json()
    assert [(o["title"], o["plan_count"]) for o in detail["outlines"]] == [("出发", 0), ("异象", 2)]
    assert detail["warnings"] == []

    resp = await client.post(f"{base}/outline-snapshots/{snaps[0]['id']}/restore")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert (result["outlines"], result["plans"], result["links_restored"]) == (2, 2, 3)
    assert result["backup"]["reason"] == "restore" and result["backup"]["outline_count"] == 1

    outlines = (await client.get(f"{base}/outlines")).json()
    # 原始 id 恢复
    assert [(o["id"], o["title"]) for o in outlines] == [(o1["id"], "出发"), (o2["id"], "异象")]
    restored_plans = (await client.get(f"{base}/outlines/{o2['id']}/plans")).json()
    assert [(pl["id"], pl["chapter_id"]) for pl in restored_plans] == [
        (plans[0]["id"], ch2["id"]),
        (plans[1]["id"], ch3["id"]),
    ]
    chs = {c["id"]: c for c in (await client.get(f"{base}/chapters")).json()}
    assert (chs[ch1["id"]]["outline_id"], chs[ch1["id"]]["plan_id"]) == (o1["id"], None)
    assert (chs[ch2["id"]]["outline_id"], chs[ch2["id"]]["plan_id"]) == (o2["id"], plans[0]["id"])
    assert (chs[ch3["id"]]["outline_id"], chs[ch3["id"]]["plan_id"]) == (o2["id"], plans[1]["id"])
    # 覆盖后新建的章节保留但没有关联；正文无损
    assert chs[new_ch["id"]]["outline_id"] is None
    assert (await client.get(f"{base}/chapters/{ch1['id']}")).json()["content"] == "第一章正文"

    # 恢复本身也可以撤销：恢复「恢复前」的快照 → 回到向导覆盖后的版本，新章节重新关联
    resp = await client.post(f"{base}/outline-snapshots/{result['backup']['id']}/restore")
    assert resp.status_code == 200
    outlines = (await client.get(f"{base}/outlines")).json()
    assert [o["title"] for o in outlines] == ["全新一章"]
    chs = {c["id"]: c for c in (await client.get(f"{base}/chapters")).json()}
    assert chs[new_ch["id"]]["outline_id"] == outlines[0]["id"]
    assert chs[ch1["id"]]["outline_id"] is None


async def test_snapshots_pruned_to_20(client):
    p, base = await _setup(client)
    await client.post(f"{base}/outlines", json={"title": "一"})
    ids = []
    for i in range(22):
        resp = await client.post(f"{base}/outline-snapshots", json={"note": f"手动{i}"})
        assert resp.status_code == 201
        ids.append(resp.json()["id"])
    snaps = (await client.get(f"{base}/outline-snapshots")).json()
    assert len(snaps) == 20
    assert [s["id"] for s in snaps] == list(reversed(ids[2:]))
    assert snaps[0]["note"] == "手动21" and snaps[0]["reason"] == "manual" and snaps[0]["outline_count"] == 1
    resp = await client.delete(f"{base}/outline-snapshots/{ids[5]}")
    assert resp.status_code == 204
    assert (await client.get(f"{base}/outline-snapshots/{ids[5]}")).status_code == 404
    assert (await client.post(f"{base}/outline-snapshots/{ids[0]}/restore")).status_code == 404


async def test_restore_remaps_taken_ids_and_warns_shift(client):
    p, base = await _setup(client)
    o1 = (await client.post(f"{base}/outlines", json={"title": "一", "chapter_no": 1})).json()
    o2 = (await client.post(f"{base}/outlines", json={"title": "二", "chapter_no": 2})).json()
    plan = (await client.put(f"{base}/outlines/{o2['id']}/plans", json={"plans": [{"title": "细"}]})).json()[0]
    chs = (await client.post(f"{base}/chapters/from-outlines", json={})).json()
    built = (await client.post(f"{base}/outlines/{o2['id']}/plans/build")).json()
    snap = (await client.post(f"{base}/outline-snapshots", json={})).json()
    # 删除全部大纲后，另一个项目的新大纲复用了被删除的 id（SQLite 会复用最大 id）
    for o in (o1, o2):
        assert (await client.delete(f"{base}/outlines/{o['id']}")).status_code == 204
    other = await create_project(client, title="另一本")
    taken = (await client.post(f"/api/v1/projects/{other['id']}/outlines", json={"title": "占位"})).json()
    # 在第 1 章前建章（第 1 章已被占用 → 整体后移 2）：快照中的章节号与当前不一致
    o0 = (await client.post(f"{base}/outlines", json={"title": "楔子", "chapter_no": 1})).json()
    await client.put(f"{base}/outlines/{o0['id']}/plans", json={"plans": [{"title": "楔一"}, {"title": "楔二"}]})
    await client.post(f"{base}/outlines/{o0['id']}/plans/build")

    detail = (await client.get(f"{base}/outline-snapshots/{snap['id']}")).json()
    assert any("章节号发生过变化" in w for w in detail["warnings"])

    resp = await client.post(f"{base}/outline-snapshots/{snap['id']}/restore")
    assert resp.status_code == 200, resp.text
    assert resp.json()["links_restored"] == 2
    outlines = (await client.get(f"{base}/outlines")).json()
    assert [o["title"] for o in outlines] == ["一", "二"]
    # 被其他项目占用的 id 改用新 id，另一个项目的数据不受影响
    assert taken["id"] == o1["id"]  # 前提：SQLite 复用了被删除的最大 id 之前的 id
    assert taken["id"] not in [o["id"] for o in outlines]
    assert next(o["id"] for o in outlines if o["title"] == "二") == o2["id"]
    assert (await client.get(f"/api/v1/projects/{other['id']}/outlines")).json()[0]["title"] == "占位"
    new_o2 = next(o for o in outlines if o["title"] == "二")
    plans = (await client.get(f"{base}/outlines/{new_o2['id']}/plans")).json()
    assert [pl["chapter_id"] for pl in plans] == [built["chapters"][0]["id"]]
    assert plans[0]["id"] == plan["id"]
    chs_now = {c["id"]: c for c in (await client.get(f"{base}/chapters")).json()}
    assert chs_now[chs[0]["id"]]["outline_id"] == next(o["id"] for o in outlines if o["title"] == "一")
    # 恢复不改章节号
    assert chs_now[chs[0]["id"]]["chapter_no"] == 3


async def test_restore_oldest_snapshot_when_full_keeps_target(client):
    """已有 20 个快照时恢复最旧的一个：存 backup 后的裁剪不能删掉被恢复的目标，改为删掉其余最旧的。"""
    p, base = await _setup(client)
    await client.post(f"{base}/outlines", json={"title": "一"})
    ids = [(await client.post(f"{base}/outline-snapshots", json={"note": f"手动{i}"})).json()["id"] for i in range(20)]
    resp = await client.post(f"{base}/outline-snapshots/{ids[0]}/restore")
    assert resp.status_code == 200, resp.text
    backup_id = resp.json()["backup"]["id"]
    snaps = [s["id"] for s in (await client.get(f"{base}/outline-snapshots")).json()]
    assert len(snaps) == 20
    assert ids[0] in snaps and backup_id in snaps and ids[1] not in snaps
    # 目标仍可再次恢复
    assert (await client.post(f"{base}/outline-snapshots/{ids[0]}/restore")).status_code == 200
    snaps = [s["id"] for s in (await client.get(f"{base}/outline-snapshots")).json()]
    assert len(snaps) == 20 and ids[0] in snaps and ids[2] not in snaps
