"""连载资料的单书备份读写、归属与旧格式兼容。"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import BadRequestError, ConflictError
from app.models import Project, SerializationPublication
from app.schemas.transfer import BackupOptions
from app.services.project_transfer import exporter, importer
from app.services.project_transfer.serialization import SERIALIZATION_TABLES
from app.services.serialization_snapshot import edition_fingerprint
from tests.helpers import create_project
from tests.test_project_transfer import API, _backup, _import
from tests.test_scenes import setup_scene
from tests.test_serialization_concurrency import settings_overrides as settings_overrides

A = "春风🙂\r\n第一稿"
B = "春风🙂\r\n后来定稿"


async def _seed(client):
    project, chapter, chapter_url, scene = await setup_scene(client, A)
    base = f"{API}/{project['id']}/serialization"
    url = f"{base}/chapters/{chapter['id']}"
    result = await client.put(f"{base}/goal", json={"expected_revision": 0, "daily_goal": 2})
    assert result.is_success, result.text
    result = await client.patch(url, json={"expected_revision": 0, "planned_date": "2025-10-05",
                                         "expected_scene_id": scene["id"]})
    assert result.is_success, result.text
    for number, (content, channel) in enumerate(((A, "起点"), (B, "番茄")), start=1):
        if number == 2:
            current = (await client.get(chapter_url)).json()
            result = await client.patch(chapter_url, json={"expected_content_revision": current["content_revision"],
                                                          "content": content})
            assert result.is_success, result.text
        detail = (await client.get(url)).json()
        draft, revision = detail["draft"], detail["chapter"]["revision"]
        edition = await client.post(f"{url}/editions", json={
            "expected_revision": revision, "expected_content_revision": draft["content_revision"],
            "expected_fingerprint": draft["fingerprint"], "expected_scene_id": scene["id"],
        })
        assert edition.is_success, edition.text
        result = await client.post(f"{url}/publications", json={
            "edition_id": edition.json()["id"], "expected_revision": revision + 1, "channel": channel,
            "published_at": f"2025-10-0{number}T00:05:00+08:00", "request_id": str(uuid4()),
        })
        assert result.is_success, result.text
    return project["id"], chapter_url


@pytest.mark.parametrize("detached", [False, True])
async def test_serialization_transfer_roundtrip(app, client, detached):
    pid, chapter_url = await _seed(client)
    if detached:
        result = await client.delete(chapter_url)
        assert result.is_success, result.text
    data = await _backup(client, pid, versions=0)
    assert (data["version"], data["app_schema"]) == (16, "0066")
    tables = data["tables"]
    assert "chapter_versions" not in tables
    assert tables["serialization_goals"] == [{"daily_goal": 2}]
    assert len(tables["serialization_entries"]) == 1
    assert [v["content"] for v in tables["serialization_editions"]] == [A, B]
    publications = tables["serialization_publications"]
    assert len(publications) == 2
    for row in publications:
        assert {"actor_id", "request_id", "payload_hash"}.isdisjoint(row)
        row.update(actor_id=1234, request_id=str(uuid4()), payload_hash="a" * 64)
    # 非 UTC 时间输入同样代表一个确定的瞬间，SQLite 和 PostgreSQL 都按原时间导入。
    publications[0]["published_at"] = "2025-10-01T00:05:00+08:00"
    copied = (await _import(client, data))["project"]["id"]
    recovered = (await _backup(client, copied, versions=0))["tables"]
    entry, = recovered["serialization_entries"]
    assert entry["id"] != tables["serialization_entries"][0]["id"]
    if detached:
        assert entry["chapter_id"] is None and entry["planned_date"] is None
    else:
        chapter, = recovered["chapters"]
        assert chapter["id"] != tables["chapters"][0]["id"]
        assert entry["chapter_id"] == chapter["id"] and entry["planned_date"] == "2025-10-05"
    editions = {r["id"]: r for r in recovered["serialization_editions"]}
    assert {v["entry_id"] for v in editions.values()} == {entry["id"]}
    assert [(p["channel"], editions[p["edition_id"]]["content"]) for p in recovered["serialization_publications"]] == [
        ("起点", A), ("番茄", B),
    ]
    async with app.state.sessionmaker() as db:
        restored = list(await db.scalars(select(SerializationPublication).where(
            SerializationPublication.project_id == copied,
        ).order_by(SerializationPublication.id)))
        assert all((p.actor_id, p.request_id, p.payload_hash) == (None, None, None) for p in restored)
        when = restored[0].published_at
        assert when.replace(tzinfo=UTC) == datetime(2025, 9, 30, 16, 5, tzinfo=UTC)


async def test_serialization_transfer_checks_historical_graph_and_formatting(client):
    pid, _ = await _seed(client)
    original = await _backup(client, pid)
    def parse(data):
        return importer.parse_backup(json.dumps(data, ensure_ascii=False).encode())

    def entry(tables):
        return tables["serialization_entries"][0]

    def edition(tables):
        return tables["serialization_editions"][0]

    def publication(tables):
        return tables["serialization_publications"][0]

    # 验证格式本身：同步重算指纹也不能把私有作者资料或无效位置写进读者排版。
    def replace_blocks(tables, blocks):
        row = edition(tables)
        row["blocks"] = blocks
        row["fingerprint"] = edition_fingerprint(
            row["chapter_no_at_confirmation"], row["title"], row["content"], blocks,
        )

    blocks = [{"start": 0, "end": 3, "kind": "paragraph", "level": None,
               "runs": [{"start": 0, "end": 3, "bold": True, "italic": False}]},
              {"start": 5, "end": 8, "kind": "heading", "level": 2,
               "runs": [{"start": 5, "end": 8, "bold": False, "italic": False}]}]
    good = deepcopy(original)
    replace_blocks(good["tables"], blocks)
    assert parse(good).tables["serialization_editions"][0]["blocks"] == blocks
    cases = [
        lambda t: t.pop("serialization_goals"),
        lambda t: t["serialization_goals"].append({"daily_goal": 2}),
        lambda t: entry(t).update(chapter_id=123456),
        lambda t: entry(t).update(planned_date="2025-10-35"),
        lambda t: entry(t).update(stage="unknown"),
        lambda t: entry(t).update(chapter_id=None),
        lambda t: t["serialization_entries"].append({**entry(t), "id": 9}),
        lambda t: edition(t).update(content="篡改"),
        lambda t: edition(t).update(entry_id=123456),
        lambda t: edition(t).update(seq=t["serialization_editions"][1]["seq"]),
        lambda t: publication(t).update(project_id=pid+1),
        lambda t: publication(t).update(edition_id=123456),
        lambda t: publication(t).update(channel=" "),
        lambda t: publication(t).update(published_at="0001-01-01T00:00:00+08:00"),
        lambda t: replace_blocks(t, [{**blocks[0], "sections": ["私稿"]}, blocks[1]]),
        lambda t: replace_blocks(t, [{**blocks[0], "runs": [{**blocks[0]["runs"][0], "private": "私稿"}]}, blocks[1]]),
        lambda t: replace_blocks(t, [{**blocks[0], "start": 1}, blocks[1]]),
    ]
    for mutate in cases:
        broken = deepcopy(original)
        mutate(broken["tables"])
        with pytest.raises(BadRequestError):
            parse(broken)

    # 从当前版本合成真实旧版输入，旧版也不能夹带非空新表绕过验证。
    old = deepcopy(original)
    old["version"] = 14
    with pytest.raises(BadRequestError, match="旧版备份不支持连载数据"):
        parse(old)
    for table in SERIALIZATION_TABLES:
        old["tables"].pop(table)
    migrated = (await _import(client, old))["project"]["id"]
    migrated_tables = (await _backup(client, migrated))["tables"]
    assert all(migrated_tables[name] == [] for name in SERIALIZATION_TABLES)
    assert migrated_tables["chapters"][0]["content"] == B


async def test_serialization_transfer_detects_concurrent_publication(app, client):
    pid, _ = await _seed(client)
    async with app.state.sessionmaker() as db:
        plan = await exporter.prepare_export(db, (await db.get(Project, pid)).user_id, pid, BackupOptions())
    async with app.state.sessionmaker() as db:
        previous = (await db.scalars(select(SerializationPublication))).first()
        db.add(SerializationPublication(project_id=pid, edition_id=previous.edition_id, channel="补发",
                                        published_at=previous.published_at))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError, match="连载数据已变化"):
            await exporter.validate_export(db, plan)



async def test_serialization_transfer_rejects_cross_project_edition(app, client):
    pid, _ = await _seed(client)
    other = await create_project(client, title="另一本书")
    async with app.state.sessionmaker() as db:
        publication = (await db.scalars(select(SerializationPublication).limit(1))).one()
        publication.project_id = other["id"]
        await db.commit()
    for book in (pid, other["id"]):
        response = await client.get(f"{API}/{book}/backup")
        assert response.status_code == 409
        assert "作品归属不一致" in response.json()["detail"]
