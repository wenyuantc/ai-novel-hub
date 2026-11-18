"""通过工作台 HTTP 入口验证按章统计、冻结正文和历史文件。"""

import asyncio
import io
import threading
from datetime import UTC, date, datetime

from docx import Document
from sqlalchemy import update

from app.models import (
    Chapter,
    SerializationEdition,
    SerializationEntry,
    SerializationGoal,
    SerializationPublication,
    User,
)
from app.services.document_export import service as document_export
from app.services.serialization_snapshot import edition_fingerprint
from tests.helpers import create_project, register
from tests.test_scenes import setup_scene


async def _book(app, client):
    project, chapter, chapter_url, _ = await setup_scene(client, "当前草稿 B")
    pid, cid = project["id"], chapter["id"]
    async with app.state.sessionmaker() as db:
        db.add(SerializationGoal(project_id=pid, daily_goal=2))
        entry = SerializationEntry(project_id=pid, chapter_id=cid, stage="final", planned_date=date(2026, 11, 1))
        db.add(entry)
        await db.flush()
        text = "历史 A\n第二行🙂"
        blocks = [
            {"kind": "paragraph", "start": 0, "end": 4, "level": None,
             "runs": [{"start": 0, "end": 4, "bold": True, "italic": False}]},
            {"kind": "paragraph", "start": 5, "end": len(text), "level": None,
             "runs": [{"start": 5, "end": len(text), "bold": False, "italic": False}]},
        ]
        edition = SerializationEdition(
            entry_id=entry.id, seq=1, chapter_no=1, title="原名", content=text, blocks=blocks,
            fingerprint=edition_fingerprint(1, "原名", text, blocks),
        )
        db.add(edition)
        await db.flush()
        first = SerializationPublication(project_id=pid, edition_id=edition.id, channel="甲站",
                                         published_at=datetime(2026, 10, 31, 16, tzinfo=UTC))
        second = SerializationPublication(project_id=pid, edition_id=edition.id, channel="乙站",
                                          published_at=datetime(2026, 11, 1, 16, tzinfo=UTC))
        db.add_all([first, second])
        await db.commit()
        return f"/api/v1/projects/{pid}/serialization", chapter_url, cid, edition.id, first.id, second.id


