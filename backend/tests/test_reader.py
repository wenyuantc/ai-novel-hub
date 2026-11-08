"""阅读器：阅读进度写入 / 读取兜底、最近编辑章节、目录、跨用户、项目列表查询次数、备份往返。"""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, update

from app.models import Chapter, Project
from tests.helpers import create_project, register

API = "/api/v1/projects"


async def _chapter(client, pid: int, no: int, content: str = "正文内容", **kw) -> dict:
    resp = await client.post(
        f"{API}/{pid}/chapters", json={"chapter_no": no, "title": f"第{no}章", "content": content, **kw}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _get(client, pid: int) -> dict:
    resp = await client.get(f"{API}/{pid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _put(client, pid: int, chapter_id, ratio) -> int:
    resp = await client.put(f"{API}/{pid}/reading-progress", json={"chapter_id": chapter_id, "ratio": ratio})
    return resp.status_code


async def _touch(app, chapter_id: int, minutes_ago: int) -> None:
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Chapter)
            .where(Chapter.id == chapter_id)
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))
        )
        await db.commit()


async def test_progress_write_read_and_validation(client, app):
    await register(client)
    p = await create_project(client)
    pid = p["id"]
    assert p["reading"] is None and p["last_edited_chapter_id"] is None
    c1 = await _chapter(client, pid, 1)
    c2 = await _chapter(client, pid, 2)
    # 先把项目更新时间拨回过去：SQLite 的 now() 只精确到秒，同一秒内写入无法区分是否被改
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Project).where(Project.id == pid).values(updated_at=datetime.now(UTC) - timedelta(days=1))
        )
        await db.commit()
    before = (await _get(client, pid))["updated_at"]

    assert await _put(client, pid, c2["id"], 0.42) == 204
    got = await _get(client, pid)
    assert got["reading"] == {"chapter_id": c2["id"], "chapter_no": 2, "ratio": 0.42}
    # 阅读不改变项目的更新时间（书架「最近更新」）
    assert got["updated_at"] == before
    async with app.state.sessionmaker() as db:
        raw = (await db.get(Project, pid)).reading_progress
    assert raw["chapter_id"] == c2["id"] and raw["chapter_no"] == 2 and raw["ratio"] == 0.42
    assert raw["updated_at"].endswith("Z")
    # 列表与详情一致
    listed = (await client.get(API)).json()
    assert listed[0]["reading"] == got["reading"]

    # 边界值与校验
    assert await _put(client, pid, c1["id"], 0) == 204
    assert await _put(client, pid, c1["id"], 1) == 204
    assert (await _get(client, pid))["reading"]["ratio"] == 1.0
    assert await _put(client, pid, c1["id"], 1.01) == 422
    assert await _put(client, pid, c1["id"], -0.1) == 422
    assert await _put(client, pid, c1["id"], "abc") == 422
    assert await _put(client, pid, None, 0.5) == 422
    assert await _put(client, pid, 999999, 0.5) == 404
    # 其他项目的章节
    other = await create_project(client, title="另一本")
    oc = await _chapter(client, other["id"], 1)
    assert await _put(client, pid, oc["id"], 0.5) == 404
    assert (await _get(client, pid))["reading"]["chapter_id"] == c1["id"]
    assert await _put(client, 999999, c1["id"], 0.5) == 404


