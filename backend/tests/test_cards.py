from typing import Any

from sqlalchemy import select

from app.core.errors import BadRequestError
from app.models import (
    Character,
    Organization,
    OrganizationMember,
    Relationship,
    RelationshipHistory,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
)
from app.services import organization_service, setting_progression_service
from app.services.card_service import rename_for
from tests.helpers import create_project, register


def _id_keys(obj: Any) -> list[str]:
    """递归找出所有 id / *_id 键（卡片不得含内部 ID）。"""
    if isinstance(obj, dict):
        own = [k for k in obj if k == "id" or k.endswith("_id")]
        return own + [k for v in obj.values() for k in _id_keys(v)]
    if isinstance(obj, list):
        return [k for v in obj for k in _id_keys(v)]
    return []


async def _char(client, base, name, **kw):
    resp = await client.post(f"{base}/characters", json={"name": name, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _source_project(client):
    """项目甲：3 个角色、1 个组织（2 名成员）、2 条关系 + 1 个未导出的角色。"""
    p = await create_project(client, title="甲")
    base = f"/api/v1/projects/{p['id']}"
    a = await _char(client, base, "林远", role_type="主角", aliases=["小远"], personality="坚韧")
    b = await _char(client, base, "张三", role_type="重要配角")
    c = await _char(client, base, "李四", role_type="反派")
    outsider = await _char(client, base, "路人")
    org = (await client.post(f"{base}/organizations", json={"name": "青云宗", "org_type": "宗门"})).json()
    for cid, pos in ((a["id"], "弟子"), (b["id"], "长老"), (outsider["id"], "杂役")):
        await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": cid, "position": pos})
    rels = [
        {"a_id": b["id"], "b_id": a["id"], "role_a": "师父", "role_b": "徒弟", "intimacy": 80},
        {"a_id": a["id"], "b_id": c["id"], "role_a": "宿命之敌", "role_b": "宿命之敌", "category": "hostile"},
        {"a_id": a["id"], "b_id": outsider["id"], "role_a": "朋友"},
    ]
    for r in rels:
        assert (await client.post(f"{base}/relationships", json=r)).status_code == 201
    return base, [a, b, c], org


async def test_cards_roundtrip_with_rename(client):
    """AC7：甲导出 3 角色 + 1 组织，导入乙；冲突重命名后角色 / 组织 / 成员 / 关系全部重建，与甲的 id 无关。"""
    await register(client)
    base, chars, org = await _source_project(client)
    resp = await client.post(
        f"{base}/cards/export", json={"character_ids": [c["id"] for c in chars], "organization_ids": [org["id"]]}
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert (card["format"], card["version"]) == ("ainovel.cards", 3)
    # 没有挂职业：职业字段为空
    assert card["careers"] == [] and all(c["careers"] == [] for c in card["characters"])
    assert [c["name"] for c in card["characters"]] == ["林远", "张三", "李四"]
    assert card["characters"][0]["aliases"] == ["小远"]
    # 成员 / 关系只含导出集合内的
    assert [m["character_name"] for m in card["organizations"][0]["members"]] == ["林远", "张三"]
    assert {(r["a_name"], r["b_name"], r["role_a"]) for r in card["relationships"]} == {
        ("张三", "林远", "师父"),
        ("林远", "李四", "宿命之敌"),
    }
    assert not _id_keys(card)

    # 项目乙：已有同名「林远」与「青云宗」，以及「林远（导入）」
    p2 = await create_project(client, title="乙")
    base2 = f"/api/v1/projects/{p2['id']}"
    await _char(client, base2, "林远")
    await _char(client, base2, "林远（导入）")
    await client.post(f"{base2}/organizations", json={"name": "青云宗"})

    resp = await client.post(f"{base2}/cards/import/preview", json={"data": card})
    assert resp.status_code == 200, resp.text
    pv = resp.json()
    assert [(c["name"], c["status"], c["rename_to"]) for c in pv["characters"]] == [
        ("林远", "conflict", "林远（导入2）"),
        ("张三", "new", None),
        ("李四", "new", None),
    ]
    assert [(o["name"], o["status"]) for o in pv["organizations"]] == [("青云宗", "conflict")]
    assert all(r["status"] == "ok" for r in pv["relationships"])

    resp = await client.post(
        f"{base2}/cards/import",
        json={"data": card, "resolutions": {"characters": {"林远": "rename"}, "organizations": {"青云宗": "rename"}}},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert [(c["name"], c["imported_name"], c["action"]) for c in result["characters"]] == [
        ("林远", "林远（导入2）", "renamed"),
        ("张三", "张三", "created"),
        ("李四", "李四", "created"),
    ]
    assert result["organizations"][0]["imported_name"] == "青云宗（导入）"
    assert (result["relationships_created"], result["members_created"]) == (2, 2)

    imported = {c["name"]: c for c in (await client.get(f"{base2}/characters")).json()}
    assert imported["林远（导入2）"]["personality"] == "坚韧"
    assert imported["林远（导入2）"]["always_in_context"] is True
    new_ids = {imported[n]["id"] for n in ("林远（导入2）", "张三", "李四")}
    assert not new_ids & {c["id"] for c in chars}
    rels = (await client.get(f"{base2}/relationships", params={"character_id": imported["林远（导入2）"]["id"]})).json()
    assert sorted((r["other_name"], r["other_role"]) for r in rels) == [("张三", "师父"), ("李四", "宿命之敌")]
    assert all(r["a_id"] in new_ids and r["b_id"] in new_ids for r in rels)
    # 自定义类型在乙中新建
    assert any(
        t["role_a"] == "宿命之敌" and not t["builtin"] for t in (await client.get(f"{base2}/relationship-types")).json()
    )
    hist = (await client.get(f"{base2}/relationships/{rels[0]['id']}/history")).json()
    assert hist[0]["source"] == "import"
    orgs = {o["name"]: o for o in (await client.get(f"{base2}/organizations")).json()}
    members = (await client.get(f"{base2}/organizations/{orgs['青云宗（导入）']['id']}/members")).json()
    assert [(m["character_name"], m["position"]) for m in members] == [("林远（导入2）", "弟子"), ("张三", "长老")]


async def test_import_skip_links_to_existing(client):
    """跳过同名项时，关系 / 成员关系映射到目标项目已有的同名对象；已有关系的角色对跳过并记入 warnings。"""
    await register(client)
    base, chars, org = await _source_project(client)
    card = (
        await client.post(
            f"{base}/cards/export", json={"character_ids": [c["id"] for c in chars], "organization_ids": [org["id"]]}
        )
    ).json()
    p2 = await create_project(client, title="乙")
    base2 = f"/api/v1/projects/{p2['id']}"
    lin = await _char(client, base2, "林远")
    zhang = await _char(client, base2, "张三")
    await client.post(f"{base2}/relationships", json={"a_id": lin["id"], "b_id": zhang["id"], "role_a": "朋友"})
    qy = (await client.post(f"{base2}/organizations", json={"name": "青云宗"})).json()

    result = (await client.post(f"{base2}/cards/import", json={"data": card})).json()
    assert [c["action"] for c in result["characters"]] == ["skipped", "skipped", "created"]
    assert result["characters"][0]["id"] == lin["id"]
    assert result["relationships_created"] == 1
    assert any("之间已有关系" in w for w in result["warnings"])
    li = next(c for c in (await client.get(f"{base2}/characters")).json() if c["name"] == "李四")
    rels = (await client.get(f"{base2}/relationships", params={"character_id": li["id"]})).json()
    assert [(r["other_id"], r["other_role"]) for r in rels] == [(lin["id"], "宿命之敌")]
    members = (await client.get(f"{base2}/organizations/{qy['id']}/members")).json()
    assert {m["character_id"] for m in members} == {lin["id"], zhang["id"]}


async def test_import_unresolved_and_parent_cycle(client):
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    card = {
        "format": "ainovel.cards",
        "version": 1,
        "characters": [{"name": "甲", "id": 1, "life_status": "unknown"}, {"name": "甲"}, {"name": "丙"}],
        "organizations": [
            {"name": "A", "parent_name": "B", "members": [{"character_name": "无名氏"}]},
            {"name": "B", "parent_name": "A"},
            {"name": "C", "parent_name": "不存在"},
        ],
        "relationships": [
            {"a_name": "甲", "b_name": "乙", "role_a": "朋友", "role_b": "朋友"},
            {"a_name": "甲", "b_name": "甲", "role_a": "朋友", "role_b": "朋友"},
            # 只有空白的称呼：跳过并记 warning，不中断整个导入
            {"a_name": "甲", "b_name": "丙", "role_a": "  ", "role_b": " "},
        ],
    }
    pv = (await client.post(f"{base}/cards/import/preview", json={"data": card})).json()
    assert [r["status"] for r in pv["relationships"]] == ["unresolved", "unresolved", "unresolved"]
    assert any("重复的角色「甲」" in w for w in pv["warnings"])
    assert any("无名氏" in w for w in pv["warnings"])

    result = (await client.post(f"{base}/cards/import", json={"data": card})).json()
    assert len(result["characters"]) == 2 and result["relationships_created"] == 0
    assert any("循环引用" in w for w in result["warnings"])
    assert any("缺少称呼" in w for w in result["warnings"])
    orgs = {o["name"]: o for o in (await client.get(f"{base}/organizations")).json()}
    ids = {o["id"] for o in orgs.values()}
    # A 挂到 B 下，B → A 会成环被忽略；C 的上级不存在
    assert orgs["A"]["parent_id"] == orgs["B"]["id"] and orgs["B"]["parent_id"] is None
    assert orgs["C"]["parent_id"] is None and orgs["A"]["parent_id"] in ids
    chars = (await client.get(f"{base}/characters")).json()
    assert [(c["name"], c["life_status"]) for c in chars] == [("甲", "alive"), ("丙", "alive")]


async def test_import_rejects_bad_files(client):
    """AC10：格式错误 / 超过大小上限返回 400 中文提示。"""
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    cases = [
        ([1, 2], "应为 JSON 对象"),
        ({"format": "other", "version": 1}, "不是有效的卡片文件"),
        ({"format": "ainovel.cards", "version": 4}, "不支持的卡片版本"),
        ({"format": "ainovel.cards", "version": True}, "不支持的卡片版本"),
        ({"format": "ainovel.cards", "version": 2, "careers": [{"name": "剑修", "stages": []}]}, "卡片文件格式不正确"),
        ({"format": "ainovel.cards", "version": 1, "characters": [{"name": ""}]}, "卡片文件格式不正确"),
        ({"format": "ainovel.cards", "version": 1, "characters": "x"}, "卡片文件格式不正确"),
        (
            {"format": "ainovel.cards", "version": 1, "characters": [{"name": "大", "profile": "字" * (1024 * 1024)}]},
            "卡片文件过大",
        ),
    ]
    for data, msg in cases:
        for url in (f"{base}/cards/import/preview", f"{base}/cards/import"):
            resp = await client.post(url, json={"data": data})
            assert resp.status_code == 400, (data if len(str(data)) < 200 else url, resp.text[:200])
            assert msg in resp.json()["detail"]
    assert (await client.get(f"{base}/characters")).json() == []
    resp = await client.post(f"{base}/cards/export", json={"character_ids": [99999]})
    assert resp.status_code == 400 and resp.json()["detail"] == "部分角色不存在或不属于该项目"


def test_rename_for():
    assert rename_for("甲", {"甲"}) == "甲（导入）"
    assert rename_for("甲", {"甲", "甲（导入）", "甲（导入2）"}) == "甲（导入3）"
    long = "长" * 100
    renamed = rename_for(long, {long})
    assert len(renamed) == 100 and renamed.endswith("（导入）")


async def _career(client, base, name, stages, kind="main", **kw):
    resp = await client.post(
        f"{base}/careers", json={"name": name, "kind": kind, "stages": [{"name": s} for s in stages], **kw}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_cards_v2_careers_roundtrip(client):
    """AC7：甲导出挂有职业的角色 → 乙：没有的职业新建，同名职业复用并补齐阶段；挂接来源 import。"""
    await register(client)
    p = await create_project(client, title="甲")
    base = f"/api/v1/projects/{p['id']}"
    sword = await _career(client, base, "剑修", ["练气", "筑基", "金丹"], rules="以剑入道")
    dan = await _career(client, base, "丹师", ["一品", "二品"], kind="sub")
    await _career(client, base, "未使用", ["一"])
    lin = await _char(client, base, "林远")
    zhang = await _char(client, base, "张三")
    await client.post(
        f"{base}/characters/{lin['id']}/careers",
        json={"career_id": sword["id"], "stage_id": sword["stages"][2]["id"], "note": "初入"},
    )
    await client.post(f"{base}/characters/{lin['id']}/careers", json={"career_id": dan["id"]})
    await client.post(f"{base}/characters/{zhang['id']}/careers", json={"career_id": dan["id"]})

    card = (await client.post(f"{base}/cards/export", json={"character_ids": [lin["id"], zhang["id"]]})).json()
    assert card["version"] == 3 and not _id_keys(card)
    assert card["characters"][0]["careers"] == [
        {"career_name": "剑修", "role": "main", "stage_name": "金丹", "note": "初入"},
        {"career_name": "丹师", "role": "sub", "stage_name": "一品", "note": ""},
    ]
    assert [c["name"] for c in card["careers"]] == ["剑修", "丹师"]
    assert card["careers"][0]["rules"] == "以剑入道"
    assert [s["name"] for s in card["careers"][0]["stages"]] == ["练气", "筑基", "金丹"]

    # 乙：已有同名「剑修」但只有两阶
    p2 = await create_project(client, title="乙")
    base2 = f"/api/v1/projects/{p2['id']}"
    sword2 = await _career(client, base2, "剑修", ["练气", "筑基"])
    pv = (await client.post(f"{base2}/cards/import/preview", json={"data": card})).json()
    assert [(c["name"], c["status"], c["stages_added"]) for c in pv["careers"]] == [
        ("剑修", "reuse", ["金丹"]),
        ("丹师", "new", []),
    ]

    result = (await client.post(f"{base2}/cards/import", json={"data": card})).json()
    assert [(c["name"], c["action"], c["stages_added"]) for c in result["careers"]] == [
        ("剑修", "reused", 1),
        ("丹师", "created", 0),
    ]
    assert result["careers_assigned"] == 3 and result["warnings"] == []
    careers = {c["name"]: c for c in (await client.get(f"{base2}/careers")).json()}
    assert careers["剑修"]["id"] == sword2["id"]
    assert [s["name"] for s in careers["剑修"]["stages"]] == ["练气", "筑基", "金丹"]
    assert careers["丹师"]["kind"] == "sub"
    new_lin = next(c for c in (await client.get(f"{base2}/characters")).json() if c["name"] == "林远")
    assigned = (await client.get(f"{base2}/characters/{new_lin['id']}/careers")).json()
    assert [(a["career_name"], a["stage_name"], a["stage_position"], a["note"]) for a in assigned] == [
        ("剑修", "金丹", 3, "初入"),
        ("丹师", "一品", 1, ""),
    ]
    hist = (await client.get(f"{base2}/characters/{new_lin['id']}/career-history")).json()
    assert {h["source"] for h in hist} == {"import"}

    # 再导入一次（跳过同名角色 = 关联已有）：已挂的职业跳过并给警告
    again = (await client.post(f"{base2}/cards/import", json={"data": card})).json()
    assert again["careers_assigned"] == 0
    assert sum("该角色已挂该职业" in w for w in again["warnings"]) == 3


async def test_cards_v2_unresolved_careers(client):
    """挂接指向未定义的职业、类别不符、阶段名对不上（追加到末尾）；v1 卡片照常导入。"""
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    await _career(client, base, "剑修", ["练气"])
    card = {
        "format": "ainovel.cards",
        "version": 2,
        "characters": [
            {
                "name": "甲",
                "careers": [
                    {"career_name": "不存在", "stage_name": "一"},
                    {"career_name": "剑修", "role": "sub", "stage_name": "练气"},
                ],
            },
            {"name": "乙", "careers": [{"career_name": "剑修", "stage_name": "化神"}]},
        ],
    }
    pv = (await client.post(f"{base}/cards/import/preview", json={"data": card})).json()
    assert pv["careers"] == [] and any("不存在" in w for w in pv["warnings"])
    result = (await client.post(f"{base}/cards/import", json={"data": card})).json()
    assert result["careers_assigned"] == 1
    assert any("「不存在」无法关联" in w for w in result["warnings"])
    assert any("主修职业只能作为主职业" in w for w in result["warnings"])
    career = (await client.get(f"{base}/careers")).json()[0]
    assert [s["name"] for s in career["stages"]] == ["练气", "化神"]

    v1 = {"format": "ainovel.cards", "version": 1, "characters": [{"name": "丙"}]}
    result = (await client.post(f"{base}/cards/import", json={"data": v1})).json()
    assert [c["action"] for c in result["characters"]] == ["created"]
    assert (result["careers"], result["careers_assigned"]) == ([], 0)


async def test_cards_v3_custom_text_fields_roundtrip(client, app):
    """文本自定义字段作为无内部 ID 的角色快照跨作品导入导出。"""
    await register(client)
    src = await create_project(client, title="自定义字段来源")
    src_base = f"/api/v1/projects/{src['id']}"
    original = {
        "format": "ainovel.cards",
        "version": 3,
        "characters": [
            {
                "name": "林远",
                "current_state": "金丹期",
                "custom_text_fields": [
                    {"name": "功法", "value": "九阳真经\n练至大成"},
                    {"name": "随身物", "value": ""},
                ],
            }
        ],
    }
    preview = await client.post(f"{src_base}/cards/import/preview", json={"data": original})
    assert preview.status_code == 200, preview.text
    imported = await client.post(f"{src_base}/cards/import", json={"data": original})
    assert imported.status_code == 200, imported.text
    char_id = imported.json()["characters"][0]["id"]
    exported = await client.post(f"{src_base}/cards/export", json={"character_ids": [char_id]})
    assert exported.status_code == 200, exported.text
    card = exported.json()
    assert card["version"] == 3
    assert not _id_keys(card)
    assert card["characters"][0]["current_state"] == "金丹期"
    assert card["characters"][0]["custom_text_fields"] == [
        {"name": "功法", "value": "九阳真经\n练至大成"},
        {"name": "随身物", "value": ""},
    ]
    async with app.state.sessionmaker() as db:
        source_timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.subject_kind == "character", SettingTimeline.character_id == char_id
            )
        )
        assert source_timeline is not None
        assert source_timeline.baseline_status == "confirmed" and not source_timeline.legacy_compat_allowed
        source_fields = list(
            await db.scalars(select(SettingTextField).where(SettingTextField.timeline_id == source_timeline.id))
        )
        source_field_ids = {item.id for item in source_fields}

    target = await create_project(client, title="自定义字段目标")
    target_base = f"/api/v1/projects/{target['id']}"
    restored = await client.post(f"{target_base}/cards/import", json={"data": card})
    assert restored.status_code == 200, restored.text
    new_id = restored.json()["characters"][0]["id"]
    assert new_id != char_id
    again = await client.post(f"{target_base}/cards/export", json={"character_ids": [new_id]})
    assert again.status_code == 200, again.text
    assert again.json()["characters"][0]["custom_text_fields"] == card["characters"][0]["custom_text_fields"]
    async with app.state.sessionmaker() as db:
        target_timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.subject_kind == "character", SettingTimeline.character_id == new_id
            )
        )
        assert target_timeline is not None
        assert target_timeline.baseline_status == "confirmed" and not target_timeline.legacy_compat_allowed
        target_fields = list(
            await db.scalars(select(SettingTextField).where(SettingTextField.timeline_id == target_timeline.id))
        )
        assert source_field_ids.isdisjoint(item.id for item in target_fields)
        assert {(item.label, item.base_value) for item in target_fields} == {
            ("功法", "九阳真经\n练至大成"),
            ("随身物", ""),
        }
        existing_event = await db.scalar(
            select(SettingProgression.id).where(SettingProgression.timeline_id == target_timeline.id)
        )
        assert existing_event is None


async def test_cards_v3_conflict_warns_that_skipped_text_fields_are_not_applied(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    existing = await _char(client, base, "林远", profile="原有档案")
    card = {
        "format": "ainovel.cards",
        "version": 3,
        "characters": [{"name": "林远", "custom_text_fields": [{"name": "功法", "value": "九阳"}]}],
    }
    preview = await client.post(f"{base}/cards/import/preview", json={"data": card})
    assert preview.status_code == 200, preview.text
    assert preview.json()["characters"][0]["status"] == "conflict"
    assert any("跳过" in warning and "自定义字段" in warning for warning in preview.json()["warnings"])

    result = await client.post(f"{base}/cards/import", json={"data": card})
    assert result.status_code == 200, result.text
    assert result.json()["characters"][0]["action"] == "skipped"
    assert any("自定义字段" in warning for warning in result.json()["warnings"])
    after = await client.get(f"{base}/characters")
    assert [(item["id"], item["profile"]) for item in after.json()] == [(existing["id"], "原有档案")]


async def test_cards_v3_text_field_validation_and_legacy_compatibility(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    invalid_fields = [
        [{"name": "   ", "value": "无名"}],
        [{"name": "长" * 101, "value": "过长"}],
        [{"name": "功法", "value": "一"}, {"name": " 功法 ", "value": "二"}],
        [{"name": "功法", "value": {"text": "不是文本"}}],
    ]
    for fields in invalid_fields:
        data = {"format": "ainovel.cards", "version": 3, "characters": [{"name": "甲", "custom_text_fields": fields}]}
        for endpoint in ("import/preview", "import"):
            resp = await client.post(f"{base}/cards/{endpoint}", json={"data": data})
            assert resp.status_code == 400, resp.text
            assert "卡片文件格式不正确" in resp.json()["detail"]
    assert (await client.get(f"{base}/characters")).json() == []

    # v1/v2 从未定义新字段，继续忽略同名的未知附加数据。
    for version in (1, 2):
        data = {
            "format": "ainovel.cards",
            "version": version,
            "characters": [{"name": "旧角色", "custom_text_fields": {"not": "a list"}}],
        }
        response = await client.post(f"{base}/cards/import/preview", json={"data": data})
        assert response.status_code == 200, response.text


async def test_cards_v3_exports_effective_custom_fields_not_base_or_inactive_events(client, app):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    char = await _char(client, base, "林远", current_state="金丹")

    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project["id"],
                SettingTimeline.subject_kind == "character",
                SettingTimeline.character_id == char["id"],
            )
        )
        if timeline is None:
            character = await db.get(Character, char["id"])
            timeline = SettingTimeline(
                project_id=project["id"],
                subject_kind="character",
                character_id=character.id,
                baseline={"current_state": character.current_state},
                baseline_status="confirmed",
            )
            db.add(timeline)
            await db.flush()
        method = SettingTextField(timeline_id=timeline.id, label="功法", base_value="练气", sort=1)
        sword = SettingTextField(timeline_id=timeline.id, label="武器", base_value="玄铁剑", sort=2)
        db.add_all([method, sword])
        await db.flush()
        db.add_all(
            [
                SettingProgression(
                    timeline_id=timeline.id,
                    custom_field_id=method.id,
                    operation="replace",
                    value="金丹",
                    anchor_kind="planned",
                    chapter_no=20,
                ),
                SettingProgression(
                    timeline_id=timeline.id,
                    custom_field_id=method.id,
                    operation="replace",
                    value="不应出现的已撤销阶段",
                    anchor_kind="planned",
                    chapter_no=21,
                    active=False,
                ),
                SettingProgression(
                    timeline_id=timeline.id,
                    custom_field_id=sword.id,
                    operation="append",
                    value="刻有纹路",
                    anchor_kind="planned",
                    chapter_no=10,
                ),
                SettingProgression(
                    timeline_id=timeline.id,
                    custom_field_id=sword.id,
                    operation="replace",
                    value="不应出现的孤儿变化",
                    anchor_kind="chapter",
                    chapter_id=None,
                ),
            ]
        )
        await db.commit()

    response = await client.post(f"{base}/cards/export", json={"character_ids": [char["id"]]})
    assert response.status_code == 200, response.text
    card = response.json()
    assert card["version"] == 3 and not _id_keys(card)
    assert card["characters"][0]["custom_text_fields"] == [
        {"name": "功法", "value": "金丹"},
        {"name": "武器", "value": "玄铁剑\n刻有纹路"},
    ]


async def test_cards_v3_excludes_relations_with_absent_latest_state(client, app):
    await register(client)
    base, chars, org = await _source_project(client)
    async with app.state.sessionmaker() as db:
        rel = await db.scalar(
            select(Relationship).where(
                Relationship.pair_low == min(chars[0]["id"], chars[1]["id"]),
                Relationship.pair_high == max(chars[0]["id"], chars[1]["id"]),
            )
        )
        assert rel is not None
        rel.is_present = False
        await db.commit()
    resp = await client.post(
        f"{base}/cards/export", json={"character_ids": [c["id"] for c in chars], "organization_ids": [org["id"]]}
    )
    assert resp.status_code == 200, resp.text
    assert {(r["a_name"], r["b_name"]) for r in resp.json()["relationships"]} == {("林远", "李四")}


async def test_cards_v3_skip_and_rename_preserve_target_fields_and_project_isolation(client, make_client):
    await register(client)
    source = await create_project(client, title="来源")
    target = await create_project(client, title="目标")
    source_base = f"/api/v1/projects/{source['id']}"
    target_base = f"/api/v1/projects/{target['id']}"

    def card(value):
        return {
            "format": "ainovel.cards",
            "version": 3,
            "characters": [{"name": "林远", "custom_text_fields": [{"name": "功法", "value": value}]}],
        }

    source_result = await client.post(f"{source_base}/cards/import", json={"data": card("九阳")})
    target_result = await client.post(f"{target_base}/cards/import", json={"data": card("九阴")})
    assert source_result.status_code == target_result.status_code == 200
    source_id = source_result.json()["characters"][0]["id"]
    target_id = target_result.json()["characters"][0]["id"]
    source_card = (await client.post(f"{source_base}/cards/export", json={"character_ids": [source_id]})).json()

    skipped = await client.post(f"{target_base}/cards/import", json={"data": source_card})
    assert skipped.status_code == 200 and skipped.json()["characters"][0]["action"] == "skipped"
    assert any("自定义字段" in warning for warning in skipped.json()["warnings"])
    unchanged = await client.post(f"{target_base}/cards/export", json={"character_ids": [target_id]})
    assert unchanged.json()["characters"][0]["custom_text_fields"] == [{"name": "功法", "value": "九阴"}]

    renamed = await client.post(
        f"{target_base}/cards/import",
        json={"data": source_card, "resolutions": {"characters": {"林远": "rename"}}},
    )
    assert renamed.status_code == 200 and renamed.json()["characters"][0]["action"] == "renamed"
    renamed_id = renamed.json()["characters"][0]["id"]
    copied = await client.post(f"{target_base}/cards/export", json={"character_ids": [renamed_id]})
    assert copied.json()["characters"][0]["custom_text_fields"] == [{"name": "功法", "value": "九阳"}]

    cross_project = await client.post(f"{target_base}/cards/export", json={"character_ids": [source_id]})
    assert cross_project.status_code == 400
    outsider = make_client()
    await register(outsider, "other_user")
    cross_user = await outsider.post(f"{source_base}/cards/export", json={"character_ids": [source_id]})
    assert cross_user.status_code == 404


async def test_cards_v1_v2_import_creates_confirmed_baselines_without_custom_fields(client, app):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    imported_ids = []
    for version in (1, 2):
        data = {
            "format": "ainovel.cards",
            "version": version,
            "characters": [{"name": f"旧卡角色{version}", "current_state": "练气"}],
        }
        response = await client.post(f"{base}/cards/import", json={"data": data})
        assert response.status_code == 200, response.text
        imported_ids.append(response.json()["characters"][0]["id"])
    exported = await client.post(f"{base}/cards/export", json={"character_ids": imported_ids})
    assert all(item["custom_text_fields"] == [] for item in exported.json()["characters"])
    async with app.state.sessionmaker() as db:
        rows = list(
            await db.scalars(
                select(SettingTimeline).where(
                    SettingTimeline.subject_kind == "character", SettingTimeline.character_id.in_(imported_ids)
                )
            )
        )
        assert len(rows) == 2
        assert all(row.baseline_status == "confirmed" and row.baseline["current_state"] == "练气" for row in rows)
        fields = await db.scalars(
            select(SettingTextField).where(SettingTextField.timeline_id.in_([r.id for r in rows]))
        )
        assert not list(fields)


async def test_cards_v3_import_rolls_back_characters_and_fields_on_mid_import_failure(client, app, monkeypatch):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    original_add = setting_progression_service.add_initial_text_fields

    async def fail_second_field(db, timeline, fields):
        if fields and fields[0][0] == "第二项":
            raise BadRequestError("模拟字段写入失败")
        return await original_add(db, timeline, fields)

    monkeypatch.setattr(setting_progression_service, "add_initial_text_fields", fail_second_field)
    data = {
        "format": "ainovel.cards",
        "version": 3,
        "characters": [
            {"name": "甲", "custom_text_fields": [{"name": "第一项", "value": "已写入"}]},
            {"name": "乙", "custom_text_fields": [{"name": "第二项", "value": "失败"}]},
        ],
    }
    response = await client.post(f"{base}/cards/import", json={"data": data})
    assert response.status_code == 400 and response.json()["detail"] == "模拟字段写入失败"
    assert (await client.get(f"{base}/characters")).json() == []
    async with app.state.sessionmaker() as db:
        timelines = list(await db.scalars(select(SettingTimeline).where(SettingTimeline.project_id == project["id"])))
        assert timelines == []


async def test_cards_import_skips_hidden_relationship_without_overwriting_history(client, app):
    """卡片没有章节信息，不猜测隐藏关系何时恢复，也不覆盖原历史。"""
    await register(client)
    source_base, source_chars, _ = await _source_project(client)
    exported = await client.post(
        f"{source_base}/cards/export", json={"character_ids": [source_chars[0]["id"], source_chars[1]["id"]]}
    )
    assert exported.status_code == 200, exported.text
    project = await create_project(client, title="目标")
    base = f"/api/v1/projects/{project['id']}"
    a = await _char(client, base, "林远")
    b = await _char(client, base, "张三")
    created = await client.post(
        f"{base}/relationships", json={"a_id": a["id"], "b_id": b["id"], "role_a": "朋友"}
    )
    assert created.status_code == 201, created.text
    relation_id = created.json()["id"]
    async with app.state.sessionmaker() as db:
        relation = await db.get(Relationship, relation_id)
        assert relation is not None
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project["id"],
                SettingTimeline.subject_kind == "relationship",
                SettingTimeline.character_id == min(a["id"], b["id"]),
                SettingTimeline.related_character_id == max(a["id"], b["id"]),
            )
        )
        if timeline is None:
            timeline = SettingTimeline(
                project_id=project["id"],
                subject_kind="relationship",
                character_id=min(a["id"], b["id"]),
                related_character_id=max(a["id"], b["id"]),
                baseline={
                    "exists": True,
                    "identity": {"a_id": relation.a_id, "b_id": relation.b_id, "type_id": relation.type_id},
                    "intimacy": relation.intimacy,
                    "status": relation.status,
                    "description": relation.description,
                },
            )
            db.add(timeline)
            await db.flush()
        event = SettingProgression(
            timeline_id=timeline.id,
            field_key="presence",
            operation="replace",
            value={"exists": False},
            anchor_kind="planned",
            chapter_no=20,
        )
        db.add(event)
        relation.is_present = False
        await db.flush()
        timeline_id = timeline.id
        event_id = event.id
        history_before = [
            (item.id, item.changes)
            for item in await db.scalars(
                select(RelationshipHistory).where(RelationshipHistory.relationship_id == relation_id)
            )
        ]
        await db.commit()

    result = await client.post(f"{base}/cards/import", json={"data": exported.json()})
    assert result.status_code == 200, result.text
    assert result.json()["relationships_created"] == 0
    assert any("关系历史" in warning and "跳过" in warning for warning in result.json()["warnings"])
    async with app.state.sessionmaker() as db:
        relation = await db.get(Relationship, relation_id)
        assert relation is not None and not relation.is_present
        history_after = [
            (item.id, item.changes)
            for item in await db.scalars(
                select(RelationshipHistory).where(RelationshipHistory.relationship_id == relation_id)
            )
        ]
        assert history_after == history_before
        events = list(await db.scalars(select(SettingProgression).where(SettingProgression.timeline_id == timeline_id)))
        assert [(item.id, item.active, item.value) for item in events] == [(event_id, True, {"exists": False})]


