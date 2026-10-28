"""原生工具历史、上下文预算与不可信数据隔离。"""

import asyncio
import json
import time
from contextlib import aclosing
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import format_project_info
from app.ai.prompts.agent import SAFETY_RULES
from app.ai.prompts.registry import prompt_meta, render
from app.ai.providers import LLMProvider
from app.core.errors import BadRequestError
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    AIConfig,
    Chapter,
    Character,
    Outline,
    Project,
)
from app.services import project_service, prompt_service

RESULT_LIMIT = 8000
TRUNCATED = "内容过长已截断，请用分页参数读取"


def untrusted_text(text: str) -> str:
    # 转义边界标记，防止用户数据自行关闭数据区。
    text = text.replace("【不可信数据开始】", "［不可信数据开始］").replace("【不可信数据结束】", "［不可信数据结束］")
    return f"【不可信数据开始】\n{text}\n【不可信数据结束】"


def tool_result(data: Any) -> str:
    payload = json.dumps({"untrusted": True, "data": data}, ensure_ascii=False, default=str)
    if len(payload) <= RESULT_LIMIT:
        return payload
    # 截断嵌套 JSON 后作为字符串重新编码，始终保持外层合法 JSON。
    raw = json.dumps(data, ensure_ascii=False, default=str)
    lo, hi = 0, min(len(raw), RESULT_LIMIT)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = json.dumps({"untrusted": True, "data": raw[:mid], "warning": TRUNCATED}, ensure_ascii=False)
        if len(candidate) <= RESULT_LIMIT:
            lo = mid
        else:
            hi = mid - 1
    return json.dumps({"untrusted": True, "data": raw[:lo], "warning": TRUNCATED}, ensure_ascii=False)


async def validate_page_context(db: AsyncSession, project_id: int, raw: dict | None) -> dict:
    if not raw:
        return {}
    cleaned = {}
    route = str(raw.get("route") or "")[:500]
    if route.startswith(f"/projects/{project_id}/") or route == f"/projects/{project_id}":
        cleaned["route"] = route
    for key, model in (("chapter_id", Chapter), ("outline_id", Outline), ("character_id", Character)):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            exists = await db.scalar(select(model.id).where(model.id == value, model.project_id == project_id))
            if exists is not None:
                cleaned[key] = value
    if cleaned.get("chapter_id") and isinstance(raw.get("selection"), str):
        chapter = await db.get(Chapter, cleaned["chapter_id"])
        selection = raw["selection"][:2000]
        if chapter is not None and selection and selection in chapter.content:
            cleaned["selection"] = selection
    return cleaned


def message_dict(row: AgentMessage) -> dict:
    if row.role == "tool" and not row.tool_call_id:
        # 撤销备注没有对应的 assistant.tool_calls，不能冒充原生 tool_result。
        return {"role": "user", "content": untrusted_text(row.content)}
    value: dict[str, Any] = {"role": row.role, "content": row.content}
    if row.tool_calls:
        value["tool_calls"] = row.tool_calls
    if row.tool_call_id:
        value.update(tool_call_id=row.tool_call_id, name=row.tool_name)
    return value


