"""规划领域 API：真实读写、批量 CAS、关联与显式交汇。"""

import asyncio
import json

from sqlalchemy import delete, func, select
from sqlalchemy import text as sql_text

from app.models import PlanningUnit, Project, SubplotCrossingParticipant
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def setup(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "初遇", "chapter_no": 1})).json()
    character = (await client.post(f"{base}/characters", json={"name": "林远"})).json()
    return base, chapter, character


async def matrix(client, base):
    response = await client.get(f"{base}/planning/matrix")
    assert response.status_code == 200, response.text
    return response.json()


async def test_tag_search_filters_before_pagination(client):
    base, _, _ = await setup(client)
    revision = (await matrix(client, base))["revision"]
    for name in ("铺垫", "推进目标", "推进待核对"):
        result = await client.post(f"{base}/planning/tags", json={"expected_revision": revision, "name": name})
        assert result.status_code == 201, result.text
        revision = result.json()["revision"]
    result = await client.get(f"{base}/planning/tags", params={"q": " 推进 ", "page": 2, "page_size": 1})
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 2
    assert [item["name"] for item in result.json()["items"]] == ["推进待核对"]
    empty = await client.get(f"{base}/planning/tags", params={"q": "无匹配标签"})
    assert empty.json()["total"] == 0 and empty.json()["items"] == []


async def test_detached_search_uses_preserved_source_label(client):
    base, chapter, character = await setup(client)
    result = await client.patch(f"{base}/planning/units/batch", json={
        "expected_revision": (await matrix(client, base))["revision"],
        "targets": [{"kind": "chapter", "id": chapter["id"]}],
        "pov": {"op": "set", "character_id": character["id"]},
    })
    assert result.status_code == 200, result.text
    assert (await client.delete(f"{base}/chapters/{chapter['id']}")).status_code == 204
    found = await client.get(f"{base}/planning/detached", params={"q": "初遇"})
    assert found.status_code == 200 and found.json()["total"] == 1
    empty = await client.get(f"{base}/planning/detached", params={"q": "无匹配标题"})
    assert empty.status_code == 200 and empty.json()["total"] == 0


