"""0076 升级、空库回退、原生整库恢复和跨作者单书导入的实际行为。"""

import asyncio
import json
import threading
import time
from contextlib import AsyncExitStack

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update

from app.core.database import create_engine
from app.models import Role, User
from app.services.backups.database import snapshot_database
from tests.helpers import create_project, register
from tests.test_annotation_native_backup import BACKEND, _clients, _ok, _postgres_pair, _restore
from tests.test_project_transfer import _backup, _import
from tests.test_serialization_concurrency import finalize_body


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_terms_native_restore_and_single_book_owner(tmp_path, dialect):
    async with AsyncExitStack() as stack:
        if dialect == "postgresql":
            source, target = await stack.enter_async_context(_postgres_pair())
        else:
            source, target = (f"sqlite+aiosqlite:///{tmp_path / name}" for name in ("source.db", "restore.db"))
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", source.replace("%", "%%"))
        cfg.attributes["configure_logger"] = False
        await asyncio.to_thread(command.upgrade, cfg, "0075")
        engine = create_engine(source)
        stack.push_async_callback(engine.dispose)
        roles = select(Role.permissions).where(Role.system_key == "super_admin")
        async with engine.begin() as db:
            old = await db.scalar(roles)
        await asyncio.to_thread(command.upgrade, cfg, "0076")
        async with engine.begin() as db:
            assert set(await db.scalar(roles)) - set(old) == {"publication_terms.read", "publication_terms.update"}
        await asyncio.to_thread(command.downgrade, cfg, "0075")
        async with engine.begin() as db:
            assert await db.scalar(roles) == old
        await asyncio.to_thread(command.upgrade, cfg, "0076")

        site_path = "/api/v1/admin/publication-terms"
        personal_path = "/api/v1/publication-terms"
        snapshot = tmp_path / "snapshot"
        async with _clients(tmp_path, source) as (admin, author, recipient):
            uid = (await register(admin, "terms-admin"))["id"]
            await register(author, "terms-writer")
            await register(recipient, "terms-recipient")
            async with engine.begin() as db:
                await db.execute(update(User).where(User.id == uid).values(is_admin=True))
            for actor, url, word in ((admin, site_path, "全站"), (author, personal_path, "原作者"),
                                     (recipient, personal_path, "新作者")):
                assert await _ok(actor.get(url)) == {"terms": [], "revision": 0}
                await _ok(actor.put(url, json={"terms": [word], "expected_revision": 0}))
            await _ok(author.put(personal_path, json={"terms": ["原作者", "私有词"], "expected_revision": 1}))

            pid = (await create_project(author))["id"]
            await _ok(author.post(f"/api/v1/projects/{pid}/chapters", json={
                "title": "交接", "content": "全站 原作者 新作者",
            }))
            book = await _backup(author, pid)
            assert "publication_terms" not in book["tables"]
            assert "私有词" not in json.dumps(book, ensure_ascii=False)
            copied = (await _import(recipient, book))["project"]["id"]
            chapter, = await _ok(recipient.get(f"/api/v1/projects/{copied}/chapters"))
            entry = f"/api/v1/projects/{copied}/serialization/chapters/{chapter['id']}"
            response = await recipient.post(f"{entry}/editions", json=finalize_body(await _ok(recipient.get(entry))))
            assert response.status_code == 409, response.text
            assert [h["term"] for h in response.json()["preflight"]["hits"]] == ["全站", "新作者"]
            await asyncio.to_thread(snapshot_database, source, snapshot, deadline=time.monotonic() + 60,
                                    stop_event=threading.Event())
            await _ok(author.put(personal_path, json={"terms": [], "expected_revision": 2}))

        with pytest.raises(RuntimeError, match="先备份"):
            await asyncio.to_thread(command.downgrade, cfg, "0075")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0076"

        await _restore(snapshot, target)
        async with _clients(tmp_path, target) as (admin, author, recipient):
            for actor, username in ((admin, "terms-admin"), (author, "terms-writer"), (recipient, "terms-recipient")):
                await _ok(actor.post("/api/v1/auth/login", json={"username": username, "password": "secret123"}))
            assert await _ok(admin.get(site_path)) == {"terms": ["全站"], "revision": 1}
            assert await _ok(author.get(personal_path)) == {"terms": ["原作者", "私有词"], "revision": 2}
            assert await _ok(recipient.get(personal_path)) == {"terms": ["新作者"], "revision": 1}
            edited = await _ok(author.put(personal_path, json={"terms": ["新词"], "expected_revision": 2}))
            assert edited == {"terms": ["新词"], "revision": 3}