async def test_archive_day_and_exports_survive_chapter_deletion(app, client, make_client):
    base, chapter_url, cid, edition_id, first, second = await _book(app, client)
    detail = await client.get(f"{base}/chapters/{cid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["draft_changed"]
    assert detail.json()["draft"]["content"] == "当前草稿 B"
    assert detail.json()["edition"]["id"] == edition_id
    assert detail.json()["editions"][0]["id"] == edition_id
    assert "content" not in detail.json()["editions"][0]
    other = await create_project(client, title="另一本书")
    other_base = f"/api/v1/projects/{other['id']}/serialization"
    assert (await client.get(f"{other_base}/publications/{first}")).status_code == 404
    assert (await client.get(f"{other_base}/publications/{first}/export?format=txt")).status_code == 404
    outsider = make_client()
    await register(outsider, "outsider")
    assert (await outsider.get(f"{base}/publications/{first}/export?format=md")).status_code == 404

    assert (await client.delete(chapter_url)).status_code == 204
    history = (await client.get(f"{base}/publications", params={"channel": "乙站"})).json()
    assert history["total"] == 1 and history["items"][0]["id"] == second
    assert history["items"][0]["chapter_id"] is None
    response = await client.get(f"{base}/publications/{first}")
    assert response.status_code == 200, response.text
    assert response.json()["draft"] is None and response.json()["edition"]["content"] == "历史 A\n第二行🙂"

    month = (await client.get(f"{base}/overview?month=2026-11")).json()
    assert month["days"][0] == {"date": "2026-11-01", "planned_count": 0, "first_published_count": 1}
    assert month["days"][1]["first_published_count"] == 0
    day = (await client.get(f"{base}/days/2026-11-01")).json()
    assert day["planned"]["total"] == 0
    assert day["first_published"]["items"][0]["id"] == first
    for fmt in ("txt", "md", "docx"):
        export = await client.get(f"{base}/publications/{first}/export?format={fmt}")
        assert export.status_code == 200, export.text if fmt != "docx" else export.status_code
        assert export.headers["cache-control"] == "no-store"
        if fmt == "docx":
            paragraphs = Document(io.BytesIO(export.content)).paragraphs
            assert [p.text for p in paragraphs] == ["第1章 原名", "历史 A", "第二行🙂"]
            assert paragraphs[1].runs[0].bold
        else:
            assert "第1章 原名" in export.text and "第二行🙂" in export.text and "当前草稿" not in export.text
            assert ("<strong>历史 A</strong>" if fmt == "md" else "历史 A\n第二行🙂") in export.text


async def test_backdated_earlier_month_moves_first_publication_without_changing_stock(app, client):
    base, _, cid, edition_id, _, _ = await _book(app, client)
    pid = int(base.split("/")[-2])
    async with app.state.sessionmaker() as db:
        earlier = SerializationPublication(project_id=pid, edition_id=edition_id, channel="补登",
                                           published_at=datetime(2026, 10, 31, 15, 59, tzinfo=UTC))
        db.add(earlier)
        await db.commit()
        record_id = earlier.id
    october = (await client.get(f"{base}/overview?month=2026-10")).json()
    november = (await client.get(f"{base}/overview?month=2026-11")).json()
    assert october["days"][-1]["first_published_count"] == 1
    assert november["days"][0] == {"date": "2026-11-01", "planned_count": 1, "first_published_count": 0}
    assert october["stock_count"] == november["stock_count"] == 0
    scheduled = (await client.get(f"{base}/days/2026-11-01")).json()
    assert scheduled["planned"]["items"][0]["chapter_id"] == cid and scheduled["first_published"]["total"] == 0
    day = (await client.get(f"{base}/days/2026-10-31")).json()
    assert day["first_published"]["items"][0]["id"] == record_id
    assert (await client.get(f"{base}/chapters?stock_only=true")).json()["total"] == 0
    assert (await client.get(f"{base}/chapters/{cid}?edition_id=9999999")).status_code == 404


async def test_publication_download_rechecks_authorization_after_render(app, client, monkeypatch):
    base, _, _, _, first, _ = await _book(app, client)
    entered = threading.Event()
    release = threading.Event()
    original = document_export.render_docx

    def pause(model):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("测试未能释放 Word 渲染")
        return original(model)

    monkeypatch.setattr(document_export, "render_docx", pause)
    request = asyncio.create_task(client.get(f"{base}/publications/{first}/export?format=docx"))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.username == "alice").values(token_version=User.token_version + 1))
            await db.commit()
    finally:
        release.set()
    result = await request
    assert result.status_code == 401 and "Content-Disposition" not in result.headers


async def test_generating_working_copy_does_not_block_frozen_edition_details(app, client, monkeypatch):
    base, _, cid, edition, first, _ = await _book(app, client)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == cid).values(status="generating"))
        await db.commit()

    async def forbid_draft(*args):
        raise AssertionError("生成中的草稿不能进入发布预览")

    monkeypatch.setattr("app.services.serialization_read.capture_draft", forbid_draft)
    for path in (f"/chapters/{cid}", f"/publications/{first}"):
        response = await client.get(base + path)
        assert response.status_code == 200, response.text
        assert response.json()["draft"] is None and not response.json()["draft_changed"]
        assert response.json()["edition"]["id"] == edition
    assert (await client.get(f"{base}/publications/{first}/export?format=txt")).status_code == 200


async def test_one_connection_can_read_workbench_and_render_with_pool_released(app, client, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.deps import get_db

    base, _, _, _, first, _ = await _book(app, client)
    source = app.state.sessionmaker
    engine = create_async_engine(source.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=1)
    factory = async_sessionmaker(engine, expire_on_commit=False, info=source.kw["info"])
    original_render = document_export.render_docx

    async def one_connection():
        async with factory() as session:
            yield session

    def render(model):
        assert engine.pool.checkedout() == 0, "Word 渲染期间仍持有请求或快照连接"
        return original_render(model)

    monkeypatch.setitem(app.dependency_overrides, get_db, one_connection)
    monkeypatch.setattr(document_export, "render_docx", render)
    try:
        overview = await client.get(f"{base}/overview?month=2026-11")
        assert overview.status_code == 200, overview.text
        response = await client.get(f"{base}/publications/{first}/export?format=docx")
        assert response.status_code == 200, response.text
    finally:
        await engine.dispose()