async def test_cards_import_creates_confirmed_membership_baseline(client, app):
    """新导入成员建立明确基础，成员行与时间线属于同一事务。"""
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    card = {
        "format": "ainovel.cards",
        "version": 3,
        "characters": [{"name": "林远"}],
        "organizations": [
            {
                "name": "青云宗",
                "members": [{"character_name": "林远", "position": "内门弟子", "status": "active", "note": "持令牌"}],
            }
        ],
    }
    response = await client.post(f"{base}/cards/import", json={"data": card})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["members_created"] == 1
    character_id = body["characters"][0]["id"]
    org_id = body["organizations"][0]["id"]
    async with app.state.sessionmaker() as db:
        member = await db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id, OrganizationMember.character_id == character_id
            )
        )
        assert member is not None and (member.position, member.status, member.note) == ("内门弟子", "active", "持令牌")
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project["id"],
                SettingTimeline.subject_kind == "membership",
                SettingTimeline.organization_id == org_id,
                SettingTimeline.character_id == character_id,
            )
        )
        assert timeline is not None
        assert timeline.baseline_status == "confirmed" and not timeline.legacy_compat_allowed
        assert timeline.baseline == {"exists": True, "position": "内门弟子", "status": "active", "note": "持令牌"}
        event = await db.scalar(select(SettingProgression.id).where(SettingProgression.timeline_id == timeline.id))
        assert event is None


