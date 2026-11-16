"""真实 PG：Agent 事件交付后的进度提交不能污染下一段 REPEATABLE READ。"""

import asyncio
from collections import Counter
from contextlib import aclosing
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.core.errors import AppError
from app.models import AgentChange, AgentMessage, AIConfig, Character, GenerationLog, Project, Task, User
from app.services import authorization_service as authz
from app.services.agent import service
from app.services.agent.tools.base import Tool
from app.services.task_engine.handlers import agent as agent_handler
from tests import pg_support
from tests.helpers import wait_task, wait_until
from tests.mcp_fakes import FakeMcpConnector
from tests.test_agent_e4ig_postgresql import AUTH_STOPPED, TOKEN_STOPPED, _change, _client, _pg_world
from tests.test_agent_entries_e4id import _detail, _send
from tests.test_agent_mcp_skills import URL, change_action, send_task, setup
from tests.test_agent_revocation_e4ie import ORIGINAL, STOP, _changes
from tests.test_agent_runtime import call


@pytest.fixture
async def pg_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="Agent 进度刷新事务测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2, start_first_engine=True,
    ) as apps:
        yield apps


@pytest.fixture
async def flush_world(pg_apps, monkeypatch):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        observed = SimpleNamespace(flushes=[], sqlstates=[], output_updates=0, tools=Counter())
        world.observed = observed
        engine = editor_app.state.task_engine
        monkeypatch.setattr(engine, "flush_interval", 0)
        original_flush = engine._flush

        async def flushed(hub, **kwargs):
            await original_flush(hub, **kwargs)
            observed.flushes.append(hub.stage)

        monkeypatch.setattr(engine, "_flush", flushed)

        def sql_error(context):
            original = context.original_exception
            cause = getattr(original, "__cause__", None)
            observed.sqlstates.append(getattr(original, "sqlstate", None)
                                      or getattr(cause, "sqlstate", None))

        def sql_written(_conn, _cursor, statement, _parameters, _context, _executemany):
            normalized = statement.lstrip().lower()
            if normalized.startswith("update tasks set ") and "output_text" in normalized:
                observed.output_updates += 1

        for method in ("run", "preview", "execute"):
            original = getattr(Tool, method)

            def observe(operation, name):
                async def wrapped(tool, ctx, arguments):
                    observed.tools[(name, tool.name)] += 1
                    return await operation(tool, ctx, arguments)

                return wrapped

            monkeypatch.setattr(Tool, method, observe(original, method))
        sync_engine = editor_app.state.engine.sync_engine
        event.listen(sync_engine, "handle_error", sql_error)
        event.listen(sync_engine, "after_cursor_execute", sql_written)
        try:
            yield world
        finally:
            event.remove(sync_engine, "handle_error", sql_error)
            event.remove(sync_engine, "after_cursor_execute", sql_written)


def _record_flushes(world, record_property):
    observed = world.observed
    record_property("real_progress_flushes", len(observed.flushes))
    record_property("task_output_updates", observed.output_updates)
    record_property("postgresql_sqlstates", ",".join(str(code) for code in observed.sqlstates))
    assert len(observed.flushes) >= 2
    assert observed.output_updates >= len(observed.flushes)
    assert observed.sqlstates == []


@pytest.mark.parametrize("auto", [False, True], ids=["proposals", "automatic"])
async def test_write_tool_events_survive_real_progress_flush(flush_world, fake_llm, record_property, auto):
    world = flush_world
    if auto:
        response = await world.editor_client.patch(
            f"{world.base}/conversations/{world.cid}", json={"auto_execute": True},
        )
        assert response.status_code == 200, response.text
    fake_llm.push_tools([
        call("update_project", {"synopsis": "刷新后的简介"}, "first"),
        call("create_character", {"name": "刷新角色"}, "second"),
    ])
    if auto:
        fake_llm.push_tools([("text", {"text": "两项已完成。"})])
    turn = await _send(world.editor_client, world.base, world.cid, "两项修改")
    task = await wait_task(world.editor_client, turn, statuses=STOP, within=20)
    assert task["status"] == ("succeeded" if auto else "paused"), task
    _record_flushes(world, record_property)
    changes = await _changes(world.editor_app, turn["id"])
    assert [(item.tool_call_id, item.status) for item in changes] == [
        ("first", "executed" if auto else "proposed"),
        ("second", "executed" if auto else "proposed"),
    ]
    assert len(fake_llm.tool_calls) == (2 if auto else 1)
    assert world.observed.tools[("execute", "update_project")] == (1 if auto else 0)
    assert world.observed.tools[("execute", "create_character")] == (1 if auto else 0)
    async with world.editor_app.state.sessionmaker() as db:
        project = await db.get(Project, world.project["id"])
        assert (project.synopsis, project.theme) == (
            ("刷新后的简介", ORIGINAL["theme"]) if auto else (ORIGINAL["synopsis"], ORIGINAL["theme"])
        )
        names = list(await db.scalars(select(Character.name).where(Character.project_id == world.project["id"])))
        assert names == (["刷新角色"] if auto else [])
        results = list(await db.scalars(select(AgentMessage.tool_call_id).where(
            AgentMessage.turn_task_id == turn["id"], AgentMessage.role == "tool",
        ).order_by(AgentMessage.id)))
        assert results == (["first", "second"] if auto else [])
        assert await db.scalar(select(func.count(GenerationLog.id)).where(
            GenerationLog.task_id == turn["id"],
        )) == (2 if auto else 1)


