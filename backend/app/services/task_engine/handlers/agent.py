"""agent.turn：同会话互斥，确认后续跑，重试复用已持久化的工具结果。"""

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.models import AgentChange, AgentConversation, AgentMessage, Task
from app.schemas.agent import SendMessage
from app.services import ai_config_service, skill_service
from app.services.agent import context, history, service
from app.services.agent.runner import AgentJob, run_agent
from app.services.ai_dispatch import guard_provider
from app.services.task_engine.registry import TaskSpec, register


class AgentHandler:
    kinds = ("agent.turn",)

    async def validate_and_claim(self, db, ctx, *, user_id, kind, params):
        pid, cid = int(params["project_id"]), int(params["conversation_id"])
        conversation = await service.owned_conversation(db, user_id, pid, cid, lock=True)
        await db.execute(select(AgentConversation.id).where(AgentConversation.id == cid).with_for_update())
        stamp = await history.require_open_history(db, conversation, adopt_empty=True)
        if await service.active_turn(db, cid):
            raise ConflictError("当前会话已有进行中或待确认的轮次")
        req = SendMessage.model_validate(params)
        cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
        await ai_config_service.build_provider(
            cfg,
            ctx.cipher,
            ctx.factory,
            ctx.settings.ai_request_timeout,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        page = await context.validate_page_context(db, pid, req.page_context.model_dump() if req.page_context else None)
        # 强制启用的 Skill：显式 skill_id（不可见 → 404）或消息开头的 /触发词；快照随消息保存，本轮续跑沿用
        skill = (
            await skill_service.get_visible_skill(db, user_id, req.skill_id)
            if req.skill_id is not None
            else await skill_service.match_trigger(db, user_id, req.content)
        )
        message = AgentMessage(
            conversation_id=cid,
            role="user",
            content=req.content,
            page_context=page,
            skill_id=skill.id if skill else None,
            skill_snapshot=await skill_service.snapshot_of(db, skill) if skill else None,
        )
        db.add(message)
        conversation.last_message_at = datetime.now(UTC)
        if not conversation.title:
            conversation.title = req.content[:40]
        await db.flush()
        return TaskSpec(
            project_id=pid,
            chapter_id=None,
            resource_key=f"agent:{cid}",
            title=f"智能体：{req.content[:20]}",
            input={"request": params, "ai_config_id": cfg.id, "message_id": message.id, "history_epoch": stamp.epoch},
        )

    async def validate_retry(self, db, ctx, *, user_id, task):
        if await db.scalar(select(Task.id).where(Task.retry_of_id == task.id).limit(1)):
            raise ConflictError("此轮已有重试任务，请使用最新任务继续")
        req = task.input["request"]
        cid = int(req["conversation_id"])
        conversation = await service.owned_conversation(db, user_id, task.project_id, cid, lock=True)
        await history.require_open_history(db, conversation, expected_epoch=task.input.get("history_epoch"))
        if await service.active_turn(db, cid):
            raise ConflictError("当前会话已有进行中或待确认的轮次")
        cfg = await ai_config_service.resolve_config(db, user_id, task.input.get("ai_config_id"))
        await ai_config_service.build_provider(
            cfg,
            ctx.cipher,
            ctx.factory,
            ctx.settings.ai_request_timeout,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        message = await db.get(AgentMessage, task.input["message_id"])
        # 上一次重试可能在排队中已被取消，尚未 build_job；以已持久化消息的实际归属为准。
        source_id = message.turn_task_id or task.input.get("retry_source_id") or task.id
        return TaskSpec(
            project_id=task.project_id,
            chapter_id=None,
            resource_key=f"agent:{cid}",
            title=task.title,
            input={**task.input, "ai_config_id": cfg.id, "retry_source_id": source_id},
        )

    async def revalidate_resume(self, db, ctx, task):
        req = task.input["request"]
        conversation = await service.owned_conversation(db, task.user_id, task.project_id, req["conversation_id"])
        await history.require_open_history(db, conversation, expected_epoch=task.input.get("history_epoch"))
        pending = await db.scalar(
            select(AgentChange.id)
            .where(AgentChange.turn_task_id == task.id, AgentChange.status.in_(("proposed", "executing")))
            .limit(1)
        )
        if pending is not None:
            raise ConflictError("请先确认或拒绝本轮全部修改")

    def finish_result(self, task):
        raise ConflictError("请确认或拒绝全部修改，或取消本轮智能体任务")

    async def build_job(self, ctx, task):
        async with ctx.sessionmaker() as db:
            req = task.input["request"]
            conversation = await service.owned_conversation(
                db, task.user_id, task.project_id, req["conversation_id"], lock=True,
            )
            stamp = await history.require_open_history(db, conversation, expected_epoch=task.input.get("history_epoch"))
            cfg = await ai_config_service.resolve_config(db, task.user_id, task.input.get("ai_config_id"))
            provider = await ai_config_service.build_provider(
                cfg,
                ctx.cipher,
                ctx.factory,
                ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
            )
            # 重试仍属同一逻辑轮次：沿用消息和提案，不创建第二条 user 消息。
            old_id = task.input.get("retry_source_id")
            if old_id:
                for model in (AgentMessage, AgentChange):
                    await db.execute(
                        update(model)
                        .where(
                            model.conversation_id == conversation.id,
                            model.turn_task_id == old_id,
                        )
                        .values(turn_task_id=task.id)
                    )
            await db.execute(
                update(AgentMessage)
                .where(
                    AgentMessage.id == task.input["message_id"],
                    AgentMessage.conversation_id == conversation.id,
                )
                .values(turn_task_id=task.id)
            )
            await db.commit()
            return AgentJob(
                task.id, task.user_id, task.project_id, conversation.id, task.input["message_id"], cfg,
                guard_provider(provider, stamp, ctx.sessionmaker), stamp,
            )

    def run(self, job, ctx):
        return run_agent(job, ctx)

    def progress(self, task, text):
        return min(90, 5 + len(text) // 100)

    async def on_abort(self, ctx, task):
        # 每个工具效果与状态同事务；取消只停止后续步骤，已执行效果保留且可撤销。
        return None


register(AgentHandler())
