"""相同的讨论样本经原生整库备份恢复后，历史内容、位置及备份时点权限须一致。"""

import asyncio
import hashlib
import os
import subprocess
import threading
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.main import create_app
from app.services import authorization_service
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from tests.helpers import create_project, make_settings, register

BACKEND = Path(__file__).resolve().parents[1]


def selection(scene_id, revision, content, quote):
    start = content.index(quote)
    return {
        "scene_id": scene_id, "expected_content_revision": revision,
        "start": start, "end": start + len(quote),
        "selection_hash": hashlib.sha256(quote.encode()).hexdigest(),
        "body": f"审稿意见：{quote}", "operation_id": str(uuid4()),
    }


async def _ok(request):
    response = await request
    assert response.is_success, response.text
    return response.json()


@asynccontextmanager
async def _clients(tmp_path, db_url):
    app = create_app(make_settings(tmp_path, database_url=db_url), start_task_engine=False)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(app.state.engine.dispose)
        stack.push_async_callback(app.state.system_settings.close)
        await authorization_service.initialize(app.state.sessionmaker)
        await app.state.system_settings.load()
        clients = []
        for _ in range(3):
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
            clients.append(await stack.enter_async_context(client))
        yield clients


@asynccontextmanager
async def _postgres_pair():
    # pg_dump 导出整个库。每次新建两份随机空库，与使用临时 schema 的并发测试隔开。
    raw = os.getenv("AINOVEL_POSTGRES_BACKUP_TEST_URL")
    if not raw:
        pytest.skip("未指定有 CREATEDB 权限的本机专用 AINOVEL_POSTGRES_BACKUP_TEST_URL")
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or url.database != "ainovel_test"
            or url.host not in {"127.0.0.1", "localhost", "::1"} or url.query):
        raise ValueError("整库验收须连接本机专用 ainovel_test")
    admin = await asyncpg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False))
    async with AsyncExitStack() as stack:
        stack.push_async_callback(admin.close)
        pair = []
        for _ in range(2):
            name = f"annotation_backup_{uuid4().hex}"
            await admin.execute(f'CREATE DATABASE "{name}"')
            stack.push_async_callback(admin.execute, f'DROP DATABASE "{name}"')
            pair.append(url.set(database=name).render_as_string(hide_password=False))
        yield pair


async def _history(user, path):
    threads = (await _ok(user.get(path)))["items"]
    messages = {
        row["id"]: (await _ok(user.get(f"{path}/{row['id']}/messages")))["items"]
        for row in threads
    }
    return threads, messages