async def test_cards_import_skips_exited_membership_without_changing_history(client, app):
    """已退出的成员仅有历史主体时，卡片不推测其重新加入的章节。"""
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = await _char(client, base, "林远")
    org_response = await client.post(f"{base}/organizations", json={"name": "青云宗"})
    assert org_response.status_code == 201, org_response.text
    org = org_response.json()
    member_response = await client.post(
        f"{base}/organizations/{org['id']}/members",
        json={"character_id": character["id"], "position": "内门弟子", "note": "原始备注"},
    )
    assert member_response.status_code == 201, member_response.text
    member_id = member_response.json()["id"]
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project["id"],
                SettingTimeline.subject_kind == "membership",
                SettingTimeline.organization_id == org["id"],
                SettingTimeline.character_id == character["id"],
            )
        )
        assert timeline is not None
        timeline_id, revision = timeline.id, timeline.revision
    removed = await client.delete(
        f"{base}/organizations/{org['id']}/members/{member_id}",
        params={"edit_mode": "progression", "expected_revision": revision, "chapter_no": 20},
    )
    assert removed.status_code == 204, removed.text
    assert (await client.get(f"{base}/organizations/{org['id']}/members")).json() == []
    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, timeline_id)
        assert timeline is not None
        before = (dict(timeline.baseline), timeline.revision, timeline.baseline_status, timeline.legacy_compat_allowed)
        events_before = [
            (e.id, e.field_key, e.value, e.active, e.chapter_no)
            for e in await db.scalars(select(SettingProgression).where(SettingProgression.timeline_id == timeline_id))
        ]
        assert len(events_before) == 1 and events_before[0][2] == {"exists": False}

    card = {
        "format": "ainovel.cards",
        "version": 3,
        "characters": [{"name": "林远"}],
        "organizations": [
            {"name": "青云宗", "members": [{"character_name": "林远", "position": "掌门", "note": "来自卡片"}]}
        ],
    }
    result = await client.post(f"{base}/cards/import", json={"data": card})
    assert result.status_code == 200, result.text
    assert result.json()["members_created"] == 0
    assert any("成员历史" in warning and "跳过" in warning for warning in result.json()["warnings"])
    assert (await client.get(f"{base}/organizations/{org['id']}/members")).json() == []
    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, timeline_id)
        assert timeline is not None
        after = (dict(timeline.baseline), timeline.revision, timeline.baseline_status, timeline.legacy_compat_allowed)
        assert after == before
        events_after = [
            (e.id, e.field_key, e.value, e.active, e.chapter_no)
            for e in await db.scalars(select(SettingProgression).where(SettingProgression.timeline_id == timeline_id))
        ]
        assert events_after == events_before


