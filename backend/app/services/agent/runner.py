"""持久化工具循环。每次模型响应先落库，再处理调用；断点重试不重复写入。"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.ai.providers import AIProviderError, LLMProvider
from app.core.errors import AppError, ConflictError
from app.models import AgentChange, AgentConversation, AgentMessage, AIConfig, Task
from app.schemas.agent import ChangeOut
from app.services import project_service
from app.services.agent import context, mcp_tools, transactions
from app.services.agent.tools import get_tool, list_tools
from app.services.agent.tools.base import ToolCtx
from app.services.task_engine.registry import Event, TaskContext

MAX_TOOL_CALLS = 8
MAX_WRITE_PROPOSALS = 50
AUTO_EXECUTE_LIMIT = 3


@dataclass
class AgentJob:
    task_id: int
    user_id: int
    project_id: int
    conversation_id: int
    input_message_id: int
    config: AIConfig
    provider: LLMProvider


async def _checkpoint(db, task_id: int, values: dict) -> None:
    task = await db.get(Task, task_id)
    task.checkpoint = dict(values)
    await db.commit()


async def _result(db, job: AgentJob, call: dict, data: dict) -> None:
    exists = await db.scalar(
        select(AgentMessage.id).where(
            AgentMessage.conversation_id == job.conversation_id,
            AgentMessage.turn_task_id == job.task_id,
            AgentMessage.tool_call_id == call["id"],
        )
    )
    if exists is None:
        db.add(
            AgentMessage(
                conversation_id=job.conversation_id,
                turn_task_id=job.task_id,
                role="tool",
                content=context.tool_result(data),
                tool_call_id=call["id"],
                tool_name=call["name"],
            )
        )
        await db.commit()


def _out(change: AgentChange) -> dict:
    return ChangeOut.model_validate(change).model_dump(mode="json")


def _can_auto_execute(change: AgentChange) -> bool:
    # 外部插件调用不可撤销，一律需要确认（免确认工具在 runner 中直接调用，不生成提案）。
    if change.kind == "external":
        return False
    # 整体同步可能修改或删除多条子记录，即使顶层只有一个 small 提案也必须确认。
    if change.tool_name == "update_chapter_plans":
        return False
    if change.tool_name == "update_career" and change.arguments.get("stages") is not None:
        return False
    return change.kind in ("small", "text")


async def _external_call(
    job: AgentJob,
    db,
    call: dict,
    args: dict,
    external: "mcp_tools.Resolved | None",
    sessions: mcp_tools.McpSessions,
    batch: list[AgentChange],
) -> AsyncIterator[Event]:
    """MCP 工具：先按 inputSchema 校验；免确认工具直接调用，其余生成 external 提案（不可撤销）。"""
    if external is None:
        await _result(db, job, call, {"error": mcp_tools.CHANGED})
        return
    yield "stage", {"stage": f"调用工具：{external.label}"}
    yield "tool_start", {"call_id": call["id"], "name": call["name"], "label": external.label}
    invalid = mcp_tools.validate(external, args)
    if invalid is not None:
        await _result(db, job, call, invalid)
        yield "tool_end", {"call_id": call["id"], "ok": False, "summary": invalid["error"]}
        return
    if external.auto_confirm:
        value = await mcp_tools.call_data(sessions, external, args)
        await _result(db, job, call, value)
        yield (
            "tool_end",
            {"call_id": call["id"], "ok": "error" not in value, "summary": str(value.get("error") or "调用完成")},
        )
        return
    count = await db.scalar(select(func.count(AgentChange.id)).where(AgentChange.turn_task_id == job.task_id))
    if (count or 0) >= MAX_WRITE_PROPOSALS:
        await _result(db, job, call, {"error": "单轮最多 50 项写操作，请分批进行"})
        return
    change = AgentChange(
        conversation_id=job.conversation_id,
        project_id=job.project_id,
        turn_task_id=job.task_id,
        tool_call_id=call["id"],
        tool_name=call["name"],
        arguments=args,
        kind="external",
        preview=mcp_tools.preview(external.entry, external, args),
        seq=(count or 0) + 1,
    )
    db.add(change)
    await db.commit()
    await db.refresh(change)
    batch.append(change)
    yield "proposal", {"change": _out(change)}


async def _process_calls(
    job: AgentJob, ctx: TaskContext, checkpoint: dict, sessions: mcp_tools.McpSessions
) -> AsyncIterator[Event]:
    from app.services.agent import service

    tool_map = checkpoint.get("mcp_tool_map") or {}

    async with transactions.session(ctx.sessionmaker) as db:
        # 重试 / 续跑时，停在 executing 的中断外部调用判为失败（不重新调用），否则这一轮会一直等待确认
        if await service.recover_external(db, task_id=job.task_id):
            await db.commit()
        assistant = await db.get(AgentMessage, checkpoint["assistant_message_id"])
        calls = assistant.tool_calls or []
        project = await project_service.get_owned_project(db, job.user_id, job.project_id)
        tool_ctx = ToolCtx(db, project, job.user_id, ctx)
        batch = []
        for index, call in enumerate(calls):
            existing = await db.scalar(
                select(AgentMessage.id).where(
                    AgentMessage.conversation_id == job.conversation_id,
                    AgentMessage.turn_task_id == job.task_id,
                    AgentMessage.tool_call_id == call["id"],
                )
            )
            if existing is not None:
                continue
            change = await db.scalar(
                select(AgentChange).where(
                    AgentChange.conversation_id == job.conversation_id,
                    AgentChange.turn_task_id == job.task_id,
                    AgentChange.tool_call_id == call["id"],
                )
            )
            if change is not None:
                batch.append(change)
                continue
            if index >= checkpoint.get("allowed_calls", 0):
                await _result(db, job, call, {"error": "本轮最多调用 8 次工具，请直接回答或分批进行"})
                continue
            tool = get_tool(call["name"])
            try:
                args = json.loads(call["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("参数必须是对象")
            except (ValueError, TypeError):
                await _result(db, job, call, {"error": "工具参数必须是合法的 JSON 对象"})
                continue
            if tool is None and call["name"] in tool_map:
                external = await mcp_tools.resolve(
                    db, ctx.settings, job.user_id, job.conversation_id, tool_map, call["name"]
                )
                async for event in _external_call(job, db, call, args, external, sessions, batch):
                    yield event
                continue
            if tool is None:
                await _result(db, job, call, {"error": "工具不存在"})
                continue
            yield "stage", {"stage": f"调用工具：{tool.name}"}
            yield "tool_start", {"call_id": call["id"], "name": tool.name, "label": tool.description}
            if tool.kind == "read":
                try:
                    value = await tool.run(tool_ctx, args)
                except AppError as exc:
                    value = {"error": exc.detail}
                await _result(db, job, call, value)
                yield (
                    "tool_end",
                    {
                        "call_id": call["id"],
                        "ok": "error" not in value,
                        "summary": str(value.get("error") or "读取完成"),
                    },
                )
                continue
            count = await db.scalar(select(func.count(AgentChange.id)).where(AgentChange.turn_task_id == job.task_id))
            if (count or 0) >= MAX_WRITE_PROPOSALS:
                await _result(db, job, call, {"error": "单轮最多 50 项写操作，请分批进行"})
                continue
            preview = await tool.preview(tool_ctx, args)
            if "error" in preview:
                await _result(db, job, call, preview)
                yield "tool_end", {"call_id": call["id"], "ok": False, "summary": str(preview["error"])}
                continue
            change = AgentChange(
                conversation_id=job.conversation_id,
                project_id=job.project_id,
                turn_task_id=job.task_id,
                tool_call_id=call["id"],
                tool_name=tool.name,
                arguments=args,
                kind=tool.kind,
                preview=preview,
                seq=(count or 0) + 1,
            )
            db.add(change)
            await db.commit()
            await db.refresh(change)
            batch.append(change)
            yield "proposal", {"change": _out(change)}
        pending = [c for c in batch if c.status == "proposed"]
        conversation = await db.get(AgentConversation, job.conversation_id, populate_existing=True)
        total = await db.scalar(select(func.count(AgentChange.id)).where(AgentChange.turn_task_id == job.task_id))
        auto = (
            conversation.auto_execute
            and pending
            and not checkpoint.get("auto_execute_used")
            and total == len(pending)
            and len(pending) <= AUTO_EXECUTE_LIMIT
            and all(_can_auto_execute(c) for c in pending)
        )
        if auto:
            checkpoint["auto_execute_used"] = True
            await _checkpoint(db, job.task_id, checkpoint)
            for change in pending:
                try:
                    await service.execute_change(db, ctx, change, user_id=job.user_id)
                except ConflictError as exc:
                    if not exc.extra or exc.extra.get("code") != "preview_changed":
                        raise
                    # 数据在提案到自动执行之间变化：保留最新预览并等待明确确认。
                    break
                await db.commit()
                await db.refresh(change)
                yield "executed", {"change": _out(change)}
        pending = [c.id for c in batch if c.status in ("proposed", "executing")]
        checkpoint["pending_change_ids"] = pending
        for change in batch:
            if change.id not in pending:
                await _result(
                    db,
                    job,
                    {"id": change.tool_call_id, "name": change.tool_name},
                    change.result or {"error": change.error or "修改未执行"},
                )
                yield (
                    "tool_end",
                    {
                        "call_id": change.tool_call_id,
                        "ok": change.status == "executed",
                        "summary": str((change.result or {}).get("message") or change.error or "修改已执行"),
                    },
                )
        if pending:
            await _checkpoint(db, job.task_id, checkpoint)
            yield "stage", {"stage": f"等待确认 {len(pending)} 项修改"}
            yield "paused", {"reason": "confirm", "change_ids": pending}
            return
        checkpoint["processed_assistant_id"] = assistant.id
        checkpoint["pending_change_ids"] = []
        await _checkpoint(db, job.task_id, checkpoint)


async def _save_log(
    job: AgentJob,
    ctx: TaskContext,
    *,
    messages: list,
    prompt: dict,
    round_no: int,
    text: str,
    calls: list,
    usage: dict,
    started: float,
    status: str,
    error: str = "",
) -> None:
    from app.services import generation_log_service

    async with transactions.session(ctx.sessionmaker) as db:
        log = await generation_log_service.write_log(
            db,
            project_id=job.project_id,
            chapter_id=None,
            kind="agent.turn",
            task_id=job.task_id,
            ai_config_id=job.config.id,
            provider=job.config.provider,
            model=job.config.model,
            params={
                "conversation_id": job.conversation_id,
                "round": round_no,
                "tool_calls": calls,
                "prompt": prompt,
            },
            messages=messages,
            status=status,
            error=error,
            output_chars=len(text),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        log.prompt_tokens = usage.get("input_tokens")
        log.completion_tokens = usage.get("output_tokens")
        await db.commit()


async def _run_agent(job: AgentJob, ctx: TaskContext) -> AsyncIterator[Event]:
    """一次运行内复用 MCP 连接；运行结束、暂停或异常时统一关闭。"""
    sessions = mcp_tools.McpSessions(
        ctx.mcp_connector, ctx.cipher, ctx.settings.allow_private_ai_endpoints, settings=ctx.settings,
    )
    try:
        async with aclosing(_turn(job, ctx, sessions)) as events:
            async for event in events:
                yield event
    finally:
        await sessions.aclose()


async def _turn(job: AgentJob, ctx: TaskContext, sessions: mcp_tools.McpSessions) -> AsyncIterator[Event]:
    async with transactions.session(ctx.sessionmaker) as db:
        task = await db.get(Task, job.task_id)
        checkpoint = dict(task.checkpoint or {})
        if "mcp_tool_map" not in checkpoint:
            # 本轮 MCP 映射表：续跑 / 重试沿用，确认期间修改配置不会让名称漂移
            conversation = await db.get(AgentConversation, job.conversation_id)
            checkpoint["mcp_tool_map"] = await mcp_tools.build_tool_map(db, ctx.settings, job.user_id, conversation)
            await _checkpoint(db, job.task_id, checkpoint)
        history_text = "".join(
            await db.scalars(
                select(AgentMessage.content)
                .where(
                    AgentMessage.turn_task_id == job.task_id,
                    AgentMessage.role == "assistant",
                )
                .order_by(AgentMessage.id)
            )
        )
    yield "reset", {}
    if history_text:
        yield "delta", {"text": history_text}
    if checkpoint.get("finished_message_id"):
        yield "done", {"message_id": checkpoint["finished_message_id"], "usage": checkpoint.get("usage", {})}
        return
    while True:
        if checkpoint.get("assistant_message_id") != checkpoint.get("processed_assistant_id"):
            paused = False
            async with aclosing(_process_calls(job, ctx, checkpoint, sessions)) as events:
                async for event in events:
                    if event[0] == "paused":
                        paused = True
                    yield event
            if paused:
                return
        async with transactions.session(ctx.sessionmaker) as db:
            conversation = await db.get(AgentConversation, job.conversation_id)
            project = await project_service.get_owned_project(db, job.user_id, job.project_id)
            user_message = await db.get(AgentMessage, job.input_message_id)
            try:
                messages, prompt = await context.build_messages(
                    db, conversation, project, job.config, job.provider, user_message.page_context, job.task_id
                )
            except (AppError, AIProviderError) as exc:
                yield "error", {"detail": str(exc)}
                return
            external_specs = await mcp_tools.tool_specs(
                db,
                ctx.settings,
                job.user_id,
                job.conversation_id,
                checkpoint.get("mcp_tool_map") or {},
                job.config.provider,
            )
        tools = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in list_tools()]
        tools.extend(external_specs)
        used = checkpoint.get("tool_calls_used", 0)
        choice = "none" if used >= MAX_TOOL_CALLS else "auto"
        round_no = checkpoint.get("round", 0) + 1
        pieces, calls, usage = [], [], {}
        started = time.monotonic()
        try:
            async with aclosing(
                job.provider.chat_tools_stream(
                    messages,
                    job.config.model,
                    tools,
                    tool_choice=choice,
                    max_tokens=job.config.max_output_k * 1000 if job.config.max_output_k else None,
                    thinking=job.config.thinking_level,
                )
            ) as stream:
                async for name, data in stream:
                    if name == "text":
                        pieces.append(data["text"])
                        yield "delta", {"text": data["text"]}
                    elif name == "tool_call":
                        calls.append(dict(data))
                    elif name == "usage":
                        usage.update(data)
        except asyncio.CancelledError:
            await _save_log(
                job,
                ctx,
                messages=messages,
                prompt=prompt,
                round_no=round_no,
                text="".join(pieces),
                calls=calls,
                usage=usage,
                started=started,
                status="cancelled",
            )
            raise
        except (AIProviderError, AppError) as exc:
            await _save_log(
                job,
                ctx,
                messages=messages,
                prompt=prompt,
                round_no=round_no,
                text="".join(pieces),
                calls=calls,
                usage=usage,
                started=started,
                status="error",
                error=str(exc),
            )
            yield "error", {"detail": str(exc)}
            return
        text = "".join(pieces)
        await _save_log(
            job,
            ctx,
            messages=messages,
            prompt=prompt,
            round_no=round_no,
            text=text,
            calls=calls,
            usage=usage,
            started=started,
            status="success",
        )
        async with transactions.session(ctx.sessionmaker) as db:
            assistant = AgentMessage(
                conversation_id=job.conversation_id,
                turn_task_id=job.task_id,
                role="assistant",
                content=text,
                tool_calls=calls or None,
                usage=usage,
            )
            db.add(assistant)
            await db.flush()
            allowed = max(0, min(len(calls), MAX_TOOL_CALLS - used))
            cumulative = dict(checkpoint.get("usage") or {})
            for key in ("input_tokens", "output_tokens"):
                cumulative[key] = cumulative.get(key, 0) + int(usage.get(key, 0))
            checkpoint.update(
                round=round_no,
                assistant_message_id=assistant.id,
                allowed_calls=allowed,
                tool_calls_used=used + allowed,
                usage=cumulative,
            )
            if not calls:
                checkpoint["finished_message_id"] = assistant.id
            await _checkpoint(db, job.task_id, checkpoint)
        if not calls:
            yield "done", {"message_id": assistant.id, "usage": cumulative}
            return
        if choice == "none":
            # 不遵守 tool_choice 的模型也不能导致无限循环；补全每个协议结果再明确结束。
            async with transactions.session(ctx.sessionmaker) as db:
                for call in calls:
                    await _result(db, job, call, {"error": "已达到工具调用上限，请直接回答"})
                final = AgentMessage(
                    conversation_id=job.conversation_id,
                    turn_task_id=job.task_id,
                    role="assistant",
                    content="本轮已达到工具调用上限，请分批继续。",
                )
                db.add(final)
                await db.flush()
                checkpoint["finished_message_id"] = final.id
                checkpoint["processed_assistant_id"] = assistant.id
                await _checkpoint(db, job.task_id, checkpoint)
            yield "delta", {"text": final.content}
            yield "done", {"message_id": final.id, "usage": cumulative}
            return


async def run_agent(job: AgentJob, ctx: TaskContext) -> AsyncIterator[Event]:
    try:
        async with aclosing(_run_agent(job, ctx)) as events:
            async for event in events:
                yield event
    except DBAPIError as exc:
        if not transactions.is_serialization_error(exc):
            raise
        yield "error", {"detail": transactions.CONCURRENT_CHANGE}
    except ConflictError as exc:
        yield "error", {"detail": exc.detail}