@pytest.mark.parametrize("engine", ["sqlite", "postgresql"])
async def test_native_restore_with_collaborator_and_review_history(tmp_path, engine):
    async with AsyncExitStack() as stack:
        if engine == "postgresql":
            source, target = await stack.enter_async_context(_postgres_pair())
        else:
            source, target = (f"sqlite+aiosqlite:///{tmp_path / name}" for name in ("live.db", "restored.db"))
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", source.replace("%", "%%"))
        cfg.attributes["configure_logger"] = False
        await asyncio.to_thread(command.upgrade, cfg, "head")

        snapshot = tmp_path / "snapshot"
        async with _clients(tmp_path, source) as (owner, reviewer, outsider):
            await register(owner, "review-owner")
            guest = await register(reviewer, "review-guest")
            await register(outsider, "review-outsider")
            project = await create_project(owner)
            base = f"/api/v1/projects/{project['id']}"
            members = f"{base}/collaborators"
            invitation = await _ok(owner.post(members, json={"username": guest["username"], "role": "viewer"}))
            await _ok(reviewer.post(
                f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
                json={"expected_revision": invitation["revision"]},
            ))
            content = "🙂春风吹过\n溪水流淌"
            chapter = await _ok(owner.post(f"{base}/chapters", json={"title": "第一章", "content": content}))
            chapter_url = f"{base}/chapters/{chapter['id']}"
            scenes = f"{chapter_url}/scenes"
            path = f"{chapter_url}/annotations"
            page = await _ok(owner.get(scenes))
            first = page["items"][0]["id"]
            second = await _ok(owner.post(scenes, json={
                "expected_content_revision": page["content_revision"], "content": "旧桥仍在",
            }))
            revision = (await _ok(owner.get(scenes)))["content_revision"]
            ids = []
            for scene, body, quote in ((first, content, "春风"), (first, content, "溪水"),
                                       (second["id"], "旧桥仍在", "旧桥")):
                thread = await _ok(reviewer.post(path, json=selection(scene, revision, body, quote)))
                ids.append(thread["id"])
            for actor, body in ((owner, "回复一"), (reviewer, "回复二")):
                await _ok(actor.post(f"{path}/{ids[0]}/messages", json={"body": body, "operation_id": str(uuid4())}))
            await _ok(owner.post(f"{path}/{ids[1]}/resolve"))
            await _ok(owner.patch(f"{scenes}/{second['id']}", json={
                "expected_content_revision": revision, "content": "已经拆掉",
            }))
            timeline = f"{base}/story-timeline"
            saved = await _ok(owner.put(timeline + "/calendar", json={
                "expected_revision": 0, "era_name": "星历", "months": [{"id": "fog", "name": "雾月", "days": 20}],
            }))
            for kind, target_id, day in (("chapter", chapter["id"], 10), ("scene", second["id"], 3)):
                saved = await _ok(owner.put(f"{timeline}/{kind}/{target_id}/date", json={
                    "expected_revision": saved["revision"], "date": {"year": 3, "month_id": "fog", "day": day},
                }))
            dated = await _ok(reviewer.get(timeline))
            assert [item["source"] for item in dated["items"]] == ["scene", "chapter", "inherited"]
            history = await _history(owner, path)
            states = {row["id"]: (row["anchor_valid"], row["resolved"]) for row in history[0]}
            assert states == {ids[0]: (True, False), ids[1]: (True, True), ids[2]: (False, False)}
            assert [message["body"] for message in history[1][ids[0]]] == ["审稿意见：春风", "回复一", "回复二"]
            await asyncio.to_thread(snapshot_database, source, snapshot, deadline=time.monotonic() + 60,
                                    stop_event=threading.Event())
            member, = await _ok(owner.get(members))
            revoked = await owner.delete(f"{members}/{guest['id']}", params={"expected_revision": member["revision"]})
            assert revoked.status_code == 204, revoked.text
            assert (await reviewer.get(path)).status_code == 404
            await _ok(owner.post(f"{path}/{ids[0]}/messages", json={
                "body": "快照后的留言", "operation_id": str(uuid4()),
            }))

        if engine == "sqlite":
            await asyncio.to_thread(restore_sqlite_snapshot, snapshot, Path(make_url(target).database))
        else:
            url = make_url(target)
            await asyncio.to_thread(subprocess.run, [
                "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges", "--no-password",
                "--host", url.host, "--port", str(url.port or 5432), "--username", url.username,
                "--dbname", url.database, str(snapshot),
            ], env={"PATH": os.environ["PATH"], "PGPASSWORD": url.password or ""}, timeout=60, check=True)

        async with _clients(tmp_path, target) as (owner, reviewer, outsider):
            for client, name in ((owner, "review-owner"), (reviewer, "review-guest"), (outsider, "review-outsider")):
                await _ok(client.post("/api/v1/auth/login", json={"username": name, "password": "secret123"}))
            assert await _ok(reviewer.get(timeline)) == dated
            assert await _history(reviewer, path) == history
            assert (await outsider.get(path)).status_code == 404
            assert (await reviewer.post(f"{path}/{ids[2]}/resolve")).status_code == 403
            await _ok(reviewer.post(f"{path}/{ids[0]}/messages", json={
                "body": "恢复后继续审阅", "operation_id": str(uuid4()),
            }))
            resolved = await _ok(owner.post(f"{path}/{ids[2]}/resolve"))
            assert resolved["resolved"] and not resolved["anchor_valid"]
