"""大纲导入导出（AC5 / AC8）：跨项目往返（追加 / 替换）、角色按名字匹配、格式与大小校验。"""

import pytest

from tests.helpers import create_project, register


def _id_keys(obj, path="") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "id" or k.endswith("_id") or k.endswith("_ids"):
                found.append(f"{path}.{k}")
            found += _id_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _id_keys(v, f"{path}[{i}]")
    return found


async def _source(client) -> tuple[str, dict]:
    p = await create_project(client, title="甲书")
    base = f"/api/v1/projects/{p['id']}"
    lin = (await client.post(f"{base}/characters", json={"name": "林远"})).json()
    su = (await client.post(f"{base}/characters", json={"name": "苏晚"})).json()
    await client.post(f"{base}/outlines", json={"volume_no": 1, "volume_title": "启程", "chapter_no": 1, "title": "一"})
    o2 = (
        await client.post(
            f"{base}/outlines", json={"volume_no": 1, "volume_title": "启程", "chapter_no": 2, "title": "二"}
        )
    ).json()
    await client.post(f"{base}/outlines", json={"volume_no": 2, "volume_title": "深空", "chapter_no": 3, "title": "三"})
    await client.put(
        f"{base}/outlines/{o2['id']}/plans",
        json={
            "plans": [
                {"title": "二上", "key_events": ["相遇"], "character_ids": [lin["id"], su["id"]], "est_words": 4000},
                {"title": "二下", "goal": "埋伏笔", "emotion": "悬疑"},
            ]
        },
    )
    exported = (await client.get(f"{base}/outlines/export")).json()
    return base, exported


async def test_export_import_append_and_replace(client):
    await register(client)
    _, exported = await _source(client)
    assert exported["format"] == "ainovel.outline" and exported["version"] == 1
    assert [(v["volume_no"], v["volume_title"], len(v["outlines"])) for v in exported["volumes"]] == [
        (1, "启程", 2),
        (2, "深空", 1),
    ]
    plan = exported["volumes"][0]["outlines"][1]["plans"][0]
    assert plan["characters"] == ["林远", "苏晚"] and plan["est_words"] == 4000 and plan["key_events"] == ["相遇"]
    assert _id_keys(exported) == []

    target = await create_project(client, title="乙书")
    tbase = f"/api/v1/projects/{target['id']}"
    lin_b = (await client.post(f"{tbase}/characters", json={"name": "林大侠", "aliases": ["林远"]})).json()
    await client.post(f"{tbase}/outlines", json={"volume_no": 1, "chapter_no": 1, "title": "原有一"})
    await client.post(f"{tbase}/outlines", json={"volume_no": 1, "chapter_no": 2, "title": "原有二"})
    await client.post(f"{tbase}/chapters/from-outlines", json={})

    preview = (await client.post(f"{tbase}/outlines/import/preview", json={"data": exported})).json()
    assert (preview["volumes"], preview["outlines"], preview["plans"]) == (2, 3, 2)
    assert preview["unresolved_characters"] == ["苏晚"]
    assert preview["warnings"] == ["未找到名为「苏晚」的角色，已忽略"]
    assert len((await client.get(f"{tbase}/outlines")).json()) == 2  # 预览不写库

    resp = await client.post(f"{tbase}/outlines/import", json={"data": exported, "mode": "append"})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert (result["mode"], result["outlines"], result["plans"], result["snapshot_id"]) == ("append", 3, 2, None)
    assert [(o["volume_no"], o["volume_title"], o["chapter_no"], o["title"]) for o in result["items"]] == [
        (2, "启程", 3, "一"),
        (2, "启程", 4, "二"),
        (3, "深空", 5, "三"),
    ]
    tree = (await client.get(f"{tbase}/outlines/tree")).json()
    two = next(t for t in tree if t["title"] == "二")
    assert [(p["title"], p["character_ids"], p["chapter_id"]) for p in two["plans"]] == [
        ("二上", [lin_b["id"]], None),
        ("二下", [], None),
    ]
    assert two["plans"][1]["goal"] == "埋伏笔" and two["plans"][1]["emotion"] == "悬疑"

    # 整体替换：先存快照，已写章节只解除关联
    resp = await client.post(f"{tbase}/outlines/import", json={"data": exported, "mode": "replace"})
    result = resp.json()
    assert result["snapshot_id"] is not None
    assert [(o["volume_no"], o["chapter_no"], o["title"]) for o in result["items"]] == [
        (1, 1, "一"),
        (1, 2, "二"),
        (2, 3, "三"),
    ]
    snaps = (await client.get(f"{tbase}/outline-snapshots")).json()
    assert snaps[0]["id"] == result["snapshot_id"] and snaps[0]["reason"] == "import_replace"
    assert snaps[0]["outline_count"] == 5
    chapters = (await client.get(f"{tbase}/chapters")).json()
    assert len(chapters) == 2 and all(c["outline_id"] is None for c in chapters)
    assert len((await client.get(f"{tbase}/outlines")).json()) == 3


def _file(**kw):
    data = {
        "format": "ainovel.outline",
        "version": 1,
        "volumes": [{"volume_title": "卷", "outlines": [{"title": "一"}]}],
    }
    data.update(kw)
    return data


@pytest.mark.parametrize(
    ("data", "detail"),
    [
        ([1, 2], "大纲文件格式不正确：应为 JSON 对象"),
        (_file(format="ainovel.cards"), "不是有效的大纲文件（format 应为 ainovel.outline）"),
        (_file(version=2), "不支持的大纲文件版本：2（当前支持 1）"),
        (_file(volumes=[{"outlines": [{"title": ""}]}]), "大纲文件格式不正确：volumes.0.outlines.0.title"),
        (_file(volumes=[{"outlines": []}]), "大纲文件中没有任何章纲"),
        (
            _file(volumes=[{"outlines": [{"title": "x" * 10, "summary": "长" * (1024 * 1024)}]}]),
            "大纲文件过大，上限为 2MB",
        ),
    ],
)
async def test_import_rejects_bad_files(client, data, detail):
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    for url in (f"{base}/outlines/import/preview", f"{base}/outlines/import"):
        resp = await client.post(url, json={"data": data})
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"].startswith(detail)
    assert (await client.get(f"{base}/outlines")).json() == []
