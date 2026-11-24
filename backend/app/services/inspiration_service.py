"""灵感模式：服务端持久化的立项会话（固定步骤的选项流程）、生成选项（同步 SSE）、转为项目 / 应用到已有项目。

- 会话状态整体存在 inspiration_sessions.state（JSON），读写都经 SessionState 校验。
- 步骤顺序 STEPS：title → synopsis → theme → tags → selling_points → audience。某一步原本已定、值发生变化
  （改值 / 跳过 / 清除）时，排在它后面且已定的步骤标记 stale（保留原值）；修改 idea 时所有已定步骤标记 stale。
- 生成选项照风格提取的模式：同步 SSE、不进任务引擎；解析失败或有效选项不足时在同一请求内重试（最多 2 次，
  先发 retry 事件）；成功后才把这一轮写进会话，客户端中途断开不落库。
- 不写生成记录：generation_logs.project_id 不能为空，而立项时会话可能还没有项目。
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

import anyio
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import build_inspiration_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import InspirationOrigin, InspirationSession, Project
from app.models.inspiration_generation import InspirationGenerationClaim
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
from app.services import ai_config_service, kb_service, project_access, project_service, prompt_service
from app.services.ai_dispatch import is_sqlite_busy, visibility_sessionmaker
from app.services.ai_usage_recording import meter_provider
from app.services.audit_writer import AuditOperation
from app.services.authorization_service import AuthenticatedActor, actor_snapshot, require_authenticated
from app.services.task_engine import workers

if TYPE_CHECKING:
    from app.services.task_engine.request_worker import RequestWorker

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
STATE_WRITE_ATTEMPTS = 5


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
    session_id, user_id = session.id, session.user_id
    if session_id is None:
        await db.flush()
        session_id = session.id
    await db.commit()
    # 共享来源可能在提交后撤销；投影前锁内复核原邀请，不返回旧快照。
    return to_out(await get_owned_session(db, user_id, session_id))


def _serialized_write_failed(exc: DBAPIError) -> bool:
    return (getattr(exc.orig, "sqlstate", None) in ("40001", "40P01")
            or (isinstance(exc, OperationalError) and is_sqlite_busy(exc)))


async def _commit_state_change[T](db: AsyncSession, change: Callable[[], Awaitable[T]]) -> T:
    """只在可确认撤销的冲突下重读和重放。外部调用和提交后的投影由调用方处理。"""
    for attempt in range(STATE_WRITE_ATTEMPTS):
        try:
            result = await change()
            await db.commit()
            return result
        except DBAPIError as exc:
            if not _serialized_write_failed(exc):
                raise
        except StaleDataError:
            pass
        await db.rollback()
        await asyncio.sleep(0.01 * (attempt + 1))
    raise ConflictError("灵感会话正在更新，请刷新后重试")


async def _change_state(
    db: AsyncSession, user_id: int, session_id: int, change: Callable[[SessionState], None],
) -> SessionOut:
    async def write() -> None:
        session = await get_owned_session(db, user_id, session_id)
        state = load_state(session)
        change(state)
        _store(session, state)

    await _commit_state_change(db, write)
    return to_out(await get_owned_session(db, user_id, session_id))


# ---------------------------------------------------------------- 会话增删查


def _uuid4(value: str) -> bool:
    try:
        value_uuid = UUID(value)
        return str(value_uuid) == value and value_uuid.version == 4
    except (AttributeError, TypeError, ValueError):
        return False


def _origin_valid(origin: InspirationOrigin) -> bool:
    return (
        type(origin.project_id) is int and origin.project_id > 0
        and _uuid4(origin.invitation_id) and _uuid4(origin.origin_key)
    )


async def get_owned_session(db: AsyncSession, user_id: int, session_id: int) -> InspirationSession:
    """本人会话的项目来源必须仍是同一轮已接受邀请。"""
    with db.no_autoflush:
        session = await db.scalar(
            select(InspirationSession).where(InspirationSession.id == session_id, InspirationSession.user_id == user_id)
        )
        if session is None:
            raise NotFoundError("灵感会话不存在")
        origin = await db.get(InspirationOrigin, session.id)
        if origin is None:
            if session.project_id is not None:
                project = await db.get(Project, session.project_id)
                # 旧作者会话没有来源行；不能把无来源的跨用户会话当成普通私人会话。
                if project is not None and project.user_id != user_id:
                    raise NotFoundError("灵感会话不存在")
            return session
        if not _origin_valid(origin) or session.project_id != origin.project_id:
            raise NotFoundError("灵感会话不存在")
        original = (origin.project_id, origin.invitation_id, origin.origin_key)
        try:
            _, access, member = await project_access.resolve_access(db, user_id, origin.project_id, for_update=True)
        except NotFoundError:
            raise NotFoundError("灵感会话不存在") from None
        if not access.is_collaborator or member is None or member.invitation_id != origin.invitation_id:
            raise NotFoundError("灵感会话不存在")
        # 项目锁之后重新锁读会话和来源；整数 ID 复用不能让旧请求读取或覆盖新会话。
        session = await db.scalar(select(InspirationSession).where(
            InspirationSession.id == session_id, InspirationSession.user_id == user_id,
        ).with_for_update().execution_options(populate_existing=True))
        fresh = await db.scalar(select(InspirationOrigin).where(
            InspirationOrigin.session_id == session_id,
        ).with_for_update().execution_options(populate_existing=True))
        if (session is None or fresh is None or not _origin_valid(fresh)
                or session.project_id != original[0]
                or (fresh.project_id, fresh.invitation_id, fresh.origin_key) != original):
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
    if not rows:
        return []
    with db.no_autoflush:
        origins = (await db.scalars(select(InspirationOrigin).where(
            InspirationOrigin.session_id.in_([s.id for s in rows]),
        ))).all()
    origin_projects = {origin.session_id: origin.project_id for origin in origins}
    visible: dict[int, InspirationSession] = {}
    # 多个来源按作品 ID 稳定加锁，避免与其他会话读取/写入发生反序等待。
    for row in sorted(rows, key=lambda s: (origin_projects.get(s.id, 0), s.id)):
        try:
            visible[row.id] = await get_owned_session(db, user_id, row.id)
        except NotFoundError:
            continue
    return [to_brief(visible[s.id]) for s in rows if s.id in visible]


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
    # 清理本人会话不返回其内容；失权后仍可删除自己不可再读取的旧快照。
    async def remove() -> None:
        with db.no_autoflush:
            session = await db.scalar(select(InspirationSession).where(
                InspirationSession.id == session_id, InspirationSession.user_id == user_id,
            ).with_for_update())
        if session is None:
            raise NotFoundError("灵感会话不存在")
        await db.delete(session)

    await _commit_state_change(db, remove)


async def update_session(db: AsyncSession, user_id: int, session_id: int, data: SessionUpdate) -> SessionOut:
    def change(state: SessionState) -> None:
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
        if "scale" in data.model_fields_set:
            state.scale = data.scale

    return await _change_state(db, user_id, session_id, change)


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
    return await _change_state(db, user_id, session_id, lambda state: apply_step(state, step, data))


# ---------------------------------------------------------------- 补充想法


async def add_note(db: AsyncSession, user_id: int, session_id: int, data: NoteCreate) -> SessionOut:
    def change(state: SessionState) -> None:
        text = data.text.strip()
        if not text:
            raise BadRequestError("补充想法不能为空")
        if len(state.notes) >= MAX_NOTES:
            raise BadRequestError(f"补充想法最多 {MAX_NOTES} 条")
        state.notes.append(text)

    return await _change_state(db, user_id, session_id, change)


async def delete_note(db: AsyncSession, user_id: int, session_id: int, index: int) -> SessionOut:
    def change(state: SessionState) -> None:
        if not 0 <= index < len(state.notes):
            raise NotFoundError("补充想法不存在")
        state.notes.pop(index)

    return await _change_state(db, user_id, session_id, change)


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


@dataclass(frozen=True, slots=True)
class _GenerationKey:
    worker_key: str
    operation_key: str
    session_id: int
    step: str

    def predicates(self):
        return (
            InspirationGenerationClaim.session_id == self.session_id,
            InspirationGenerationClaim.step == self.step,
            InspirationGenerationClaim.operation_key == self.operation_key,
            InspirationGenerationClaim.worker_key == self.worker_key,
        )


@dataclass(frozen=True, slots=True)
class _GenerationClaim:
    worker: "RequestWorker"
    actor: AuthenticatedActor | int
    key: _GenerationKey

    def require_local(self) -> None:
        if not self.worker.valid or self.worker.worker_key != self.key.worker_key:
            raise ConflictError("灵感生成状态已变化，请重试")

    @asynccontextmanager
    async def watch_external_io(self) -> AsyncIterator[None]:
        self.require_local()
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(None) as deadline:
                with self.worker.on_lease_loss(lambda: deadline.reschedule(loop.time())):
                    yield
                    self.require_local()
        except TimeoutError:
            self.require_local()
            raise

    async def require(self, db: AsyncSession, *, write: bool = False) -> None:
        await self.worker.require(db, self.key.worker_key, read_only=not write)
        statement = select(InspirationGenerationClaim.operation_key).where(*self.key.predicates())
        if write:
            statement = statement.with_for_update(read=True)
        if await db.scalar(statement) is None:
            raise ConflictError("灵感会话已被删除，选项未保存")
        self.require_local()


@dataclass
class InspirationJob:
    user_id: int
    session_id: int
    step: str
    feedback: str
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    claim: _GenerationClaim
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    released: bool = False


# 仅登记已结束、可由原代次条件删除的占用。存活判定和竞争仍由数据库负责。
_pending_releases: WeakKeyDictionary["RequestWorker", set[_GenerationKey]] = WeakKeyDictionary()


async def _require_generation_session(
    db: AsyncSession, user_id: int, session_id: int, *, lock: bool = False,
) -> InspirationSession:
    session = await get_owned_session(db, user_id, session_id)
    origin = await db.get(InspirationOrigin, session.id)
    if origin is not None:
        await project_access.require_project(db, user_id, origin.project_id, "ai.use", for_update=True)
        # 共享向量、实际发送和结果屏障接齐前，不让个人会话绕过协作 AI 授权。
        raise ConflictError("协作灵感生成暂不可用，请稍后重试", extra={"code": "collaboration_ai_unavailable"})
    if lock:
        session = await db.scalar(select(InspirationSession).where(
            InspirationSession.id == session_id, InspirationSession.user_id == user_id,
        ).with_for_update().execution_options(populate_existing=True))
        if session is None:
            raise NotFoundError("灵感会话不存在")
    return session


async def _acquire(
    sessionmaker: async_sessionmaker[AsyncSession], claim: _GenerationClaim, user_id: int,
) -> tuple[SessionState, int | None]:
    step_key = (
        InspirationGenerationClaim.session_id == claim.key.session_id,
        InspirationGenerationClaim.step == claim.key.step,
    )
    # 对已存在的占用按原 worker 核实；此事务结束后才加用户、会话及新 worker 的锁。
    async with sessionmaker() as db:
        async def refresh_old_owner() -> str | None:
            current = await db.scalar(select(InspirationGenerationClaim.worker_key).where(*step_key))
            if current is not None and not await db.scalar(select(workers.confirmed_terminal(current))):
                await workers.prepare_orphan_scan(db, claim.worker.engine.lease_seconds, worker_key=current)
            return current

        old_key = await _commit_state_change(db, refresh_old_owner)
    async with sessionmaker() as db:
        async def acquire() -> tuple[SessionState, int | None]:
            await require_authenticated(db, claim.actor, write=True)
            session = await _require_generation_session(db, user_id, claim.key.session_id, lock=True)
            state = load_state(session)
            await claim.worker.require(db, claim.key.worker_key)
            insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            row = insert(InspirationGenerationClaim).values(
                session_id=claim.key.session_id, step=claim.key.step,
                operation_key=claim.key.operation_key, worker_key=claim.key.worker_key,
            )
            result = await db.execute(row.on_conflict_do_nothing(
                index_elements=[InspirationGenerationClaim.session_id, InspirationGenerationClaim.step],
            ))
            if result.rowcount != 1 and old_key is not None:
                result = await db.execute(update(InspirationGenerationClaim).where(
                    *step_key, InspirationGenerationClaim.worker_key == old_key, workers.confirmed_terminal(old_key),
                ).values(operation_key=claim.key.operation_key, worker_key=claim.key.worker_key))
            if result.rowcount != 1:
                raise ConflictError("该步骤正在生成选项，请稍候再试")
            return state, session.project_id

        return await _commit_state_change(db, acquire)


async def _delete_claim(sessionmaker: async_sessionmaker[AsyncSession], key: _GenerationKey) -> None:
    async with sessionmaker() as db:
        async def release() -> None:
            await db.execute(delete(InspirationGenerationClaim).where(*key.predicates()))

        await _commit_state_change(db, release)


async def _release(sessionmaker: async_sessionmaker[AsyncSession], claim: _GenerationClaim) -> None:
    with anyio.CancelScope(shield=True):
        try:
            await _delete_claim(sessionmaker, claim.key)
        except Exception:  # noqa: BLE001 用原身份重新核对，不记录请求或 SQL。
            _pending_releases.setdefault(claim.worker, set()).add(claim.key)
            logger.warning("灵感生成占用暂未释放")


async def release_generate(job: InspirationJob, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    if not job.released:
        await _release(sessionmaker, job.claim)
        job.released = True


async def _retry_releases(worker: "RequestWorker") -> None:
    pending = _pending_releases.get(worker)
    if not pending:
        return
    for key in tuple(pending):
        try:
            await _delete_claim(worker.engine.ctx.sessionmaker, key)
        except Exception:  # noqa: BLE001 下一次心跳继续按原身份处理。
            return
        pending.discard(key)


def schedule_releases(worker: "RequestWorker", active: asyncio.Task | None) -> asyncio.Task | None:
    if _pending_releases.get(worker) and (active is None or active.done()):
        return asyncio.create_task(_retry_releases(worker), name="inspiration-release")
    return active


async def _require_send(sessionmaker: async_sessionmaker[AsyncSession], claim: _GenerationClaim) -> None:
    async with sessionmaker() as db:
        await require_authenticated(db, claim.actor)
        await claim.require(db)
    claim.require_local()


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
    worker: "RequestWorker",
) -> InspirationJob:
    load_state(await _require_generation_session(db, user_id, session_id))
    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    override = await prompt_service.get_override(db, user_id, f"inspiration.{step}")
    sessionmaker = visibility_sessionmaker(db)
    actor = actor_snapshot(db, user_id)
    await db.close()
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    key = worker.worker_key
    if key is None:
        raise ConflictError("灵感生成状态暂不可用，请稍后再试")
    claim = _GenerationClaim(worker, actor, _GenerationKey(key, str(uuid4()), session_id, step))
    try:
        state, project_id = await _acquire(sessionmaker, claim, user_id)
        decided = build_decided(state, step)
        knowledge = ""
        async with sessionmaker() as read:
            project = await read.get(Project, project_id) if project_id is not None else None
            if (project is not None and project.user_id == user_id
                    and project.deleted_at is None and project.deletion_started_at is None):
                @asynccontextmanager
                async def query_embed_scope() -> AsyncIterator[None]:
                    await read.close()
                    await _require_send(sessionmaker, claim)
                    async with claim.watch_external_io():
                        yield

                knowledge = await kb_service.knowledge_for_project(
                    read, project, "\n".join([state.idea, *state.notes, decided]),
                    cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private,
                    purpose=f"灵感：{step}", query_embed_scope=query_embed_scope,
                )
            else:
                project = None
        provider = meter_provider(
            provider, cfg, sessionmaker, actor_id=user_id, kind=f"inspiration.{step}",
            project_id=project.id if project is not None else None,
            project_owner_id=project.user_id if project is not None else None,
        )
        messages = build_inspiration_messages(
            step, idea=state.idea, notes=state.notes, decided=decided, history=build_history(state, step),
            feedback=req.feedback, knowledge=knowledge, override=override,
        )
    except BaseException:
        await _release(sessionmaker, claim)
        raise
    return InspirationJob(
        user_id=user_id,
        session_id=session_id,
        step=step,
        feedback=req.feedback.strip(),
        provider=provider,
        model=cfg.model,
        messages=messages,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        claim=claim,
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
    rnd = Round(options=options, feedback=job.feedback, created_at=datetime.now(UTC).isoformat())
    async with sessionmaker() as db:
        async def append_round() -> bool:
            session = await db.scalar(select(InspirationSession).where(
                InspirationSession.id == job.session_id, InspirationSession.user_id == job.user_id
            ).with_for_update())
            if session is None:
                return False
            await job.claim.require(db, write=True)
            state = load_state(session)
            rounds = [*state.steps[job.step].rounds, rnd]  # type: ignore[index]
            state.steps[job.step].rounds = rounds[-MAX_ROUNDS:]  # type: ignore[index]
            _store(session, state)
            return True

        if not await _commit_state_change(db, append_round):
            return None
        return rnd, to_out(await get_owned_session(db, job.user_id, job.session_id))


async def run_generate(
    job: InspirationJob, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[str, dict]]:
    """delta… →（不合格时 retry → delta…，最多 2 次）→ done {round, session} / error。只在成功时写库。"""
    try:
        reason = ""
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                yield "retry", {"attempt": attempt, "max": MAX_RETRIES, "reason": reason}
            await _require_send(sessionmaker, job.claim)
            pieces: list[str] = []
            stream_iter = job.provider.generate_stream(
                job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
            )
            async with aclosing(stream_iter) as stream:
                while True:
                    try:
                        async with job.claim.watch_external_io():
                            piece = await anext(stream)
                    except StopAsyncIteration:
                        break
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
        logger.warning("灵感模式生成选项失败")
        yield "error", {"detail": "生成失败：服务器内部错误"}
    finally:
        await release_generate(job, sessionmaker)


# ---------------------------------------------------------------- 转为项目 / 应用到已有项目 / 从项目发起


def _decided(state: SessionState, step: str) -> Any:
    value = _effective(state, step)
    return value if value else None


async def convert(db: AsyncSession, user_id: int, session_id: int, data: ConvertRequest) -> ProjectOut:
    project = await _commit_state_change(db, lambda: _convert_without_commit(db, user_id, session_id, data))
    await db.refresh(project)
    return await project_service.project_out(db, project, actor_user_id=user_id)


async def _convert_without_commit(db: AsyncSession, user_id: int, session_id: int, data: ConvertRequest) -> Project:
    from app.services.quota_service import check_project_creation

    await check_project_creation(db, user_id)
    session = await get_owned_session(db, user_id, session_id)
    await db.refresh(session)
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
    return project


def _proposals(state: SessionState) -> dict[str, Any]:
    proposed: dict[str, Any] = {step: _decided(state, step) for step in STEPS}
    proposed["premise"] = state.idea.strip() or None
    proposed["target_words"] = state.scale.target_words if state.scale is not None else None
    return {k: v for k, v in proposed.items() if v}


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or (isinstance(value, str) and not value.strip())


async def _session_target(
    db: AsyncSession, user_id: int, session_id: int, project_id: int,
    capability: Literal["content.read", "content.edit"],
) -> tuple[InspirationSession, Project]:
    """需要两个作品时按 ID 顺序取锁，之后复验本人会话和原邀请。"""
    with db.no_autoflush:
        raw = await db.scalar(select(InspirationSession).where(
            InspirationSession.id == session_id, InspirationSession.user_id == user_id,
        ))
        if raw is None:
            raise NotFoundError("灵感会话不存在")
        origin = await db.get(InspirationOrigin, session_id)
        if origin is not None and not _origin_valid(origin):
            raise NotFoundError("灵感会话不存在")
        if origin is not None and project_id < origin.project_id:
            project = await project_access.require_project(db, user_id, project_id, capability, for_update=True)
            session = await get_owned_session(db, user_id, session_id)
        else:
            session = await get_owned_session(db, user_id, session_id)
            project = await project_access.require_project(db, user_id, project_id, capability, for_update=True)
        return session, project


async def apply_preview(db: AsyncSession, user_id: int, session_id: int, project_id: int) -> list[ApplyPreviewItem]:
    session, project = await _session_target(db, user_id, session_id, project_id, "content.read")
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
    session, project = await _session_target(db, user_id, session_id, data.project_id, "content.edit")
    async with AuditOperation(db, user_id, "project.update", "project", project.id, project_id=project.id) as audit:
        audit.target(project)
        audit.track(project, data.fields)
        proposed = _proposals(load_state(session))
        for field in dict.fromkeys(data.fields):
            if field not in proposed:
                raise BadRequestError(f"「{APPLY_LABELS[field]}」没有可应用的内容")
        for field in dict.fromkeys(data.fields):
            value = proposed[field]
            setattr(project, field, list(value) if isinstance(value, list) else value)
        await audit.commit()
    await db.refresh(project)
    return await project_service.project_out(db, project, actor_user_id=user_id)


async def from_project(db: AsyncSession, user_id: int, project_id: int) -> SessionOut:
    """用项目已有字段预填（非空的步骤为已定、source=project）；同一项目已有进行中的会话时直接返回它。"""
    with db.no_autoflush:
        project, access, member = await project_access.resolve_access(db, user_id, project_id, for_update=True)
        if project.user_id != user_id and (not access.is_collaborator or member is None):
            raise NotFoundError("项目不存在")
        invitation_id = member.invitation_id if project.user_id != user_id and member is not None else None
        if invitation_id is not None and not _uuid4(invitation_id):
            raise NotFoundError("项目不存在")
    existing = await db.scalar(
        select(InspirationSession)
        .outerjoin(InspirationOrigin, InspirationOrigin.session_id == InspirationSession.id)
        .where(
            InspirationSession.user_id == user_id,
            InspirationSession.project_id == project.id,
            InspirationSession.status == "active",
            InspirationOrigin.invitation_id == invitation_id if invitation_id is not None
            else InspirationOrigin.session_id.is_(None),
        )
        .order_by(InspirationSession.id.desc())
        .limit(1)
    )
    if existing is not None:
        return to_out(await get_owned_session(db, user_id, existing.id))
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
    await db.flush()
    session_id = session.id
    if invitation_id is not None:
        db.add(InspirationOrigin(session_id=session_id, project_id=project.id, invitation_id=invitation_id))
    await db.commit()
    return to_out(await get_owned_session(db, user_id, session_id))
