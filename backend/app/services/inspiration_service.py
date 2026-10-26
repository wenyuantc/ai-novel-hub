"""灵感模式：服务端持久化的立项会话（固定步骤的选项流程）、生成选项（同步 SSE）、转为项目 / 应用到已有项目。

- 会话状态整体存在 inspiration_sessions.state（JSON），读写都经 SessionState 校验。
- 步骤顺序 STEPS：title → synopsis → theme → tags → selling_points → audience。某一步原本已定、值发生变化
  （改值 / 跳过 / 清除）时，排在它后面且已定的步骤标记 stale（保留原值）；修改 idea 时所有已定步骤标记 stale。
- 生成选项照风格提取的模式：同步 SSE、不进任务引擎；解析失败或有效选项不足时在同一请求内重试（最多 2 次，
  先发 retry 事件）；成功后才把这一轮写进会话，客户端中途断开不落库。
- 不写生成记录：generation_logs.project_id 不能为空，而立项时会话可能还没有项目。
"""

import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import build_inspiration_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import InspirationSession, Project
from app.schemas.inspiration import (
    APPLY_FIELDS,
    APPLY_LABELS,
    IDEA_MAX,
    MAX_NOTES,
    MAX_ROUNDS,
    MAX_SESSIONS,
    STEP_LABELS,
    STEPS,
    ApplyPreviewItem,
    ApplyRequest,
    ConvertRequest,
    GenerateRequest,
    NoteCreate,
    Round,
    Scale,
    SessionBrief,
    SessionCreate,
    SessionOut,
    SessionState,
    SessionUpdate,
    StepUpdate,
)
from app.schemas.project import (
    AUDIENCE_MAX,
    SELLING_POINTS_MAX,
    SYNOPSIS_MAX,
    THEME_MAX,
    ProjectOut,
    WizardParams,
    WizardProgress,
    normalize_tags,
)
from app.services import ai_config_service, project_service, prompt_service

logger = logging.getLogger(__name__)

# 定下某一步时的长度上限（与 projects 列一致）
VALUE_LIMITS: dict[str, int] = {
    "title": 200,
    "synopsis": SYNOPSIS_MAX,
    "theme": THEME_MAX,
    "selling_points": SELLING_POINTS_MAX,
    "audience": AUDIENCE_MAX,
}
# AI 选项的长度上限（超出即丢弃）。简介在提示词里要求 150–400 字，这里留 100 字余量，避免因略超而反复重试
OPTION_LIMITS: dict[str, int] = {
    "title": 30,
    "synopsis": 500,
    "theme": 60,
    "tags": 10,
    "selling_points": 200,
    "audience": 60,
}
# 选项数量：(下限, 上限)；少于下限视为不合格（触发重试），多于上限截断
OPTION_COUNTS: dict[str, tuple[int, int]] = {step: (4, 6) for step in STEPS}
OPTION_COUNTS["tags"] = (6, 10)
MAX_RETRIES = 2
SESSION_TITLE_LEN = 30
# 进程内的生成锁（单进程部署）；条目超过 TTL 视为遗留（例如响应体从未开始迭代、finally 没有执行）
LOCK_TTL_SECONDS = 600.0
_generating: dict[tuple[int, str], float] = {}


# ---------------------------------------------------------------- 状态读写


def load_state(session: InspirationSession) -> SessionState:
    """读出 state 并校验；库里的数据不合法（手工改库 / 限制收紧后的旧数据）→ 409，不抛 500。"""
    try:
        return SessionState.model_validate(session.state or {})
    except ValidationError as exc:
        logger.warning("灵感会话 %s 的 state 不合法：%s", session.id, exc)
        raise ConflictError("灵感会话数据已损坏，无法继续，请删除后重新创建") from exc


