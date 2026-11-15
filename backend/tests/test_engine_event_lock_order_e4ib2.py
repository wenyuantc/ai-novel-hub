"""E4i-b2（裁决 18）：引擎事件 / 终态提交的锁序（SQLite 顺序断言；交叉等待另见 PG 用例）。

协作 handler 的 verify_event / verify_finalization 会在引擎已取发送写门禁后再锁作者 User
（guard.require_task_write 等）。修复后引擎在取门禁之前就与 actor 一起按 ID 升序锁作者 User，
handler 后续的作者 User 锁只是重入；作者自己的任务行为不变（只锁本人）。
"""

import weakref

import pytest

from app.services import ai_dispatch
from app.services import authorization_service as authz
from app.services.task_engine import engine as engine_module
from app.services.task_engine.execution_identity import TaskExecutionIdentity, frozen_project_owner_id
from app.services.task_engine.registry import get_handler
from tests.test_collaboration_outline_entry_e4g3 import (
    CONTINUE_JSON,
    _continue,
    _terminal,
    _world,
)


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def _record_locks(monkeypatch):
    """按 session 记录 User 锁与发送写门禁；在 handler 事件/终态核验处切出引擎事务的前后两段。"""
    order: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
    segments: list[tuple[str, list, list]] = []
    original_users, original_gate = authz.lock_users, ai_dispatch.lock_visibility_write

    async def lock_users(db, *ids, **kwargs):
        order.setdefault(db, []).append(("users", tuple(sorted(set(ids)))))
        return await original_users(db, *ids, **kwargs)

    async def lock_visibility_write(db, project_id, **kwargs):
        order.setdefault(db, []).append(("send_gate", (project_id,)))
        return await original_gate(db, project_id, **kwargs)

    monkeypatch.setattr(authz, "lock_users", lock_users)
    monkeypatch.setattr(ai_dispatch, "lock_visibility_write", lock_visibility_write)
    # 引擎模块级导入了门禁函数，须一并替换才能观测单章 / 终态分支。
    monkeypatch.setattr(engine_module, "lock_visibility_write", lock_visibility_write)

    handler_cls = type(get_handler("outline.continue"))
    original_event, original_final = handler_cls.verify_event, handler_cls.verify_finalization

    def wrap(name, original):
        async def hook(self, db, task, **kwargs):
            # 进入 handler 时引擎已取门禁；之前的锁即引擎所取，之后的是 handler 自己的。
            before = list(order.setdefault(db, []))
            try:
                return await original(self, db, task, **kwargs)
            finally:
                segments.append((name, before, order[db][len(before):]))
                order[db] = []
        return hook

    monkeypatch.setattr(handler_cls, "verify_event", wrap("event", original_event))
    monkeypatch.setattr(handler_cls, "verify_finalization", wrap("final", original_final))
    return segments


@pytest.mark.parametrize("actor", ["editor", "author"])
async def test_engine_event_and_terminal_lock_users_before_send_write_gate(
    app, client, make_client, fake_llm, monkeypatch, actor,
):
    guest = make_client()
    world = await _world(client, guest)
    assert world.owner["id"] < world.guest["id"]
    segments = _record_locks(monkeypatch)
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(guest if actor == "editor" else client, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert {name for name, _before, _after in segments} == {"event", "final"}, segments
    project_gate = ("send_gate", (world.project["id"],))
    owner_id, guest_id = world.owner["id"], world.guest["id"]
    for name, before, after in segments:
        gate_at = max(i for i, item in enumerate(before) if item == project_gate)
        users_before_gate = [ids for kind, ids in before[:gate_at] if kind == "users"]
        assert users_before_gate, (name, before)
        later = {user for kind, ids in after if kind == "users" for user in ids}
        if actor == "editor":
            # 作者 User 与 actor 在门禁之前按升序一起锁定；handler 之后的作者 User 锁只是重入。
            assert users_before_gate[-1] == (owner_id, guest_id), (name, before)
            assert later <= {owner_id, guest_id}, (name, after)
        else:
            # 作者任务不变：门禁前只锁本人。
            assert users_before_gate[-1] == (owner_id,), (name, before)
            assert later <= {owner_id}, (name, after)


def _identity(input_json: str, *, grant=("00000000-0000-4000-8000-000000000001", 1), actor_id=2, project_id=1):
    from datetime import UTC, datetime

    return TaskExecutionIdentity(
        task_id=1, task_key="00000000-0000-4000-8000-000000000002", actor_id=actor_id, project_id=project_id,
        chapter_id=None, kind="outline.continue", resource_key=None, serial_key=None, retry_of_id=None,
        queued_at=datetime.now(UTC), execution_key="00000000-0000-4000-8000-000000000003", grant_identity=grant,
        step_cursor=0, actor_token_version=0, input_json=input_json, checkpoint_json="null",
    )


def test_frozen_project_owner_id_reads_bindings_and_fails_closed():
    from app.core.errors import ConflictError

    assert frozen_project_owner_id(_identity('{"_project_model":{"project_owner_id":1}}', grant=None)) is None
    assert frozen_project_owner_id(_identity('{"_project_model":{"project_owner_id":1}}')) == 1
    assert frozen_project_owner_id(_identity(
        '{"_project_model":{"project_owner_id":1},"_shared_embedding":{"project_owner_id":1}}',
    )) == 1
    for bad in (
        "{}",  # 协作任务没有服务端绑定
        '{"_project_model":{"project_owner_id":1},"_shared_embedding":{"project_owner_id":3}}',  # 绑定不一致
        '{"_project_model":{"project_owner_id":2}}',  # 作者等于 actor
        '{"_project_model":{"project_owner_id":true}}',
        '{"_project_model":{"project_owner_id":"1"}}',
        '{"_project_model":null}',
    ):
        with pytest.raises(ConflictError):
            frozen_project_owner_id(_identity(bad))