async def test_read_tool_results_survive_progress_flush_without_reexecution(
    flush_world, fake_llm, record_property,
):
    world = flush_world
    fake_llm.push_tools([
        call("get_project_overview", {}, "overview"),
        call("list_characters", {}, "characters"),
    ])
    fake_llm.push_tools([("text", {"text": "已读取作品和角色。"})])
    turn = await _send(world.editor_client, world.base, world.cid)
    task = await wait_task(world.editor_client, turn, statuses=STOP, within=20)
    assert task["status"] == "succeeded", task
    _record_flushes(world, record_property)
    assert len(fake_llm.tool_calls) == 2
    assert world.observed.tools[("run", "get_project_overview")] == 1
    assert world.observed.tools[("run", "list_characters")] == 1
    async with world.editor_app.state.sessionmaker() as db:
        results = list(await db.scalars(select(AgentMessage.tool_call_id).where(
            AgentMessage.turn_task_id == turn["id"], AgentMessage.role == "tool",
        ).order_by(AgentMessage.id)))
        assert results == ["overview", "characters"]
        assert await db.scalar(select(func.count(AgentChange.id)).where(
            AgentChange.turn_task_id == turn["id"],
        )) == 0


@pytest.mark.parametrize("identity", ["token", "grant", "execution", "model"])
async def test_identity_change_after_tool_stage_flush_cannot_create_proposal(
    flush_world, fake_llm, monkeypatch, record_property, identity,
):
    world = flush_world
    engine = world.editor_app.state.task_engine
    entered, release = asyncio.Event(), asyncio.Event()
    original_flush = engine._flush
    original_event_task = engine._event_task
    original_verify = agent_handler.AgentHandler.verify_event
    rejections = []

    async def observe_event_task(*args, **kwargs):
        try:
            return await original_event_task(*args, **kwargs)
        except AppError as exc:
            rejections.append(("execution", (exc.extra or {}).get("code"), exc.detail))
            raise

    async def observe_verify(self, *args, **kwargs):
        try:
            return await original_verify(self, *args, **kwargs)
        except AppError as exc:
            rejections.append(("resources", (exc.extra or {}).get("code"), exc.detail))
            raise

    monkeypatch.setattr(engine, "_event_task", observe_event_task)
    monkeypatch.setattr(agent_handler.AgentHandler, "verify_event", observe_verify)

    async def held(hub, **kwargs):
        await original_flush(hub, **kwargs)
        if hub.stage == "调用工具：update_project" and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

    monkeypatch.setattr(engine, "_flush", held)
    fake_llm.push_tools([call("update_project", {"synopsis": "不能提交"}, "denied")])
    turn = await _send(world.editor_client, world.base, world.cid)
    replacement = str(uuid4())
    try:
        await asyncio.wait_for(entered.wait(), 15)
        if identity == "grant":
            await _change(world, "viewer")
        else:
            async with world.owner_app.state.sessionmaker() as db:
                if identity == "token":
                    await db.execute(update(User).where(User.id == world.editor["id"]).values(
                        token_version=User.token_version + 1,
                    ))
                elif identity == "execution":
                    await db.execute(update(Task).where(Task.id == turn["id"]).values(execution_key=replacement))
                else:
                    config = await db.scalar(select(AIConfig).where(AIConfig.user_id == world.owner["id"]))
                    fields = {name: getattr(config, name) for name in (
                        "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
                        "is_default", "thinking_level", "context_window_k", "max_output_k", "created_at", "updated_at",
                    )}
                    config_id = config.id
                    await db.delete(config)
                    await db.flush()
                    db.add(AIConfig(id=config_id, instance_key=replacement, **fields))
                await db.commit()
    finally:
        release.set()
    await wait_until(lambda: turn["id"] not in engine._running, within=15)
    record_property("original_guard_rejections", str(rejections))
    expected_guard = {
        "token": ("execution", None, "未登录或登录已过期"),
        "grant": ("execution", "task_execution_changed", "任务执行身份已变化，请核对后重试"),
        "execution": ("execution", "task_execution_changed", "任务执行身份已变化，请核对后重试"),
        "model": ("resources", "agent_execution_changed", "智能体任务已变化，请重新发起"),
    }
    assert expected_guard[identity] in rejections
    assert len(fake_llm.tool_calls) == 1
    assert world.observed.sqlstates == []
    assert world.observed.tools[("execute", "update_project")] == 0
    assert await _changes(world.editor_app, turn["id"]) == []
    async with world.editor_app.state.sessionmaker() as db:
        project = await db.get(Project, world.project["id"])
        assert project.synopsis == ORIGINAL["synopsis"]
        row = await db.get(Task, turn["id"])
        if identity == "execution":
            assert row.execution_key == replacement and row.status == "running"
        else:
            assert row.status == "failed"
            assert row.error == TOKEN_STOPPED if identity == "token" else row.error in AUTH_STOPPED, row.error
        assert await db.scalar(select(func.count(AgentMessage.id)).where(
            AgentMessage.turn_task_id == turn["id"], AgentMessage.role == "tool",
        )) == 0


