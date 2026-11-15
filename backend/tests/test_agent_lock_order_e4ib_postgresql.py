"""E4i-b 裁决 16：协作者 Agent 运行器锁序在真实 PostgreSQL 上不与作者发送 lease 交叉等待。

旧顺序：编辑者 `_lock_execution` 先取发送写门禁，之后工具 `lock_agent_write` 才锁作者 User；作者发送 lease
先 FOR SHARE 作者 User 再等共享门禁 → 交叉等待（门禁为轮询 advisory，作者在 2 秒后以 ai_visibility_busy 失败）。
新顺序：作者 User 与 actor 一起在门禁之前按 ID 升序锁定，作者 lease 只是在 User 行上排队，编辑者提交后照常完成。

编排只用 Event 与 pg_blocking_pids 观测（无固定等待）；双方都必须成功。
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.models import AgentConversation, Task, User
from app.services import ai_dispatch, project_ai_config
from app.services import authorization_service as authz
from app.services.agent import access as agent_access
from app.services.agent import runner, transactions
from app.services.task_engine.execution_identity import from_queued_task
from tests import pg_support
from tests.helpers import create_ai_config, create_project, register, wait_task

OWNER_KEY = "sk-owner-e4ib-pg-0001"


@pytest.fixture
async def postgres_agent_lock_app(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="Agent 锁序测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=1,
    ) as apps:
        yield apps[0]


async def _world(app, owner_client, editor_client, *, allow_owner_ai=False):
    owner = await register(owner_client, "e4ib-pg-owner")
    await create_ai_config(owner_client, api_key=OWNER_KEY)
    project = await create_project(owner_client)
    editor = await register(editor_client, "e4ib-pg-editor")
    base = f"/api/v1/projects/{project['id']}"
    invitation = await owner_client.post(f"{base}/collaborators", json={
        "username": "e4ib-pg-editor", "role": "editor", "allow_owner_ai": allow_owner_ai,
    })
    assert invitation.status_code == 201, invitation.text
    accepted = await editor_client.post(
        f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
        json={"expected_revision": invitation.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    async with app.state.sessionmaker() as db:
        # 与真实入口（E4i-d）一致：协作者会话记录创建时的邀请轮次。
        conversation = AgentConversation(user_id=editor["id"], project_id=project["id"],
                                         collaboration_invitation_id=member["invitation_id"])
        db.add(conversation)
        await db.commit()
        cid = conversation.id
    # 作者先注册：作者 ID 更小，旧顺序下“actor 先锁、作者后锁”恰为非升序。
    assert owner["id"] < editor["id"]
    return SimpleNamespace(owner=owner, editor=editor, project=project, conversation_id=cid,
                           grant=(member["invitation_id"], member["revision"]))


async def _editor_job(app, world):
    async with app.state.sessionmaker() as db:
        version = (await db.get(User, world.editor["id"])).token_version
        task = Task(user_id=world.editor["id"], project_id=world.project["id"], kind="agent.turn", status="queued",
                    input={"request": {"conversation_id": world.conversation_id}, "message_id": 1,
                           "_agent_actor_token_version": version, "_task_actor_token_version": version,
                           "_collaboration_grant": {"invitation_id": world.grant[0], "revision": world.grant[1]}})
        db.add(task)
        await db.flush()
        execution = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", execution.execution_key
        await db.commit()
    binding = project_ai_config.ResolvedProjectModel(
        project_id=world.project["id"], actor_id=world.editor["id"], project_owner_id=world.owner["id"],
        model_type="text", source="actor", config_id=1, config_owner_id=world.editor["id"],
        grant_identity=world.grant, config_fingerprint="0" * 64, config_instance_key=str(uuid4()),
    )
    return runner.AgentJob(
        task.id, world.editor["id"], world.project["id"], world.conversation_id, 1, None, None, None,
        task_key=task.statistics_key, queued_at=execution.queued_at, execution_key=execution.execution_key,
        actor_token_version=version, execution=execution, execution_state=runner._AgentExecutionState(execution),
        model_binding=binding,
    )


async def _blocked_by(maker, waiter_pid: int, holder_pid: int) -> bool:
    async with maker() as db:
        return bool(await db.scalar(
            text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"), {"holder": holder_pid, "waiter": waiter_pid},
        ))


async def test_collaborator_runner_lock_order_does_not_deadlock_with_author_send_lease(postgres_agent_lock_app):
    app = postgres_agent_lock_app
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as owner_client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as editor_client,
    ):
        world = await _world(app, owner_client, editor_client)
    job = await _editor_job(app, world)
    pids: dict[str, int] = {}
    editor_locked, author_started, author_user_locked, proceed = (asyncio.Event() for _ in range(4))

    async def editor_run():
        # 运行器的真实写事务：先 _lock_execution（含发送写门禁），再像工具一样锁后复核 Agent 写能力。
        async with transactions.session(app.state.sessionmaker) as db:
            await runner._lock_execution(db, job)
            pids["editor"] = await db.scalar(text("SELECT pg_backend_pid()"))
            editor_locked.set()
            await proceed.wait()
            await agent_access.lock_agent_write(
                db, world.editor["id"], world.project["id"], grant=world.grant, visibility_write=False,
            )
            await db.commit()

    async def author_send_lease():
        # 作者真实发送 lease 的同一顺序：authorize_send_actor（作者 User FOR SHARE）→ 共享发送门禁。
        await editor_locked.wait()
        async with app.state.sessionmaker() as db:
            pids["author"] = await db.scalar(text("SELECT pg_backend_pid()"))
            author_started.set()
            await authz.require_authenticated_for_send(db, world.owner["id"])
            author_user_locked.set()
            await ai_dispatch._lock(db, world.project["id"], write=False)  # noqa: SLF001
            await db.commit()

    editor = asyncio.create_task(editor_run())
    author = asyncio.create_task(author_send_lease())
    try:
        await asyncio.wait_for(author_started.wait(), 15)

        async def author_waiting_on_editor():
            # 新顺序：作者在作者 User 行上被编辑者阻塞；旧顺序：作者已拿到 User 共享锁、转而轮询门禁。
            while not author_user_locked.is_set():
                if await _blocked_by(app.state.sessionmaker, pids["author"], pids["editor"]):
                    return
                if author.done():
                    return

        await asyncio.wait_for(author_waiting_on_editor(), 15)
        proceed.set()
        results = await asyncio.wait_for(asyncio.gather(editor, author, return_exceptions=True), 30)
    finally:
        proceed.set()
        for work in (editor, author):
            if not work.done():
                work.cancel()
        await asyncio.gather(editor, author, return_exceptions=True)
    # 旧顺序下作者 lease 会在门禁轮询超时后以 ai_visibility_busy 失败（编辑者在作者 User 行上等待）。
    assert results == [None, None], results
    assert author_user_locked.is_set()


async def test_postgresql_editor_turn_falls_back_to_owner_model(postgres_agent_lock_app, fake_llm):
    """真实 PG（Agent 会话 REPEATABLE READ）下编辑者轮次端到端：授权回退作者模型、锁序与发送守卫通过。"""
    from tests.test_agent_handler_e4ib import _send

    app = postgres_agent_lock_app
    await app.state.task_engine.start()
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as owner_client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as editor_client,
    ):
        world = await _world(app, owner_client, editor_client, allow_owner_ai=True)
        task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
        done = await wait_task(editor_client, task, within=20)
    assert done["status"] == "succeeded", done
    assert [item["api_key"] for item in fake_llm.tool_calls] == [OWNER_KEY]
    async with app.state.sessionmaker() as db:
        stored = await db.scalar(select(Task).where(Task.id == task["id"]))
        assert stored.input["_project_model"]["source"] == "owner"


async def test_postgresql_editor_turn_with_compaction_keeps_marked_summary(postgres_agent_lock_app, fake_llm):
    """真实 PG 下编辑者同一轮先压缩（写协作来源标记）再主对话：摘要保存锁序与主发送守卫均通过。"""
    from sqlalchemy import update

    from app.models import AgentMessage, AIConfig
    from app.services.agent import context
    from tests.test_agent_handler_e4ib import _send

    app = postgres_agent_lock_app
    await app.state.task_engine.start()
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as owner_client,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as editor_client,
    ):
        world = await _world(app, owner_client, editor_client, allow_owner_ai=True)
        async with app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.user_id == world.owner["id"]).values(context_window_k=1))
            for index in range(4):
                db.add(AgentMessage(conversation_id=world.conversation_id, role="user",
                                    content=f"旧消息{index}" + "较早" * 500))
            await db.commit()
        fake_llm.push("编辑者摘要")
        task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id, "本轮输入")
        done = await wait_task(editor_client, task, within=20)
    assert done["status"] == "succeeded", done
    assert fake_llm.calls and {item["api_key"] for item in fake_llm.calls} == {OWNER_KEY}
    assert [item["api_key"] for item in fake_llm.tool_calls] == [OWNER_KEY]
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, world.conversation_id)
        assert conversation.summary
        assert conversation.summary_source == context.summary_source_for(world.grant)