async def test_progress_fallback_after_delete_or_empty(client, app):
    await register(client)
    pid = (await create_project(client))["id"]
    c1 = await _chapter(client, pid, 1)
    c2 = await _chapter(client, pid, 2, content="   ")  # 空白正文 = 未写
    c3 = await _chapter(client, pid, 3)
    c4 = await _chapter(client, pid, 4)

    # 进度指向的章节被删除：退回前面最近的已写章节（跳过空章），ratio 归零
    assert await _put(client, pid, c4["id"], 0.7) == 204
    assert (await client.delete(f"{API}/{pid}/chapters/{c4['id']}")).status_code == 204
    assert (await _get(client, pid))["reading"] == {"chapter_id": c3["id"], "chapter_no": 3, "ratio": 0.0}
    assert await _put(client, pid, c3["id"], 0.3) == 204
    assert (await client.delete(f"{API}/{pid}/chapters/{c3['id']}")).status_code == 204
    assert (await _get(client, pid))["reading"] == {"chapter_id": c1["id"], "chapter_no": 1, "ratio": 0.0}

    # 指向的章节存在但正文为空
    assert await _put(client, pid, c2["id"], 0.5) == 204
    assert (await _get(client, pid))["reading"] == {"chapter_id": c1["id"], "chapter_no": 1, "ratio": 0.0}

    # 前面没有已写章节 → null
    assert await _put(client, pid, c1["id"], 0.5) == 204
    assert (await client.delete(f"{API}/{pid}/chapters/{c1['id']}")).status_code == 204
    assert (await _get(client, pid))["reading"] is None

    # 进度里的章节 id 属于别的项目（损坏数据）：不能读出别人的章节，按记录的章号退回本项目的已写章节
    c5 = await _chapter(client, pid, 5)
    other = (await create_project(client, title="另一本"))["id"]
    oc = await _chapter(client, other, 3)
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Project)
            .where(Project.id == pid)
            .values(reading_progress={"chapter_id": oc["id"], "chapter_no": 9, "ratio": 0.5})
        )
        await db.commit()
    assert (await _get(client, pid))["reading"] == {"chapter_id": c5["id"], "chapter_no": 5, "ratio": 0.0}

    # 结构不对的旧数据不报错
    async with app.state.sessionmaker() as db:
        await db.execute(update(Project).where(Project.id == pid).values(reading_progress={"chapter_id": "x"}))
        await db.commit()
    assert (await _get(client, pid))["reading"] is None


async def test_last_edited_only_written_chapters(client, app):
    await register(client)
    pid = (await create_project(client))["id"]
    c1 = await _chapter(client, pid, 1)
    c2 = await _chapter(client, pid, 2)
    empty = await _chapter(client, pid, 3, content="")
    await _touch(app, c1["id"], 30)
    await _touch(app, c2["id"], 10)
    await _touch(app, empty["id"], 1)  # 最新但正文为空，不计入
    assert (await _get(client, pid))["last_edited_chapter_id"] == c2["id"]
    await _touch(app, c1["id"], 5)
    assert (await _get(client, pid))["last_edited_chapter_id"] == c1["id"]
    listed = {p["id"]: p for p in (await client.get(API)).json()}
    assert listed[pid]["last_edited_chapter_id"] == c1["id"]
    # 只有空章节的项目
    other = (await create_project(client, title="空书"))["id"]
    await _chapter(client, other, 1, content="")
    assert (await _get(client, other))["last_edited_chapter_id"] is None


async def test_toc_only_written_with_volumes(client, app):
    await register(client)
    pid = (await create_project(client))["id"]
    resp = await client.post(
        f"{API}/{pid}/outlines", json={"volume_no": 2, "volume_title": "风起", "chapter_no": 1, "title": "大纲一"}
    )
    assert resp.status_code == 201, resp.text
    outline = resp.json()
    c2 = await _chapter(client, pid, 2, content="第二章正文")
    await _chapter(client, pid, 3, content="")
    c1 = await _chapter(client, pid, 1, content="第一章正文", outline_id=outline["id"])
    resp = await client.get(f"{API}/{pid}/reader/toc")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {
            "id": c1["id"],
            "chapter_no": 1,
            "title": "第1章",
            "word_count": 5,
            "volume_no": 2,
            "volume_title": "风起",
        },
        {
            "id": c2["id"],
            "chapter_no": 2,
            "title": "第2章",
            "word_count": 5,
            "volume_no": None,
            "volume_title": None,
        },
    ]
    assert (await client.get(f"{API}/999999/reader/toc")).status_code == 404