async def test_cards_import_rolls_back_if_membership_creation_fails(client, app, monkeypatch):
    """卡片中途写成员失败时，不保留此前写入的角色、字段、组织和成员。"""
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    original = organization_service.new_member

    async def fail_second_member(db, org, data):
        if data.position == "触发失败":
            raise BadRequestError("模拟成员写入失败")
        return await original(db, org, data)

    monkeypatch.setattr(organization_service, "new_member", fail_second_member)
    card = {
        "format": "ainovel.cards",
        "version": 3,
        "characters": [
            {"name": "甲", "custom_text_fields": [{"name": "功法", "value": "练气"}]},
            {"name": "乙"},
        ],
        "organizations": [
            {
                "name": "青云宗",
                "members": [
                    {"character_name": "甲", "position": "弟子"},
                    {"character_name": "乙", "position": "触发失败"},
                ],
            }
        ],
    }
    response = await client.post(f"{base}/cards/import", json={"data": card})
    assert response.status_code == 400 and response.json()["detail"] == "模拟成员写入失败"
    assert (await client.get(f"{base}/characters")).json() == []
    assert (await client.get(f"{base}/organizations")).json() == []
    async with app.state.sessionmaker() as db:
        assert not list(await db.scalars(select(SettingTimeline).where(SettingTimeline.project_id == project["id"])))
        assert not list(await db.scalars(select(Organization).where(Organization.project_id == project["id"])))