async def test_matrix_read_is_virtual_and_batch_is_atomic(client, app):
    base, chapter, character = await setup(client)
    first = await matrix(client, base)
    assert first["total"] == 1 and first["rows"][0]["unit_id"] is None
    assert first["rows"][0]["pov_character_name"] is None
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(PlanningUnit)) == 0
    body = {
        "expected_revision": first["revision"],
        "targets": [{"kind": "chapter", "id": chapter["id"]}],
        "pov": {"op": "set", "character_id": character["id"]},
        "references": [{"kind": "character", "op": "add", "ids": [character["id"]]}],
    }
    saved = await client.patch(f"{base}/planning/units/batch", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["rows"][0]["pov_character_id"] == character["id"]
    assert saved.json()["rows"][0]["pov_character_name"] == "林远"
    assert saved.json()["revision"] == first["revision"] + 1
    body["pov"] = {"op": "clear"}
    stale = await client.patch(f"{base}/planning/units/batch", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_planning_revision"
    assert (await matrix(client, base))["rows"][0]["pov_character_id"] == character["id"]


async def test_crossing_without_nodes_is_progress_and_delete_is_guarded(client):
    base, chapter, _ = await setup(client)
    revision = (await matrix(client, base))["revision"]
    subplots = []
    for name in ("寻亲", "夺位", "复仇"):
        response = await client.post(f"{base}/planning/subplots", json={"expected_revision": revision, "name": name})
        assert response.status_code == 201, response.text
        revision = response.json()["revision"]
        subplots.append(response.json()["item"])
    created = await client.post(
        f"{base}/planning/crossings",
        json={
            "expected_revision": revision,
            "source": {"kind": "chapter", "id": chapter["id"]},
            "title": "殿前相认",
            "description": "三条线索在朝会上相交",
            "subplot_ids": [s["id"] for s in subplots],
        },
    )
    assert created.status_code == 201, created.text
    crossing = created.json()["item"]
    revision = created.json()["revision"]
    weave = (await client.get(f"{base}/planning/weave", params={"subplot_ids": subplots[0]["id"]})).json()
    assert len(weave["tracks"]) == 1 and weave["tracks"][0]["progress_keys"]
    assert weave["crossings"][0]["subplot_ids"] == [s["id"] for s in subplots]
    assert not weave["tracks"][0]["nodes"]
    denied = await client.delete(
        f"{base}/planning/subplots/{subplots[0]['id']}", params={"expected_revision": revision}
    )
    assert denied.status_code == 409
    response = await client.delete(
        f"{base}/planning/crossings/{crossing['id']}", params={"expected_revision": revision}
    )
    assert response.status_code == 200, response.text
    assert (await client.get(f"{base}/planning/subplots")).json()["total"] == 3


async def test_concurrent_batch_only_one_wins(client):
    base, chapter, character = await setup(client)
    revision = (await matrix(client, base))["revision"]
    body = {
        "expected_revision": revision,
        "targets": [{"kind": "chapter", "id": chapter["id"]}],
        "pov": {"op": "set", "character_id": character["id"]},
    }
    responses = await asyncio.gather(*(client.patch(f"{base}/planning/units/batch", json=body) for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]


async def test_invalid_batch_does_not_materialize_units(client, app):
    base, chapter, character = await setup(client)
    second = (await client.post(f"{base}/chapters", json={"title": "第二章", "chapter_no": 2})).json()
    revision = (await matrix(client, base))["revision"]
    failed = await client.patch(
        f"{base}/planning/units/batch",
        json={
            "expected_revision": revision,
            "targets": [{"kind": "chapter", "id": chapter["id"]}, {"kind": "chapter", "id": second["id"]}],
            "pov": {"op": "set", "character_id": character["id"]},
            "references": [{"kind": "world", "op": "add", "ids": [987654]}],
        },
    )
    assert failed.status_code == 400, failed.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(PlanningUnit)) == 0
    assert (await matrix(client, base))["revision"] == revision


async def test_all_reference_kinds_and_per_kind_replacement(client):
    base, chapter, character = await setup(client)
    other = (await client.post(f"{base}/characters", json={"name": "明月"})).json()
    world = (await client.post(f"{base}/world", json={"title": "宗门戒律", "category": "rule"})).json()
    org = (await client.post(f"{base}/organizations", json={"name": "青云宗"})).json()
    career = (await client.post(f"{base}/careers", json={"name": "剑修", "stages": [{"name": "入门"}]})).json()
    types = (await client.get(f"{base}/relationship-types")).json()
    relation = await client.post(
        f"{base}/relationships",
        json={
            "a_id": character["id"],
            "b_id": other["id"],
            "type_id": types[0]["id"],
        },
    )
    assert relation.status_code == 201, relation.text
    catalog = (await client.get(f"{base}/planning/entries", params={"kind": "relationship"})).json()
    relationship = catalog["items"][0]["reference"]
    operations = [
        {"kind": "character", "op": "add", "ids": [character["id"]]},
        {"kind": "world", "op": "add", "ids": [world["id"]]},
        {"kind": "organization", "op": "add", "ids": [org["id"]]},
        {"kind": "career", "op": "add", "ids": [career["id"]]},
        {"kind": "relationship", "op": "add", "timeline_ids": [relationship["timeline_id"]]},
    ]
    revision = (await matrix(client, base))["revision"]
    response = await client.patch(
        f"{base}/planning/units/batch",
        json={
            "expected_revision": revision,
            "targets": [{"kind": "chapter", "id": chapter["id"]}],
            "references": operations,
        },
    )
    assert response.status_code == 200, response.text
    assert {ref["kind"] for ref in response.json()["rows"][0]["references"]} == {
        "character",
        "world",
        "organization",
        "career",
        "relationship",
    }
    replaced = await client.patch(
        f"{base}/planning/units/batch",
        json={
            "expected_revision": response.json()["revision"],
            "targets": [{"kind": "chapter", "id": chapter["id"]}],
            "references": [{"kind": "character", "op": "replace", "ids": [other["id"]]}],
        },
    )
    assert replaced.status_code == 200, replaced.text
    refs = replaced.json()["rows"][0]["references"]
    assert {ref["id"] for ref in refs if ref["kind"] == "character"} == {other["id"]}
    assert len(refs) == 5
    columns = [{"kind": "setting", "reference": ref} for ref in refs]
    view = (await client.get(f"{base}/planning/matrix", params={"column_refs": json.dumps(columns)})).json()
    assert all(cell["manual"] for cell in view["cells"])
    assert next(column for column in view["columns"] if column["name"] == "青云宗")["tracking_supported"]


async def test_matrix_distinguishes_ambiguity_unwritten_and_manual(client):
    base, chapter, character = await setup(client)
    world = (await client.post(f"{base}/world", json={"title": "林远", "category": "place"})).json()
    saved = await client.patch(f"{base}/chapters/{chapter['id']}", json={"content": "林远来到了林远。"})
    assert saved.status_code == 200
    columns = [
        {"kind": "setting", "reference": {"kind": "character", "id": character["id"]}},
        {"kind": "setting", "reference": {"kind": "world", "id": world["id"]}},
    ]
    view = (await client.get(f"{base}/planning/matrix", params={"column_refs": json.dumps(columns)})).json()
    assert all(
        cell["ambiguous_count"] == 2 and cell["mention_count"] == 0 and not cell["manual"] for cell in view["cells"]
    )
    assert all(cell["has_content"] for cell in view["cells"])
    await client.patch(f"{base}/chapters/{chapter['id']}", json={"content": ""})
    empty = (await client.get(f"{base}/planning/matrix", params={"column_refs": json.dumps(columns)})).json()
    assert all(not cell["has_content"] and cell["ambiguous_count"] == 0 for cell in empty["cells"])


async def test_matrix_shares_candidate_tracking_and_exclusion_owners(client):
    base, chapter, character = await setup(client)
    changed = await client.patch(f"{base}/characters/{character['id']}", json={"aliases": ["RIVER"]})
    assert changed.status_code == 200, changed.text
    organization = (await client.post(f"{base}/organizations", json={"name": "River"})).json()
    career = (await client.post(f"{base}/careers", json={"name": "巡港人", "stages": [{"name": "初阶"}]})).json()
    world = (await client.post(f"{base}/world", json={"title": "钟律", "category": "rule"})).json()
    updated = await client.patch(f"{base}/chapters/{chapter['id']}", json={
        "content": "🙂 river crossing, river；巡港人、钟律。",
    })
    assert updated.status_code == 200, updated.text

    async def policy(kind, item, **patch):
        url = f"{base}/setting-entries/{kind}/{item['id']}/ai-settings"
        old = (await client.get(url)).json()
        response = await client.patch(url, json={
            **patch, "expected_ai_visibility_revision": old["ai_visibility_revision"],
        })
        assert response.status_code == 200, response.text

    async def counts():
        return {cell["column_key"]: (cell["tracking_supported"], cell["mention_count"], cell["ambiguous_count"])
                for cell in (await matrix(client, base))["cells"]}

    await policy("character", character, mention_case_sensitive=False, mention_exclusions=["river crossing"])
    await policy("organization", organization, mention_case_sensitive=False)
    expected = {
        f"character:{character['id']}": (True, 0, 1),
        f"organization:{organization['id']}": (True, 1, 1),
        f"career:{career['id']}": (True, 1, 0),
        f"world:{world['id']}": (True, 1, 0),
    }
    assert await counts() == expected
    await policy("character", character, mention_enabled=False)
    expected[f"character:{character['id']}"] = (False, 0, 0)
    expected[f"organization:{organization['id']}"] = (True, 2, 0)
    assert await counts() == expected


async def test_merge_requires_every_node_resolution_and_retires_other(client):
    base, chapter, character = await setup(client)
    second = (await client.post(f"{base}/chapters", json={"title": "另一场", "chapter_no": 2})).json()
    revision = (await matrix(client, base))["revision"]
    subplot = (
        await client.post(f"{base}/planning/subplots", json={"expected_revision": revision, "name": "寻亲"})
    ).json()
    revision = subplot["revision"]
    nodes = []
    for ch, text in ((chapter, "发现玉佩"), (second, "认出刻字")):
        result = await client.put(
            f"{base}/planning/subplots/{subplot['item']['id']}/nodes/chapter/{ch['id']}",
            json={"expected_revision": revision, "content": text},
        )
        assert result.status_code == 200, result.text
        revision = result.json()["revision"]
        nodes.append(result.json()["item"])
    body = {
        "expected_revision": revision,
        "survivor_unit_id": nodes[0]["unit_id"],
        "other_unit_id": nodes[1]["unit_id"],
        "pov_character_id": character["id"],
    }
    denied = await client.post(f"{base}/planning/units/merge", json=body)
    assert denied.status_code == 409 and len(denied.json()["node_conflicts"]) == 1
    assert len(denied.json()["units"]) == 2
    body["node_resolutions"] = [
        {"subplot_id": subplot["item"]["id"], "keep_node_id": nodes[1]["id"], "content": "发现玉佩并认出刻字"}
    ]
    merged = await client.post(f"{base}/planning/units/merge", json=body)
    assert merged.status_code == 200, merged.text
    weave = (await client.get(f"{base}/planning/weave")).json()
    assert weave["tracks"][0]["nodes"] == [
        {**nodes[1], "unit_id": nodes[0]["unit_id"], "content": "发现玉佩并认出刻字"}
    ]
    forbidden = await client.post(
        f"{base}/planning/units/{nodes[1]['unit_id']}/binding",
        json={
            "expected_revision": merged.json()["revision"],
            "target": {"kind": "chapter", "id": second["id"]},
        },
    )
    assert forbidden.status_code == 404


async def test_gap_counts_full_axis_and_subplot_cells_show_crossings(client):
    base, first, _ = await setup(client)
    chapters = [first]
    for number in (2, 6, 9, 12):
        chapters.append(
            (await client.post(f"{base}/chapters", json={"chapter_no": number, "title": str(number)})).json()
        )
    revision = (await matrix(client, base))["revision"]
    a = (await client.post(f"{base}/planning/subplots", json={"expected_revision": revision, "name": "主线"})).json()
    b = (
        await client.post(f"{base}/planning/subplots", json={"expected_revision": a["revision"], "name": "副线"})
    ).json()
    node = (
        await client.put(
            f"{base}/planning/subplots/{a['item']['id']}/nodes/chapter/{first['id']}",
            json={"expected_revision": b["revision"], "content": "开始"},
        )
    ).json()
    event = await client.post(
        f"{base}/planning/crossings",
        json={
            "expected_revision": node["revision"],
            "source": {"kind": "chapter", "id": chapters[3]["id"]},
            "title": "交汇",
            "description": "相遇",
            "subplot_ids": [a["item"]["id"], b["item"]["id"]],
        },
    )
    assert event.status_code == 201, event.text
    weave = (await client.get(f"{base}/planning/weave", params={"page_size": 1, "gap_threshold": 2})).json()
    gaps = weave["tracks"][0]["gaps"]
    assert [gap["gap_units"] for gap in gaps] == [2, 1]
    assert gaps[0]["exceeds_threshold"] and not gaps[1]["exceeds_threshold"]
    assert gaps[0]["after_label"] == "第 1 章 · 初遇"
    assert gaps[0]["before_label"] == "第 9 章 · 9"
    assert gaps[1]["after_label"] == "第 9 章 · 9" and gaps[1]["before_label"] is None
    assert len(weave["rows"]) == 1  # 起止标签仍来自完整轴，未把窗口外的第9章误当行序号。
    selected = await client.get(
        f"{base}/planning/matrix",
        params={
            "column_mode": "subplots",
            "source_refs": json.dumps([{"kind": "chapter", "id": chapters[3]["id"]}]),
        },
    )
    assert selected.status_code == 200, selected.text
    assert all(
        cell["node"] is None and cell["has_progress"] and len(cell["crossings"]) == 1
        for cell in selected.json()["cells"]
    )


async def test_planning_permissions_and_query_validation(client, system_admin, make_client):
    base, chapter, _ = await setup(client)
    stranger = make_client()
    person = await register(stranger, "reader")
    assert (await stranger.get(f"{base}/planning/matrix")).status_code == 404
    role = await create_role(system_admin, "规划只读", ["projects.read"])
    await assign_roles(system_admin, person["id"], [role["id"]])
    revision = (await matrix(stranger, base))["revision"]
    denied = await stranger.post(f"{base}/planning/tags", json={"expected_revision": revision, "name": "不能写"})
    assert denied.status_code == 403
    editable = await create_role(system_admin, "规划编辑", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, person["id"], [editable["id"]])
    tag = await stranger.post(f"{base}/planning/tags", json={"expected_revision": revision, "name": "检查"})
    assert tag.status_code == 201, tag.text
    assert (await client.get(f"{base}/planning/matrix", params={"column_refs": "broken"})).status_code == 422
    assert (await client.get(f"{base}/planning/matrix", params={"page_size": 101})).status_code == 422
    bad = await client.patch(
        f"{base}/planning/units/batch",
        json={
            "expected_revision": tag.json()["revision"],
            "targets": [{"kind": "chapter", "id": chapter["id"]}],
            "references": [{"kind": "relationship", "op": "add", "ids": [1]}],
        },
    )
    assert bad.status_code == 422
    assert (
        await client.delete(
            f"{base}/planning/subplots/1/nodes/chapter/-1", params={"expected_revision": tag.json()["revision"]}
        )
    ).status_code == 422


async def test_detached_rebinding_preserves_contents_and_identity(client):
    base, chapter, character = await setup(client)
    saved = await client.patch(
        f"{base}/planning/units/batch",
        json={
            "expected_revision": (await matrix(client, base))["revision"],
            "targets": [{"kind": "chapter", "id": chapter["id"]}],
            "pov": {"op": "set", "character_id": character["id"]},
            "references": [{"kind": "character", "op": "add", "ids": [character["id"]]}],
        },
    )
    assert saved.status_code == 200, saved.text
    unit_id = saved.json()["rows"][0]["unit_id"]
    assert (await client.delete(f"{base}/chapters/{chapter['id']}")).status_code == 204
    replacement = (await client.post(f"{base}/chapters", json={"title": "新章节", "chapter_no": 1})).json()
    row = (await matrix(client, base))["rows"][0]
    assert row["unit_id"] is None and row["pov_character_id"] is None
    detached = (await client.get(f"{base}/planning/detached")).json()
    assert detached["items"][0]["unit_id"] == unit_id
    rebound = await client.post(
        f"{base}/planning/units/{unit_id}/binding",
        json={
            "expected_revision": detached["revision"],
            "target": {"kind": "chapter", "id": replacement["id"]},
        },
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["rows"][0]["unit_id"] == unit_id
    assert rebound.json()["rows"][0]["pov_character_id"] == character["id"]
    assert rebound.json()["rows"][0]["pov_character_name"] == "林远"
    assert (await client.get(f"{base}/planning/detached")).json()["total"] == 0


async def test_crossing_validation_rolls_back_and_project_cascades(client, app):
    base, chapter, _ = await setup(client)
    revision = (await matrix(client, base))["revision"]
    a = (await client.post(f"{base}/planning/subplots", json={"expected_revision": revision, "name": "A"})).json()
    b = (await client.post(f"{base}/planning/subplots", json={"expected_revision": a["revision"], "name": "B"})).json()
    body = {
        "expected_revision": b["revision"],
        "source": {"kind": "chapter", "id": chapter["id"]},
        "title": "相遇",
        "description": "共同目标",
        "subplot_ids": [a["item"]["id"], b["item"]["id"]],
    }
    for ids in ([a["item"]["id"]], [a["item"]["id"], a["item"]["id"]]):
        assert (await client.post(f"{base}/planning/crossings", json={**body, "subplot_ids": ids})).status_code == 422
    invalid = await client.post(f"{base}/planning/crossings", json={**body, "subplot_ids": [a["item"]["id"], 998877]})
    assert invalid.status_code == 400
    assert (await matrix(client, base))["revision"] == b["revision"]
    event = await client.post(f"{base}/planning/crossings", json=body)
    assert event.status_code == 201, event.text
    # SQLite 与 PostgreSQL 多级级联的检查时机不同，同时锁定延迟约束契约。
    foreign_key = next(iter(SubplotCrossingParticipant.__table__.c.subplot_id.foreign_keys))
    assert foreign_key.deferrable and foreign_key.initially == "DEFERRED"
    async with app.state.sessionmaker() as db:
        await db.execute(delete(Project).where(Project.id == int(base.rsplit("/", 1)[1])))
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(PlanningUnit)) == 0
        assert await db.scalar(select(func.count()).select_from(SubplotCrossingParticipant)) == 0
        assert not (await db.execute(sql_text("PRAGMA foreign_key_check"))).all()


async def test_tags_crud_and_clear_do_not_touch_other_fields(client):
    base, chapter, character = await setup(client)
    revision = (await matrix(client, base))["revision"]
    tag = await client.post(f"{base}/planning/tags", json={"expected_revision": revision, "name": "  复查  "})
    assert tag.status_code == 201 and tag.json()["item"]["name"] == "复查"
    data = tag.json()
    duplicate = await client.post(f"{base}/planning/tags", json={"expected_revision": data["revision"], "name": "复查"})
    assert duplicate.status_code == 409
    saved = await client.patch(
        f"{base}/planning/units/batch",
        json={
            "expected_revision": data["revision"],
            "targets": [{"kind": "chapter", "id": chapter["id"]}],
            "tags": {"op": "add", "ids": [data["item"]["id"]]},
            "pov": {"op": "set", "character_id": character["id"]},
        },
    )
    assert saved.status_code == 200, saved.text
    assert (await client.get(f"{base}/planning/tags")).json()["items"][0]["usage_count"] == 1
    cleared = await client.patch(
        f"{base}/planning/units/batch",
        json={
            "expected_revision": saved.json()["revision"],
            "targets": [{"kind": "chapter", "id": chapter["id"]}],
            "tags": {"op": "clear"},
        },
    )
    assert cleared.status_code == 200 and not cleared.json()["rows"][0]["tags"]
    assert cleared.json()["rows"][0]["pov_character_id"] == character["id"]