async def test_reader_cross_user(client, app, make_client):
    await register(client)
    pid = (await create_project(client))["id"]
    c1 = await _chapter(client, pid, 1)
    other = make_client()
    await register(other, "bob")
    assert (await other.get(f"{API}/{pid}/reader/toc")).status_code == 404
    resp = await other.put(f"{API}/{pid}/reading-progress", json={"chapter_id": c1["id"], "ratio": 0.5})
    assert resp.status_code == 404
    # bob 不能用自己的项目写别人的章节
    bob_pid = (await create_project(other))["id"]
    resp = await other.put(f"{API}/{bob_pid}/reading-progress", json={"chapter_id": c1["id"], "ratio": 0.5})
    assert resp.status_code == 404
    assert (await _get(client, pid))["reading"] is None


async def test_project_list_query_count_constant(client, app):
    """项目列表的查询次数不随项目数增长（无 N+1），包括需要阅读进度兜底的项目。"""
    await register(client)
    engine = app.state.engine.sync_engine
    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    async def _measure() -> int:
        statements.clear()
        event.listen(engine, "before_cursor_execute", _count)
        try:
            resp = await client.get(API)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert resp.status_code == 200
        return len(statements)

    async def _book(i: int) -> None:
        pid = (await create_project(client, title=f"书{i}"))["id"]
        a = await _chapter(client, pid, 1)
        b = await _chapter(client, pid, 2)
        assert await _put(client, pid, b["id"], 0.5) == 204
        if i % 2:
            # 一半的项目需要兜底
            assert (await client.delete(f"{API}/{pid}/chapters/{b['id']}")).status_code == 204
        else:
            assert a["id"]

    await _book(0)
    await _book(1)
    small = await _measure()
    for i in range(2, 8):
        await _book(i)
    large = await _measure()
    assert large == small
    listed = (await client.get(API)).json()
    assert len(listed) == 8 and all(p["reading"] is not None and p["last_edited_chapter_id"] for p in listed)


async def test_progress_survives_backup_round_trip(client, app):
    await register(client)
    pid = (await create_project(client))["id"]
    await _chapter(client, pid, 1)
    c2 = await _chapter(client, pid, 2)
    assert await _put(client, pid, c2["id"], 0.6) == 204
    # 项目更新时间拨回过去，用来确认导入时改写进度不会把它改成导入时间
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Project).where(Project.id == pid).values(updated_at=datetime.now(UTC) - timedelta(days=1))
        )
        await db.commit()
    src_updated = (await _get(client, pid))["updated_at"]
    resp = await client.get(f"{API}/{pid}/backup")
    assert resp.status_code == 200
    data = json.loads(resp.content)
    assert data["app_schema"] == "0040"
    assert data["project"]["reading_progress"]["chapter_id"] == c2["id"]

    async def _import(payload: dict) -> dict:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        resp = await client.post(f"{API}/import", files={"file": ("b.ainovel.json", raw, "application/json")})
        assert resp.status_code == 201, resp.text
        return resp.json()

    result = await _import(data)
    new = result["project"]
    chapters = (await client.get(f"{API}/{new['id']}/chapters")).json()
    new_c2 = next(c for c in chapters if c["chapter_no"] == 2)
    assert new_c2["id"] != c2["id"]
    assert new["reading"] == {"chapter_id": new_c2["id"], "chapter_no": 2, "ratio": 0.6}
    assert (await _get(client, new["id"]))["updated_at"] == src_updated
    assert new["last_edited_chapter_id"] in {c["id"] for c in chapters}
    assert not any("阅读进度" in w for w in result["warnings"])

    # 指向备份中不存在的章节：清空并给出警告
    data["project"]["reading_progress"] = {"chapter_id": 987654, "chapter_no": 9, "ratio": 0.5}
    preview = await client.post(
        f"{API}/import/preview",
        files={"file": ("b.ainovel.json", json.dumps(data).encode(), "application/json")},
    )
    assert preview.status_code == 200 and any("阅读进度" in w for w in preview.json()["warnings"])
    result = await _import(data)
    assert result["project"]["reading"] is None
    assert any("阅读进度" in w for w in result["warnings"])
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, result["project"]["id"])).reading_progress == {}