def _groups(rows: list[AgentMessage]) -> list[list[AgentMessage]]:
    groups: list[list[AgentMessage]] = []
    for row in rows:
        if row.role == "tool" and groups and (row.tool_call_id or groups[-1][0].tool_calls):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _group_messages(group: list[AgentMessage], changes: dict[tuple, AgentChange]) -> list[dict]:
    first = group[0]
    if not first.tool_calls:
        return [message_dict(row) for row in group]
    results = {row.tool_call_id: message_dict(row) for row in group[1:] if row.tool_call_id}
    messages = [message_dict(first)]
    for call in first.tool_calls:
        result = results.get(call["id"])
        if result is None:
            change = changes.get((first.turn_task_id, call["id"]))
            data = {"error": "这次工具调用未完成，请重新查询状态，不要假定操作已执行"}
            if change is not None:
                if change.status == "undone":
                    data = {"message": "用户已撤销该修改"}
                elif change.result is not None:
                    data = change.result
            result = {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": tool_result(data)}
        messages.append(result)
    # 用户在待确认时撤销已完成修改，备注须位于本次原生 tool_result 组之后。
    messages.extend(message_dict(row) for row in group[1:] if not row.tool_call_id)
    return messages


def _fit_group(items: list[dict], budget: int) -> list[dict]:
    """只裁内容，不裁原生工具参数或签名；完整协议本身放不下时明确报错。"""
    fitted = deepcopy(items)
    for item in fitted:
        item["content"] = "" if item["role"] != "tool" else tool_result({"warning": TRUNCATED})
    overhead = len(json.dumps(fitted, ensure_ascii=False))
    if overhead > budget:
        raise BadRequestError("工具调用上下文超过模型预算，请换用更大上下文的模型或开启新会话")
    allowance = max(0, (budget - overhead - 40 * len(items)) // max(1, len(items)))
    for item, original in zip(fitted, items, strict=True):
        content = original.get("content", "")[:allowance]
        item["content"] = tool_result({"excerpt": content, "warning": TRUNCATED}) if item["role"] == "tool" else content
    # 转义字符会增大序列化开销，必要时使用最小合法工具结果。
    if len(json.dumps(fitted, ensure_ascii=False)) > budget:
        for item in fitted:
            item["content"] = "" if item["role"] != "tool" else tool_result({"warning": TRUNCATED})
    return fitted


async def build_messages(
    db: AsyncSession,
    conversation: AgentConversation,
    project: Project,
    cfg: AIConfig,
    provider: LLMProvider,
    page_context: dict | None = None,
    task_id: int | None = None,
) -> tuple[list[dict], dict]:
    from app.services import generation_log_service

    budget = int((cfg.context_window_k or 32) * 1000 * 0.6)
    rows = list(
        await db.scalars(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.id > (conversation.summarized_until or 0),
            )
            .order_by(AgentMessage.id)
        )
    )
    latest_user = await db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.role == "user",
        )
        .order_by(AgentMessage.id.desc())
        .limit(1)
    )
    changes = {
        (c.turn_task_id, c.tool_call_id): c
        for c in await db.scalars(select(AgentChange).where(AgentChange.conversation_id == conversation.id))
    }
    groups = _groups(rows)
    size = sum(len(json.dumps(message_dict(r), ensure_ascii=False)) for r in rows) + len(conversation.summary)
    if size > budget and len(groups) > 1:
        split = max(1, len(groups) // 2)
        older = [r for group in groups[:split] for r in group]
        override = await prompt_service.get_override(db, conversation.user_id, "agent.summarize")
        summary_messages = render(
            "agent.summarize",
            override,
            {
                "summary": untrusted_text(conversation.summary),
                "history": untrusted_text(
                    json.dumps(
                        [
                            {
                                **message_dict(r),
                                "tool_calls": [
                                    {k: v for k, v in call.items() if k != "provider_metadata"}
                                    for call in (r.tool_calls or [])
                                ],
                            }
                            for r in older
                        ],
                        ensure_ascii=False,
                    )[: max(100, budget - len(conversation.summary) - 200)]
                ),
            },
        )
        pieces = []
        started = time.monotonic()
        status, error = "success", ""
        try:
            async with aclosing(
                provider.generate_stream(summary_messages, cfg.model, max_tokens=min(2000, budget // 4))
            ) as stream:
                async for piece in stream:
                    pieces.append(piece)
            conversation.summary = "".join(pieces)[: max(100, budget // 4)]
            conversation.summarized_until = older[-1].id
        except asyncio.CancelledError:
            status, error = "cancelled", "摘要已取消"
            raise
        except Exception as exc:
            status, error = "error", str(exc)[:500]
            raise
        finally:
            await generation_log_service.write_log(
                db,
                project_id=project.id,
                chapter_id=None,
                task_id=task_id,
                kind="agent.turn",
                ai_config_id=cfg.id,
                provider=cfg.provider,
                model=cfg.model,
                params={
                    "conversation_id": conversation.id,
                    "stage": "summary",
                    "prompt": prompt_meta("agent.summarize", override),
                },
                messages=summary_messages,
                status=status,
                error=error,
                output_chars=len("".join(pieces)),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await db.commit()
        groups = groups[split:]
    # 最新作者消息始终保留；工具消息只能和对应的 assistant 作为一组保留。
    summary_message = (
        {"role": "user", "content": "较早对话的摘要：\n" + untrusted_text(conversation.summary)}
        if conversation.summary
        else None
    )
    latest = message_dict(latest_user) if latest_user is not None else None
    selected = []
    remaining = budget - (len(json.dumps(summary_message, ensure_ascii=False)) if summary_message else 0)
    if latest:
        if len(json.dumps(latest, ensure_ascii=False)) > remaining // 2:
            latest["content"] = latest["content"][: max(100, remaining // 2 - 100)] + "\n（消息按上下文预算截断）"
        remaining -= len(json.dumps(latest, ensure_ascii=False))
    for group in reversed(groups):
        if latest_user is not None and any(r.id == latest_user.id for r in group):
            continue
        items = _group_messages(group, changes)
        cost = len(json.dumps(items, ensure_ascii=False))
        if cost > remaining:
            if selected:
                break
            items = _fit_group(items, remaining)
            cost = len(json.dumps(items, ensure_ascii=False))
        selected.insert(0, (group[0].id, items))
        remaining -= cost
    if latest:
        selected.append((latest_user.id, [latest]))
    selected_messages = [item for _, group in sorted(selected, key=lambda entry: entry[0]) for item in group]
    override = await prompt_service.get_override(db, conversation.user_id, "agent.system")
    info = format_project_info(await project_service.project_info(db, project))
    safe_page = await validate_page_context(db, project.id, page_context)
    system = render(
        "agent.system",
        override,
        {
            "safety_rules": SAFETY_RULES,
            "project_info": untrusted_text(info),
            "page_hint": untrusted_text(json.dumps(safe_page, ensure_ascii=False)),
        },
    )
    # 兼容旧的损坏覆盖：通用模板兜底把缺失锁定段补到 user，此处提升回 system 权限。
    if not any(m["role"] == "system" and SAFETY_RULES in m["content"] for m in system):
        system = [{"role": "system", "content": SAFETY_RULES}] + [
            {**m, "content": m["content"].replace(SAFETY_RULES, "")} for m in system
        ]
    messages = list(system)
    if summary_message:
        messages.append(summary_message)
    messages.extend(selected_messages)
    return messages, prompt_meta("agent.system", override)