@pytest.mark.parametrize("auto_confirm", [False, True], ids=["manual-confirm", "automatic-denied"])
async def test_owner_external_tool_progress_flush_preserves_send_boundary(
    flush_world, fake_llm, monkeypatch, record_property, auto_confirm,
):
    world = flush_world
    connector = FakeMcpConnector()
    monkeypatch.setattr(world.editor_app.state.task_engine.ctx, "mcp_connector", connector)
    async with _client(world.editor_app) as author:
        _, base, url, _ = await setup(author, connector, prefs={"send": {"auto_confirm": auto_confirm}})
        fake_llm.push_tools([call("mcp__dict__send", {"to": "测试编辑部"}, "external-once")])
        if not auto_confirm:
            fake_llm.push_tools([("text", {"text": "外部操作已确认完成。"})])
        turn = await send_task(author, url)
        task = await wait_task(author, turn, statuses=STOP, within=20)
        assert connector.calls == []
        detail = (await author.get(url)).json()
        if auto_confirm:
            assert task["status"] == "failed", task
            assert task["error"] == "自动外部调用暂不可用，请稍后重试", task
            assert detail["changes"] == []
            assert len(fake_llm.tool_calls) == 1
        else:
            assert task["status"] == "paused", task
            assert len(detail["changes"]) == 1
            proposal = detail["changes"][0]
            assert (proposal["kind"], proposal["status"]) == ("external", "proposed")
            response = await author.post(
                f"{base}/changes/{proposal['id']}/confirm", json=change_action(proposal),
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "executed"
            task = await wait_task(author, turn, within=20)
            assert task["status"] == "succeeded", task
            assert connector.calls == [(URL, "send", {"to": "测试编辑部"})]
            assert len(fake_llm.tool_calls) == 2
        _record_flushes(world, record_property)
        assert connector.open == 0 and connector.connects == connector.closes
        async with world.editor_app.state.sessionmaker() as db:
            results = list(await db.scalars(select(AgentMessage.tool_call_id).where(
                AgentMessage.turn_task_id == turn["id"], AgentMessage.role == "tool",
            )))
            assert results == ([] if auto_confirm else ["external-once"])
            assert await db.scalar(select(func.count(GenerationLog.id)).where(
                GenerationLog.task_id == turn["id"],
            )) == (1 if auto_confirm else 2)


async def test_changed_auto_preview_survives_progress_flush_without_execution(
    flush_world, fake_llm, monkeypatch, record_property,
):
    world = flush_world
    response = await world.editor_client.patch(
        f"{world.base}/conversations/{world.cid}", json={"auto_execute": True},
    )
    assert response.status_code == 200, response.text
    original_run = agent_handler.run_agent
    changed = []
    snapshot_updates = []
    original_preview_changed = service._preview_changed

    async def change_after_refreshed_preview(db, *args):
        result = await original_preview_changed(db, *args)
        # service 已提交新预览并重新读取；第二连接真实更新门禁，必须结束这段旧 RR 后才能再写。
        async with world.owner_app.state.sessionmaker() as independent:
            await authz.gate(independent)
            await independent.commit()
        snapshot_updates.append(True)
        return result

    monkeypatch.setattr(service, "_preview_changed", change_after_refreshed_preview)

    async def change_after_proposal(job, ctx):
        async with aclosing(original_run(job, ctx)) as events:
            async for name, data in events:
                yield name, data
                if name == "proposal" and not changed:
                    # 提案已真实交付、原事务已提交，此时由第二实例修改目标。
                    response = await world.owner_client.patch(
                        f"/api/v1/projects/{world.project['id']}", json={"synopsis": "作者的新简介"},
                    )
                    assert response.status_code == 200, response.text
                    changed.append(data["change"]["id"])

    monkeypatch.setattr(agent_handler, "run_agent", change_after_proposal)
    fake_llm.push_tools([call("update_project", {"synopsis": "自动覆盖"}, "changed")])
    turn = await _send(world.editor_client, world.base, world.cid)
    task = await wait_task(world.editor_client, turn, statuses=STOP, within=20)
    assert task["status"] == "paused", task
    assert len(snapshot_updates) == 1
    _record_flushes(world, record_property)
    assert len(fake_llm.tool_calls) == 1
    assert world.observed.tools[("execute", "update_project")] == 0
    detail = await _detail(world.editor_client, world.base, world.cid)
    assert len(detail["changes"]) == 1 and detail["changes"][0]["status"] == "proposed"
    assert "作者的新简介" in str(detail["changes"][0]["preview"])
    async with world.editor_app.state.sessionmaker() as db:
        assert (await db.get(Project, world.project["id"])).synopsis == "作者的新简介"
