"""父集成：v5导入新作品后，四实体四类型字段与分类重做同一组时点验收。"""

from copy import deepcopy

import pytest

from tests.helpers import create_project, register
from tests.test_category_cards import _tags
from tests.test_project_transfer import _backup, _import
from tests.test_setting_categories import _create, _entry
from tests.test_setting_fields import _definition, _versions

ENTRIES = {
    "character": ("character", "林岚"),
    "world": ("world.place", "云港"),
    "organization": ("organization", "青云宗"),
    "career": ("career", "剑修"),
}
FIELD_TYPES = {"成长纪要": "text", "阵营口令": "line", "阵营": "dropdown", "参照档案": "reference"}
CATEGORIES = {"all": "同盟且新阵营", "any": "同盟或新阵营"}
PHASES = {(5, "start"): "before", (20, "start"): "before", (20, "end"): "after", (21, "start"): "after"}
TAGGED = ("character", "world")
TAG_REASON = ("tag", "同盟")
DROPDOWN_REASON = ("dropdown", "阵营", "新阵营")


def _tags_for(kind):
    return ("同盟" if kind in TAGGED else "旁观", f"{ENTRIES[kind][1]}当前整理")


async def _fields(client, index, kind, **position):
    ref = index["entries"][kind]["reference"]
    response = await client.get(
        f"{index['base']}/setting-entries/{kind}/{ref['id']}/fields", params=position,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _write_baseline(client, index, kind, label, value):
    current = await _fields(client, index, kind)
    ref = index["entries"][kind]["reference"]
    definition = index["definitions"][label]
    response = await client.patch(
        f"{index['base']}/setting-entries/{kind}/{ref['id']}/fields/{definition['id']}/baseline",
        json={**_versions(current), "field_type": definition["field_type"], "value": value},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _index(client, project_id):
    base = f"/api/v1/projects/{project_id}"
    rows = {}
    for resource in ("setting-entries", "setting-field-definitions", "setting-categories", "chapters"):
        response = await client.get(f"{base}/{resource}")
        assert response.status_code == 200, response.text
        rows[resource] = response.json()
    entries = rows["setting-entries"]["items"]
    assert len(entries) == 4
    assert {(item["type"], item["name"]) for item in entries} == set(ENTRIES.values())
    definitions = rows["setting-field-definitions"]["items"]
    categories = rows["setting-categories"]["items"]
    assert {item["label"]: item["field_type"] for item in definitions} == FIELD_TYPES
    assert {item["name"] for item in categories} == set(CATEGORIES.values())
    return {
        "base": base,
        "entries": {item["reference"]["kind"]: item for item in entries},
        "definitions": {item["label"]: item for item in definitions},
        "categories": {item["name"]: item for item in categories},
        "chapters": {item["chapter_no"]: item["id"] for item in rows["chapters"]},
    }


async def _seed(client):
    await register(client)
    project = await create_project(client, title="字段分类往返", tags=["项目题材标签"])
    base = f"/api/v1/projects/{project['id']}"
    entries = {kind: await _entry(client, base, kind, name) for kind, (_, name) in ENTRIES.items()}
    chapters = {}
    for number in (5, 20, 21):
        response = await client.post(f"{base}/chapters", json={"chapter_no": number, "title": f"章节{number}"})
        assert response.status_code == 201, response.text
        chapters[number] = response.json()["id"]
    definitions, revision = {}, 0
    for label, field_type in FIELD_TYPES.items():
        extra = {"reference_types": ["world.place", "organization"]} if field_type == "reference" else {}
        created = await _definition(client, base, revision, label=label, field_type=field_type,
                                    applicable_types=[entry_type for entry_type, _ in ENTRIES.values()], **extra)
        definitions[label], revision = created["item"], created["fields_revision"]
    options = {}
    for label in ("旧阵营", "新阵营"):
        response = await client.post(f"{base}/setting-field-definitions/{definitions['阵营']['id']}/options",
                                     json={"expected_fields_revision": revision, "label": label})
        assert response.status_code == 201, response.text
        options[label], revision = response.json()["item"]["id"], response.json()["fields_revision"]
    # 使用真实HTTP原子首写／事件入口；reference公开值只传本作品EntityRef。
    index = {"base": base, "definitions": definitions,
             "entries": {kind: {"reference": {"kind": kind, "id": row["id"]}} for kind, row in entries.items()}}
    for kind, (_, name) in ENTRIES.items():
        initial = {"成长纪要": f"{name}起始纪要\n保留原文", "阵营口令": f"{name}旧口令",
                   "阵营": {"option_id": options["旧阵营"]},
                   "参照档案": {"kind": "world", "id": entries["world"]["id"]}}
        later = {"成长纪要": f"{name}二十章纪要\n新行", "阵营口令": f"{name}新口令",
                 "阵营": {"option_id": options["新阵营"]},
                 "参照档案": {"kind": "organization", "id": entries["organization"]["id"]}}
        for label, field_type in FIELD_TYPES.items():
            baseline = await _write_baseline(client, index, kind, label, initial[label])
            response = await client.post(
                f"{base}/setting-entries/{kind}/{entries[kind]['id']}/fields/{definitions[label]['id']}/progressions",
                json={**_versions(baseline), "field_type": field_type, "value": later[label],
                      "operation": "replace", "anchor_kind": "chapter", "chapter_id": chapters[20],
                      "boundary": "end"},
            )
            assert response.status_code == 201, response.text
    # 在剧情变化之后编辑当前标签；历史位置也必须使用这份当前整理信息。
    for kind, row in entries.items():
        await _tags(client, base, kind, row["id"], list(_tags_for(kind)))
    for mode, name in CATEGORIES.items():
        await _create(client, base, name=name, match_mode=mode,
                      applicable_types=[entry_type for entry_type, _ in ENTRIES.values()], rules=[
                          {"kind": "tag", "tag": "同盟"},
                          {"kind": "dropdown", "definition_id": definitions["阵营"]["id"],
                           "option_id": options["新阵营"]},
                      ])
    return project, await _index(client, project["id"])


def _value(slot, key, index):
    value = slot[key]
    if value is None:
        return None
    if slot["field_type"] == "dropdown":
        return {option["id"]: option["label"] for option in slot["options"]}[value["option_id"]]
    if slot["field_type"] == "reference":
        summary = slot["reference" if key == "value" else "base_reference"]
        target = index["entries"][value["kind"]]
        assert summary["reference"] == value == target["reference"]
        assert (summary["type"], summary["name"]) == (target["type"], target["name"])
        return target["type"], target["name"]
    return value


async def _point(client, index, chapter_no, boundary):
    position = {"chapter_id": index["chapters"][chapter_no], "boundary": boundary}
    result = {"entries": {}, "categories": {}}
    for kind, entry in index["entries"].items():
        data = await _fields(client, index, kind, **position)
        assert data["position"]["chapter_no"] == chapter_no and data["position"]["boundary"] == boundary
        assert data["entry"] == entry and data["legacy_fields"] == []
        tags = await client.get(f"{index['base']}/setting-entries/{kind}/{entry['reference']['id']}/tags")
        assert tags.status_code == 200, tags.text
        slots = {}
        for slot in data["fields"]:
            assert slot["definition_id"] == index["definitions"][slot["label"]]["id"]
            assert slot["field_id"] is not None
            slots[slot["label"]] = slot["field_type"], _value(slot, "base_value", index), _value(slot, "value", index)
        result["entries"][entry["type"], entry["name"]] = {"tags": tuple(tags.json()["setting_tags"]), "fields": slots}
    for name, category in index["categories"].items():
        response = await client.get(f"{index['base']}/setting-categories/{category['id']}/entries", params=position)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["position"]["chapter_no"] == chapter_no and page["position"]["boundary"] == boundary
        members = {}
        for item in page["items"]:
            assert item["reference"] == index["entries"][item["reference"]["kind"]]["reference"]
            reasons = []
            for reason in item["reasons"]:
                assert reason["rule_id"] in {rule["id"] for rule in category["rules"]}
                reasons.append(("tag", reason["tag"]) if reason["kind"] == "tag" else (
                    "dropdown", reason["definition_label"], reason["option_label"],
                ))
            members[item["type"], item["name"]] = tuple(item["setting_tags"]), tuple(reasons)
        assert page["total"] == len(members)
        result["categories"][name] = {"match_mode": category["match_mode"], "total": page["total"], "members": members}
    return result


async def _matrix(client, index):
    return {position: await _point(client, index, *position) for position in PHASES}


def _expected(phase):
    result = {"entries": {}, "categories": {}}
    for kind, (entry_type, name) in ENTRIES.items():
        base = {"成长纪要": f"{name}起始纪要\n保留原文", "阵营口令": f"{name}旧口令",
                "阵营": "旧阵营", "参照档案": ("world.place", "云港")}
        later = {"成长纪要": f"{name}二十章纪要\n新行", "阵营口令": f"{name}新口令",
                 "阵营": "新阵营", "参照档案": ("organization", "青云宗")}
        values = base if phase == "before" else later
        result["entries"][entry_type, name] = {"tags": _tags_for(kind), "fields": {
            label: (field_type, base[label], values[label]) for label, field_type in FIELD_TYPES.items()
        }}
    # 固定真值表；不是另一套事件求值器或通用分类实现。
    membership = {"before": {"all": (), "any": TAGGED}, "after": {"all": TAGGED, "any": tuple(ENTRIES)}}
    for mode, name in CATEGORIES.items():
        members = {}
        for kind in membership[phase][mode]:
            reasons = (TAG_REASON,) if phase == "before" else (
                (TAG_REASON, DROPDOWN_REASON) if kind in TAGGED else (DROPDOWN_REASON,)
            )
            members[ENTRIES[kind]] = _tags_for(kind), reasons
        result["categories"][name] = {"match_mode": mode, "total": len(members), "members": members}
    return result


def _same_timeline(source, target):
    for position in PHASES:
        assert target[position] == source[position], position


async def test_v5_roundtrip_replays_four_entities_fields_and_categories_and_isolates_projects(client):
    source_project, source = await _seed(client)
    original = await _matrix(client, source)
    for position, phase in PHASES.items():
        assert original[position] == _expected(phase), position
    backup = await _backup(client, source_project["id"])
    assert (backup["version"], backup["app_schema"]) == (6, "0037")
    assert len(backup["tables"]["setting_text_fields"]) == len(backup["tables"]["setting_progressions"]) == 16
    imported = await _import(client, backup, title="导入后的独立作品")
    target_project = imported["project"]
    target = await _index(client, target_project["id"])
    copied = await _matrix(client, target)
    _same_timeline(original, copied)

    # 归一化语义相等不能掩盖跨作品沿用旧身份或引用旧目标。
    assert source_project["id"] != target_project["id"]
    target_backup = await _backup(client, target_project["id"])
    for table in ("characters", "world_settings", "organizations", "careers", "chapters", "setting_timelines",
                  "setting_field_definitions", "setting_field_options", "setting_reference_handles",
                  "setting_text_fields",
                  "setting_progressions", "setting_categories", "setting_category_rules"):
        old, new = backup["tables"][table], target_backup["tables"][table]
        assert old and len(old) == len(new), table
        assert {row["id"] for row in old}.isdisjoint(row["id"] for row in new), table

    # 永久负控制：合法文件分别丢事件／把20章end改start，必须在指定位置被同一语义断言发现。
    for damage, changed in (("drop_events", {(20, "end"), (21, "start")}), ("move_boundary", {(20, "start")})):
        broken = deepcopy(backup)
        if damage == "drop_events":
            broken["tables"]["setting_progressions"] = []
        else:
            for item in broken["tables"]["setting_progressions"]:
                item["boundary"] = "start"
        damaged_project = (await _import(client, broken, title=f"负控制{damage}"))["project"]
        damaged = await _matrix(client, await _index(client, damaged_project["id"]))
        assert {position for position in PHASES if damaged[position] != original[position]} == changed
        with pytest.raises(AssertionError):
            _same_timeline(original, damaged)

    # 新作品不能把引用重新写回源作品；失败不得留下新绑定或改变时点值。
    current = await _fields(client, target, "character")
    response = await client.patch(
        f"{target['base']}/setting-entries/character/{target['entries']['character']['reference']['id']}"
        f"/fields/{target['definitions']['参照档案']['id']}/baseline",
        json={**_versions(current), "field_type": "reference", "value": source["entries"]["world"]["reference"]},
    )
    assert response.status_code == 404, response.text
    assert (await _fields(client, target, "character"))["timeline_revision"] == current["timeline_revision"]
    _same_timeline(copied, await _matrix(client, target))
    await _write_baseline(client, target, "character", "阵营口令", "导入侧独立起始口令")
    await _tags(client, target["base"], "character", target["entries"]["character"]["reference"]["id"],
                ["导入侧独立标签"])
    target_edited = await _matrix(client, target)
    for position, phase in PHASES.items():
        row = target_edited[position]["entries"][ENTRIES["character"]]
        assert row["tags"] == ("导入侧独立标签",)
        assert row["fields"]["阵营口令"] == (
            "line", "导入侧独立起始口令", "导入侧独立起始口令" if phase == "before" else "林岚新口令",
        )
    assert set(target_edited[20, "end"]["categories"][CATEGORIES["all"]]["members"]) == {ENTRIES["world"]}
    assert target_edited[20, "end"]["categories"][CATEGORIES["any"]]["total"] == 4
    _same_timeline(original, await _matrix(client, source))
    await _write_baseline(client, source, "world", "成长纪要", "源作品独立基础\n不会修改副本")
    source_edited = await _matrix(client, source)
    assert source_edited[5, "start"]["entries"][ENTRIES["world"]]["fields"]["成长纪要"] == (
        "text", "源作品独立基础\n不会修改副本", "源作品独立基础\n不会修改副本",
    )
    _same_timeline(target_edited, await _matrix(client, target))