def _session_title(state: SessionState) -> str:
    title = state.steps["title"]
    if title.status == "decided" and isinstance(title.value, str) and title.value.strip():
        return title.value.strip()[:200]
    return state.idea.strip()[:SESSION_TITLE_LEN] or "未命名灵感"


def _store(session: InspirationSession, state: SessionState) -> None:
    # JSON 列需整体赋新值才会被识别为已修改
    session.state = state.model_dump(mode="json")
    session.title = _session_title(state)


def to_out(session: InspirationSession) -> SessionOut:
    return SessionOut(
        id=session.id,
        title=session.title,
        status=session.status,  # type: ignore[arg-type]
        project_id=session.project_id,
        converted_project_id=session.converted_project_id,
        state=load_state(session),
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def to_brief(session: InspirationSession) -> SessionBrief:
    # 列表里单个会话数据损坏不影响其他会话（仍可在列表中删除它）
    try:
        state = load_state(session)
    except ConflictError:
        state = SessionState()
    steps = state.steps.values()
    return SessionBrief(
        id=session.id,
        title=session.title,
        status=session.status,  # type: ignore[arg-type]
        project_id=session.project_id,
        converted_project_id=session.converted_project_id,
        done_steps=sum(1 for s in steps if s.status != "pending"),
        stale_steps=sum(1 for s in steps if s.stale),
        current=state.current,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


async def _commit(db: AsyncSession, session: InspirationSession) -> SessionOut:
    await db.commit()
    await db.refresh(session)
    return to_out(session)


# ---------------------------------------------------------------- 会话增删查


async def get_owned_session(db: AsyncSession, user_id: int, session_id: int) -> InspirationSession:
    """他人的会话与不存在统一 404。"""
    session = await db.scalar(
        select(InspirationSession).where(InspirationSession.id == session_id, InspirationSession.user_id == user_id)
    )
    if session is None:
        raise NotFoundError("灵感会话不存在")
    return session


async def list_sessions(db: AsyncSession, user_id: int) -> list[SessionBrief]:
    rows = (
        await db.scalars(
            select(InspirationSession)
            .where(InspirationSession.user_id == user_id)
            .order_by(InspirationSession.updated_at.desc(), InspirationSession.id.desc())
        )
    ).all()
    return [to_brief(s) for s in rows]


async def get_session(db: AsyncSession, user_id: int, session_id: int) -> SessionOut:
    return to_out(await get_owned_session(db, user_id, session_id))


async def _ensure_capacity(db: AsyncSession, user_id: int) -> None:
    """每用户最多 MAX_SESSIONS 个：满了先删最旧的已转换会话，没有可删的 → 409。"""
    count = await db.scalar(select(func.count(InspirationSession.id)).where(InspirationSession.user_id == user_id))
    if int(count or 0) < MAX_SESSIONS:
        return
    oldest = await db.scalar(
        select(InspirationSession)
        .where(InspirationSession.user_id == user_id, InspirationSession.status == "converted")
        .order_by(InspirationSession.updated_at.asc(), InspirationSession.id.asc())
        .limit(1)
    )
    if oldest is None:
        raise ConflictError(f"灵感会话已达上限（{MAX_SESSIONS} 个），请先删除一些会话")
    await db.delete(oldest)
    await db.flush()


async def create_session(db: AsyncSession, user_id: int, data: SessionCreate) -> SessionOut:
    idea = data.idea.strip()
    if not idea:
        raise BadRequestError("请先写下你的想法")
    await _ensure_capacity(db, user_id)
    state = SessionState(idea=idea, current="title")
    session = InspirationSession(user_id=user_id, status="active")
    _store(session, state)
    db.add(session)
    return await _commit(db, session)


async def delete_session(db: AsyncSession, user_id: int, session_id: int) -> None:
    session = await get_owned_session(db, user_id, session_id)
    await db.delete(session)
    await db.commit()


async def update_session(db: AsyncSession, user_id: int, session_id: int, data: SessionUpdate) -> SessionOut:
    session = await get_owned_session(db, user_id, session_id)
    state = load_state(session)
    fields = data.model_fields_set
    if data.idea is not None:
        idea = data.idea.strip()
        if not idea:
            raise BadRequestError("想法不能为空")
        if idea != state.idea:
            state.idea = idea
            for step in state.steps.values():
                if step.status == "decided":
                    step.stale = True
    if data.current is not None:
        state.current = data.current
    if "scale" in fields:
        state.scale = data.scale
    _store(session, state)
    return await _commit(db, session)


# ---------------------------------------------------------------- 步骤决定与 stale


def _effective(state: SessionState, step: str) -> Any:
    s = state.steps[step]  # type: ignore[index]
    return s.value if s.status == "decided" else None


def _clean_value(step: str, value: Any) -> str | list[str]:
    label = STEP_LABELS[step]
    if step == "tags":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise BadRequestError("类型标签应为列表")
        try:
            tags = normalize_tags(value)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        if not tags:
            raise BadRequestError("请至少选择一个类型标签")
        return tags
    if not isinstance(value, str):
        raise BadRequestError(f"{label}应为文本")
    text = value.strip()
    if not text:
        raise BadRequestError(f"{label}不能为空")
    limit = VALUE_LIMITS[step]
    if len(text) > limit:
        raise BadRequestError(f"{label}最多 {limit} 字")
    return text


def apply_step(state: SessionState, step: str, data: StepUpdate) -> None:
    """纯函数式地修改 state（原地）。原本已定的步骤值发生变化时，其后已定的步骤标记 stale。"""
    entry = state.steps[step]  # type: ignore[index]
    before = _effective(state, step)
    was_decided = entry.status == "decided"
    if data.action == "decide":
        entry.value = _clean_value(step, data.value)
        entry.status = "decided"
        entry.source = data.source
        entry.stale = False
    elif data.action == "skip":
        entry.value, entry.status, entry.source, entry.stale = None, "skipped", None, False
    elif data.action == "clear":
        entry.value, entry.status, entry.source, entry.stale = None, "pending", None, False
    else:  # keep：保持不变，只清除 stale
        entry.stale = False
        return
    if was_decided and _effective(state, step) != before:
        for later in STEPS[STEPS.index(step) + 1 :]:  # type: ignore[arg-type]
            if state.steps[later].status == "decided":
                state.steps[later].stale = True


async def update_step(db: AsyncSession, user_id: int, session_id: int, step: str, data: StepUpdate) -> SessionOut:
    session = await get_owned_session(db, user_id, session_id)
    state = load_state(session)
    apply_step(state, step, data)
    _store(session, state)
    return await _commit(db, session)


# ---------------------------------------------------------------- 补充想法


async def add_note(db: AsyncSession, user_id: int, session_id: int, data: NoteCreate) -> SessionOut:
    session = await get_owned_session(db, user_id, session_id)
    state = load_state(session)
    text = data.text.strip()
    if not text:
        raise BadRequestError("补充想法不能为空")
    if len(state.notes) >= MAX_NOTES:
        raise BadRequestError(f"补充想法最多 {MAX_NOTES} 条")
    state.notes.append(text)
    _store(session, state)
    return await _commit(db, session)


async def delete_note(db: AsyncSession, user_id: int, session_id: int, index: int) -> SessionOut:
    session = await get_owned_session(db, user_id, session_id)
    state = load_state(session)
    if not 0 <= index < len(state.notes):
        raise NotFoundError("补充想法不存在")
    state.notes.pop(index)
    _store(session, state)
    return await _commit(db, session)


# ---------------------------------------------------------------- 规模建议


def suggest_scale(target_words: int) -> Scale:
    """每章约 3000 字（至少 10 章）；每卷约 30 章，卷数 1–20、每卷章数 5–100；角色数 = 5 + 卷数 × 2（≤ 30）。"""
    chapters = max(10, round(target_words / 3000))
    volumes = min(20, max(1, math.ceil(chapters / 30)))
    per_volume = min(100, max(5, math.ceil(chapters / volumes)))
    return Scale(
        target_words=target_words,
        volume_count=volumes,
        chapters_per_volume=per_volume,
        character_count=min(30, 5 + volumes * 2),
    )


# ---------------------------------------------------------------- 生成选项（同步 SSE）


def normalize_options(step: str, raw: Any) -> list[str]:
    """{"options": [...]}（或直接是数组）→ 去空白、去重，丢掉空的和超长的，多于上限截断。

    结构不对 → JSONExtractError。
    """
    items = raw.get("options") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise JSONExtractError("输出结构不正确：缺少 options 数组")
    limit = OPTION_LIMITS[step]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = next((item[k] for k in ("text", "value", "content", "title", "name") if k in item), "")
        if not isinstance(item, str | int | float) or isinstance(item, bool):
            continue
        text = str(item).strip()
        if step == "title":
            text = text.strip("《》").strip()
        if text and len(text) <= limit and text not in out:
            out.append(text)
    return out[: OPTION_COUNTS[step][1]]


def _format_value(step: str, value: Any) -> str:
    if isinstance(value, list):
        return "、".join(value)
    return str(value)


def build_decided(state: SessionState, step: str) -> str:
    """已定内容：项目的题材（从项目发起时）+ 除本步外所有已定的步骤（按步骤顺序）。"""
    lines: list[str] = []
    if state.genre.strip():
        lines.append(f"题材：{state.genre.strip()}")
    for key in STEPS:
        entry = state.steps[key]
        if key == step or entry.status != "decided" or not entry.value:
            continue
        lines.append(f"{STEP_LABELS[key]}：{_format_value(key, entry.value)}")
    return "\n".join(lines)


def build_history(state: SessionState, step: str) -> str:
    """本步之前各轮的选项，以及每一轮对应的反馈。"""
    blocks: list[str] = []
    for i, rnd in enumerate(state.steps[step].rounds, start=1):  # type: ignore[index]
        head = f"第 {i} 轮" + (f"（作者反馈：{rnd.feedback}）" if rnd.feedback else "") + "："
        blocks.append("\n".join([head, *(f"{n}. {opt}" for n, opt in enumerate(rnd.options, start=1))]))
    return "\n\n".join(blocks)


@dataclass
class InspirationJob:
    user_id: int
    session_id: int
    step: str
    feedback: str
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    lock_key: tuple[int, str] | None = None


def _acquire(key: tuple[int, str]) -> None:
    now = time.monotonic()
    started = _generating.get(key)
    if started is not None and now - started < LOCK_TTL_SECONDS:
        raise ConflictError("该步骤正在生成选项，请稍候再试")
    _generating[key] = now


def _release(key: tuple[int, str] | None) -> None:
    if key is not None:
        _generating.pop(key, None)


async def prepare_generate(
    db: AsyncSession,
    *,
    user_id: int,
    session_id: int,
    step: str,
    req: GenerateRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> InspirationJob:
    session = await get_owned_session(db, user_id, session_id)
    state = load_state(session)
    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    override = await prompt_service.get_override(db, user_id, f"inspiration.{step}")
    messages = build_inspiration_messages(
        step,
        idea=state.idea,
        notes=state.notes,
        decided=build_decided(state, step),
        history=build_history(state, step),
        feedback=req.feedback,
        override=override,
    )
    key = (session.id, step)
    # 所有校验都通过后才占锁；锁由 run_generate 的 finally 释放
    _acquire(key)
    return InspirationJob(
        user_id=user_id,
        session_id=session.id,
        step=step,
        feedback=req.feedback.strip(),
        provider=provider,
        model=cfg.model,
        messages=messages,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        lock_key=key,
    )


def _parse(step: str, text: str) -> tuple[list[str] | None, str]:
    """返回 (选项, 不合格原因)。"""
    if not text.strip():
        return None, "模型未返回任何内容"
    try:
        options = normalize_options(step, extract_json(text))
    except JSONExtractError as exc:
        return None, str(exc)
    minimum = OPTION_COUNTS[step][0]
    if len(options) < minimum:
        return None, f"有效选项只有 {len(options)} 个（至少需要 {minimum} 个）"
    return options, ""


async def _save_round(
    sessionmaker: async_sessionmaker[AsyncSession], job: InspirationJob, options: list[str]
) -> tuple[Round, SessionOut] | None:
    async with sessionmaker() as db:
        session = await db.scalar(
            select(InspirationSession).where(
                InspirationSession.id == job.session_id, InspirationSession.user_id == job.user_id
            )
        )
        if session is None:
            return None
        state = load_state(session)
        rnd = Round(options=options, feedback=job.feedback, created_at=datetime.now(UTC).isoformat())
        rounds = [*state.steps[job.step].rounds, rnd]  # type: ignore[index]
        state.steps[job.step].rounds = rounds[-MAX_ROUNDS:]  # type: ignore[index]
        _store(session, state)
        return rnd, await _commit(db, session)


async def run_generate(
    job: InspirationJob, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[str, dict]]:
    """delta… →（不合格时 retry → delta…，最多 2 次）→ done {round, session} / error。只在成功时写库。"""
    try:
        reason = ""
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                yield "retry", {"attempt": attempt, "max": MAX_RETRIES, "reason": reason}
            pieces: list[str] = []
            stream_iter = job.provider.generate_stream(
                job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
            )
            async with aclosing(stream_iter) as stream:
                async for piece in stream:
                    pieces.append(piece)
                    yield "delta", {"text": piece}
            options, reason = _parse(job.step, "".join(pieces))
            if options is None:
                continue
            saved = await _save_round(sessionmaker, job, options)
            if saved is None:
                yield "error", {"detail": "灵感会话已被删除，选项未保存"}
                return
            rnd, session = saved
            yield "done", {"round": rnd.model_dump(mode="json"), "session": session.model_dump(mode="json")}
            return
        yield "error", {"detail": f"AI 输出不合法：{reason}，已自动重试 {MAX_RETRIES} 次，请稍后重试"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except AppError as exc:
        yield "error", {"detail": exc.detail}
    except Exception:  # noqa: BLE001
        logger.exception("灵感模式生成选项失败")
        yield "error", {"detail": "生成失败：服务器内部错误"}
    finally:
        _release(job.lock_key)


# ---------------------------------------------------------------- 转为项目 / 应用到已有项目 / 从项目发起


def _decided(state: SessionState, step: str) -> Any:
    value = _effective(state, step)
    return value if value else None


async def convert(db: AsyncSession, user_id: int, session_id: int, data: ConvertRequest) -> ProjectOut:
    session = await get_owned_session(db, user_id, session_id)
    state = load_state(session)
    if session.status == "converted" and session.converted_project_id is not None:
        raise ConflictError("该会话已转为项目，可以改用「应用到已有项目」")
    title = (data.title_override or "").strip() or (_decided(state, "title") or "")
    if not title:
        raise BadRequestError("请先定下书名")
    scale = data.scale or state.scale
    tags = list(_decided(state, "tags") or [])
    progress = WizardProgress()
    if scale is not None:
        progress.params = WizardParams(
            volume_count=scale.volume_count,
            chapters_per_volume=scale.chapters_per_volume,
            character_count=scale.character_count,
        )
    project = Project(
        user_id=user_id,
        title=title[:200],
        genre=(tags[0] if tags else state.genre.strip())[:100],
        premise=state.idea.strip()[:5000],
        status="planning",
        synopsis=(_decided(state, "synopsis") or "")[:SYNOPSIS_MAX],
        theme=(_decided(state, "theme") or "")[:THEME_MAX],
        tags=tags,
        selling_points=(_decided(state, "selling_points") or "")[:SELLING_POINTS_MAX],
        audience=(_decided(state, "audience") or "")[:AUDIENCE_MAX],
        target_words=scale.target_words if scale is not None else None,
        wizard_progress=progress.model_dump(),
    )
    db.add(project)
    await db.flush()
    if data.scale is not None:
        state.scale = data.scale
    session.status = "converted"
    session.converted_project_id = project.id
    _store(session, state)
    await db.commit()
    await db.refresh(project)
    return await project_service.project_out(db, project)


def _proposals(state: SessionState) -> dict[str, Any]:
    proposed: dict[str, Any] = {step: _decided(state, step) for step in STEPS}
    proposed["premise"] = state.idea.strip() or None
    proposed["target_words"] = state.scale.target_words if state.scale is not None else None
    return {k: v for k, v in proposed.items() if v}


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or (isinstance(value, str) and not value.strip())


async def apply_preview(db: AsyncSession, user_id: int, session_id: int, project_id: int) -> list[ApplyPreviewItem]:
    session = await get_owned_session(db, user_id, session_id)
    project = await project_service.get_owned_project(db, user_id, project_id)
    proposed = _proposals(load_state(session))
    items: list[ApplyPreviewItem] = []
    for field in APPLY_FIELDS:
        if field not in proposed:
            continue
        current = getattr(project, field)
        items.append(
            ApplyPreviewItem(
                field=field,
                label=APPLY_LABELS[field],
                current=current,
                proposed=proposed[field],
                default_checked=_is_empty(current) and proposed[field] != current,
            )
        )
    return items


async def apply(db: AsyncSession, user_id: int, session_id: int, data: ApplyRequest) -> ProjectOut:
    """只写勾选的字段；会话状态不变，可以反复应用。"""
    session = await get_owned_session(db, user_id, session_id)
    project = await project_service.get_owned_project(db, user_id, data.project_id)
    proposed = _proposals(load_state(session))
    for field in dict.fromkeys(data.fields):
        if field not in proposed:
            raise BadRequestError(f"「{APPLY_LABELS[field]}」没有可应用的内容")
    for field in dict.fromkeys(data.fields):
        value = proposed[field]
        setattr(project, field, list(value) if isinstance(value, list) else value)
    await db.commit()
    await db.refresh(project)
    return await project_service.project_out(db, project)


async def from_project(db: AsyncSession, user_id: int, project_id: int) -> SessionOut:
    """用项目已有字段预填（非空的步骤为已定、source=project）；同一项目已有进行中的会话时直接返回它。"""
    project = await project_service.get_owned_project(db, user_id, project_id)
    existing = await db.scalar(
        select(InspirationSession)
        .where(
            InspirationSession.user_id == user_id,
            InspirationSession.project_id == project.id,
            InspirationSession.status == "active",
        )
        .order_by(InspirationSession.id.desc())
        .limit(1)
    )
    if existing is not None:
        return to_out(existing)
    await _ensure_capacity(db, user_id)
    state = SessionState(idea=(project.premise or "").strip()[:IDEA_MAX], genre=(project.genre or "").strip()[:100])
    for step in STEPS:
        value = getattr(project, step)
        if step == "tags":
            try:
                value = normalize_tags(list(value or []))
            except ValueError:
                value = []
        else:
            value = (value or "").strip()
        if value:
            entry = state.steps[step]
            entry.value, entry.status, entry.source = value, "decided", "project"
    if project.target_words:
        state.scale = suggest_scale(project.target_words)
    state.current = next((s for s in STEPS if state.steps[s].status == "pending"), "scale")
    session = InspirationSession(user_id=user_id, project_id=project.id, status="active")
    _store(session, state)
    db.add(session)
    return await _commit(db, session)
