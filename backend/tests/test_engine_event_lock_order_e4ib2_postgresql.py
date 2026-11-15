"""E4i-b2（裁决 18）：引擎事件 / 终态提交锁序在真实 PostgreSQL 上不与作者发送 lease 交叉等待。

旧顺序：引擎 `_event_task` / `complete_state` 先锁 actor User 并取发送写门禁，之后协作 handler 的
`guard.require_task_write` 才锁作者 User；作者发送 lease 先 FOR SHARE 作者 User 再轮询共享门禁 → 交叉等待，
作者在门禁轮询超时（约 2 秒）后以 ai_visibility_busy 失败。
新顺序：作者 User 与 actor 一起在门禁之前按 ID 升序锁定，作者 lease 只在作者 User 行上排队，引擎事务提交后照常完成。

代表 handler：大纲续写（outline.continue）与实体生成（character.generate，复核补充）。
引擎在第二个实例运行，作者 lease 经第一个实例；编排只用 Event 与
pg_blocking_pids 观测（无固定等待），双方都必须成功。
"""

import asyncio

import pytest
from sqlalchemy import text

from app.services import ai_dispatch
from app.services import authorization_service as authz
from app.services.task_engine.registry import get_handler
from tests import test_collaboration_entity_entry_e4g3b as entity_case
from tests.conftest import _new_client
from tests.test_collaboration_outline_entry_e4g3 import (
    CONTINUE_JSON,
    EDITOR_KEY,
    _continue,
    _terminal,
    _world,
)
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_e4ib2_apps = _postgres_fixture


async def _blocked_by(maker, waiter_pid: int, holder_pid: int) -> bool:
    async with maker() as db:
        return bool(await db.scalar(
            text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"), {"holder": holder_pid, "waiter": waiter_pid},
        ))


@pytest.mark.parametrize("kind", ["outline.continue", "character.generate"])
@pytest.mark.parametrize("stage", ["event", "final"])
async def test_pg_engine_commit_does_not_cross_wait_with_author_send_lease(
    postgres_e4ib2_apps, fake_llm, monkeypatch, stage, kind,
):
    first_app, second_app = postgres_e4ib2_apps
    for app in (first_app, second_app):
        app.state.settings.allow_private_ai_endpoints = True
        app.state.settings.auto_analyze_enabled = False
    handler_cls = type(get_handler(kind))
    name = "verify_event" if stage == "event" else "verify_finalization"
    original = getattr(handler_cls, name)
    pids: dict[str, int] = {}
    engine_locked, author_started, author_user_locked, proceed = (asyncio.Event() for _ in range(4))

    async def hold_before_handler(self, db, task, **kwargs):
        # 引擎事务已取认证 / User / 发送写门禁 / 作品 / Task；handler 尚未再锁作者 User。
        if not engine_locked.is_set():
            pids["engine"] = await db.scalar(text("SELECT pg_backend_pid()"))
            engine_locked.set()
            await proceed.wait()
        return await original(self, db, task, **kwargs)

    monkeypatch.setattr(handler_cls, name, hold_before_handler)
    boxes: dict[str, object] = {}

    async def author_send_lease():
        # 作者真实发送 lease 的同一顺序：作者 User FOR SHARE → 共享发送门禁。
        await engine_locked.wait()
        async with first_app.state.sessionmaker() as db:
            pids["author"] = await db.scalar(text("SELECT pg_backend_pid()"))
            author_started.set()
            await authz.require_authenticated_for_send(db, boxes["owner_id"])
            author_user_locked.set()
            await ai_dispatch._lock(db, boxes["project_id"], write=False)  # noqa: SLF001
            await db.commit()

    engine = second_app.state.task_engine
    author = None
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await (_world if kind == "outline.continue" else entity_case._world)(owner, guest)
            # 作者先注册：作者 ID 更小，旧顺序下“actor 先锁、作者后锁”恰为非升序。
            assert world.owner["id"] < world.guest["id"]
            boxes.update(owner_id=world.owner["id"], project_id=world.project["id"])
            if kind == "outline.continue":
                fake_llm.push(CONTINUE_JSON)
                created = await _continue(guest, world)
            else:
                fake_llm.push(entity_case._answer(kind))
                created = await entity_case._start(guest, world, kind, instruction="天剑宗师承")
            assert created.status_code == 202, created.text
            author = asyncio.create_task(author_send_lease())
            await engine.start()
            await asyncio.wait_for(author_started.wait(), 15)

            async def author_waiting_on_engine():
                # 新顺序：作者在作者 User 行上被引擎事务阻塞；旧顺序：作者已取得 User 共享锁、转而轮询门禁。
                while not author_user_locked.is_set() and not author.done():
                    if await _blocked_by(first_app.state.sessionmaker, pids["author"], pids["engine"]):
                        return True
                return False

            blocked_on_user = await asyncio.wait_for(author_waiting_on_engine(), 15)
            proceed.set()
            results = await asyncio.wait_for(asyncio.gather(author, return_exceptions=True), 30)
            # 旧顺序下作者 lease 会在门禁轮询超时后以 ai_visibility_busy 失败（引擎事务在作者 User 行上等待）。
            assert results == [None], results
            assert blocked_on_user
            row = await _terminal(first_app, created.json()["id"])
            assert row.status == "succeeded", row.error
            assert row.user_id == world.guest["id"]
            if kind == "outline.continue":
                assert [d["title"] for d in row.result["drafts"]] == ["续章甲"]
                assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY]
            else:
                assert row.input["_project_model"]["source"] == "actor"
                assert [call["api_key"] for call in fake_llm.calls] == [entity_case.EDITOR_KEY]
    finally:
        proceed.set()
        if author is not None and not author.done():
            author.cancel()
        if author is not None:
            await asyncio.gather(author, return_exceptions=True)
        await engine.stop()
