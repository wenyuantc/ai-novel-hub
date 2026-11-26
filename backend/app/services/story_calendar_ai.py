"""生成临时历法候选。保存、越界和顺序影响确认仍由普通历法编辑接口处理。"""

import asyncio
import json
from collections import defaultdict
from contextlib import aclosing
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.json_extract import extract_json
from app.ai.prompts import registry
from app.ai.prompts.common import JSON_ONLY_RULE
from app.ai.providers import ProviderFactory
from app.ai.providers.base import AIProviderError, ThinkingLevel
from app.core.errors import BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import Project
from app.schemas.story_timeline import CalendarGenerateRequest, CalendarOut, MonthId, Name, Positive, StoryMonth
from app.services import (
    ai_config_service,
    ai_dispatch,
    collaboration_ai_guard,
    project_access,
    project_ai_config,
    prompt_service,
    story_timeline,
)

PROMPT_KEY = "story_timeline.calendar"


class _ProposedMonth(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    existing_id: MonthId | None
    name: Name
    days: Positive


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    era_name: Name
    months: list[_ProposedMonth] = Field(min_length=1, max_length=128)


def parse_draft(raw: str, current: CalendarOut) -> CalendarOut:
    try:
        proposed = _Proposal.model_validate(extract_json(raw))
        old_ids = {m.id for m in current.months}
        by_name = defaultdict(list)
        for month in current.months:
            by_name[month.name].append(month.id)
        used = set()
        months = []
        for month in proposed.months:
            key = month.existing_id
            if key is None and month.name in by_name:
                # 模型遗漏身份时，仅可按未歧义且完整匹配的旧名称恢复。
                namesakes = by_name[month.name]
                if len(namesakes) != 1:
                    raise ValueError
                key = namesakes[0]
            if key is None:
                key = f"m{uuid4().hex}"
            elif key not in old_ids or key in used:
                raise ValueError
            used.add(key)
            months.append(StoryMonth(id=key, name=month.name, days=month.days))
        return CalendarOut(revision=current.revision, era_name=proposed.era_name, months=months)
    except (ValueError, RecursionError):
        pass
    raise BadRequestError("模型返回的历法格式、日期范围或月份标识无效，请重试或更换文本模型")


def _background(project):
    return {"title": project.title, "genre": project.genre, "premise": project.premise[:3000]}


async def generate(
    db: AsyncSession, actor: int, pid: int, req: CalendarGenerateRequest, cipher: SecretCipher,
    factory: ProviderFactory, *, ai_timeout: float, allow_private: bool, context_budget: int,
) -> CalendarOut:
    project = await project_access.require_project(db, actor, pid, "ai.use")
    background = _background(project)
    current = await story_timeline.get_calendar(db, pid)
    if current.revision != req.expected_revision:
        raise ConflictError("故事历法已变化，请刷新后重新生成", extra={"code": "story_timeline_changed"})
    override = await prompt_service.get_override(db, actor, PROMPT_KEY)
    messages = registry.render(PROMPT_KEY, override, {
        "calendar_context": json.dumps({"project": background, "calendar": current.model_dump(),
                                        "instruction": req.instruction}, ensure_ascii=False, separators=(",", ":")),
        "json_rule": JSON_ONLY_RULE, "output_format": registry.STORY_CALENDAR_FORMAT,
    })
    binding = await project_ai_config.resolve_project_model(
        db, actor, pid, "text", cipher=cipher, config_id=req.ai_config_id, allow_private=allow_private,
    )
    stamp = await ai_dispatch.read_visibility_stamp(db, pid)

    async def verify_source(session: AsyncSession) -> None:
        row = (await session.execute(select(Project.title, Project.genre, Project.premise)
                                     .where(Project.id == pid))).one_or_none()
        latest = await story_timeline.get_calendar(session, pid)
        if row is None or _background(row) != background or latest != current:
            raise ConflictError("作品信息或历法已变化，请刷新后重新生成", extra={"code": "story_timeline_changed"})

    guard = collaboration_ai_guard.for_http(
        db, actor, binding, stamp, cipher=cipher, allow_private=allow_private, http_source_verify=verify_source,
    )
    maker = ai_dispatch.visibility_sessionmaker(db)
    await db.rollback()
    bound = await guard.prepare_provider(maker, factory, request_timeout=ai_timeout,
                                         usage_kind="story_timeline.calendar.generate")
    if sum(len(m["content"]) for m in messages) > ai_config_service.context_char_budget(bound, context_budget):
        raise BadRequestError("历法信息超过模型上下文限制，请缩短要求或选择上下文更大的文本模型")
    try:
        async with asyncio.timeout(ai_timeout):
            stream = bound.provider.generate_stream(
                messages, bound.model, max_tokens=ai_config_service.max_output_tokens(bound),
                thinking=cast(ThinkingLevel, bound.thinking_level),
            )
            chunks, length = [], 0
            async with aclosing(stream):
                async for chunk in stream:
                    length += len(chunk)
                    if length > 100_000:
                        raise BadRequestError("模型返回的历法过长，请重新生成")
                    chunks.append(chunk)
    except ai_dispatch.VisibilityPolicyError:
        raise
    except TimeoutError:
        raise BadRequestError("生成历法超时，请稍后重试") from None
    except AIProviderError:
        raise BadRequestError("历法生成失败，请检查文本模型配置后重试") from None

    await project_access.require_project(
        db, actor, pid, "ai.use", for_update=True, grant_identity=binding.grant_identity,
    )
    await project_ai_config.revalidate_project_model(db, binding, cipher=cipher, allow_private=allow_private)
    await ai_dispatch.require_visibility_stamp(db, stamp)
    await verify_source(db)
    return parse_draft("".join(chunks), current)
