"""仅供服务端的作品模型绑定；本模块不接入任何路由或执行发送。

调用方应持有相应的项目写屏障或发送 lease 后重验，再在同一受保护的上下文中
读取绑定的配置并进行完整 provider/端点校验。不得把本模块对象序列化到响应或日志，
也不得在 provider 构造、DNS 校验或实际发送失败后重新解析来切换配置。
"""

import hashlib
import ipaddress
import json
import re
import weakref
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal, get_args
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.engine.base import OptionEngine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.ai.endpoint_guard import PRIVATE_BLOCKED_HINT, is_blocked_ip
from app.core.authorization_runtime import AuthorizationRuntime
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security import SecretCipher
from app.models import AIConfig, Project, ProjectCollaborator, Task, User
from app.schemas.ai_config import ModelType, ProviderName, check_embedding_provider, check_output_within_context
from app.services import ai_dispatch, project_access
from app.services.ai_config_service import MODEL_TYPE_LABELS, lock_config_owner
from app.services.authorization_service import AuthenticatedActor, actor_snapshot

GrantIdentity = tuple[str, int]
ModelSource = Literal["actor", "owner"]
TextImageType = Literal["text", "image"]
EmbeddingReason = Literal["owner_embedding_not_allowed", "embedding_config_missing"]
PROJECT_MODEL_ABSENT = object()
SHARED_EMBEDDING_ABSENT = object()
TASK_GRANT_ABSENT = object()
_PAYLOAD_FIELDS = frozenset({
    "version", "project_id", "actor_id", "project_owner_id", "model_type", "source", "config_id",
    "config_owner_id", "grant_identity", "config_fingerprint", "config_instance_key",
})
_SHARED_EMBEDDING_FIELDS = frozenset({
    "version", "project_id", "actor_id", "project_owner_id", "grant_identity", "mode", "reason", "model",
})


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedProjectModel:
    """服务端专用；配置与来源摘要均不包含明文密钥，不可作为 HTTP 输出。"""

    project_id: int
    actor_id: int
    project_owner_id: int
    model_type: ModelType
    source: ModelSource
    config_id: int
    config_owner_id: int
    grant_identity: GrantIdentity | None
    config_fingerprint: str
    config_instance_key: str

    def __repr__(self) -> str:
        return "<ResolvedProjectModel>"


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedSharedEmbedding:
    """向量绑定或显式关键词模式；绝不采用协作者自己的共享向量模型。"""

    project_id: int
    actor_id: int
    project_owner_id: int
    grant_identity: GrantIdentity | None
    mode: Literal["vector", "keyword"]
    reason: EmbeddingReason | None
    model: ResolvedProjectModel | None

    def __repr__(self) -> str:
        return "<ResolvedSharedEmbedding>"


@dataclass(frozen=True, slots=True, repr=False)
class ProjectModelConfigSnapshot:
    """供受理前 DNS/provider 校验的私有字段快照，不含明文密钥。"""

    id: int
    user_id: int
    instance_key: str
    name: str
    provider: str
    base_url: str | None
    api_key_enc: str
    model: str
    model_type: str
    thinking_level: str
    context_window_k: int | None
    max_output_k: int | None

    def __repr__(self) -> str:
        return "<ProjectModelConfigSnapshot>"


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class ProjectModelPreflight:
    """短读事务结束后的候选；不是已受理授权，也不是发送证明。"""

    actor: AuthenticatedActor
    requested_config_id: int | None
    binding: ResolvedProjectModel
    config: ProjectModelConfigSnapshot
    allow_private: bool

    def __repr__(self) -> str:
        return "<ProjectModelPreflight>"


@dataclass(frozen=True, slots=True, repr=False)
class _PreflightState:
    evidence: tuple[object, ...]
    root_engine: Engine
    pool: object
    runtime: AuthorizationRuntime
    retry_source: object | None = None


# 不接收从请求 JSON 或任意 dataclass 复制出来的预检；证明仅存在于当前进程的受理阶段。
_PREFLIGHTS: weakref.WeakKeyDictionary[ProjectModelPreflight, _PreflightState] = weakref.WeakKeyDictionary()


def _engine_origin(bind: object) -> tuple[Engine, object] | None:
    """仅识别 SQLAlchemy 同根的事务选项包装，不能用 URL 或连接池冒充另一引擎。"""
    if type(bind) is not AsyncEngine:
        return None
    try:
        current = bind.sync_engine
        pool = current.pool
        seen: set[int] = set()
        while type(current) is OptionEngine:
            if id(current) in seen or current.pool is not pool:
                return None
            seen.add(id(current))
            current = current._proxied  # noqa: SLF001  SQLAlchemy OptionEngine 保留其原始 engine。
        if type(current) is not Engine or current.pool is not pool:
            return None
        # 同根包装也不能改写表所在 schema，或以自动提交脱离锁内重验所依赖的事务。
        options = bind.sync_engine.get_execution_options()
        if "schema_translate_map" in options or options.get("isolation_level") == "AUTOCOMMIT":
            return None
        return current, pool
    except Exception:  # noqa: BLE001  不可信或失效的 bind 不能签发或消费预检。
        return None


def _root_transaction(db: AsyncSession) -> object | None:
    """锁内重验期间根事务必须保持同一个；中途提交或替换会释放已取得的屏障。"""
    transaction = db.get_transaction()
    if transaction is None or not transaction.is_active:
        return None
    return transaction.sync_transaction


def _session_engine_origin(db: AsyncSession) -> tuple[Engine, object] | None:
    """只接受单引擎会话；映射级 binds 不得把部分授权查询引向其他数据库。"""
    try:
        if (not isinstance(db, AsyncSession) or type(db.bind) is not AsyncEngine
                or db.sync_session.bind is not db.bind.sync_engine or db.sync_session.binds):
            return None
        return _engine_origin(db.bind)
    except Exception:  # noqa: BLE001
        return None


def _same_preflight_origin(db: AsyncSession, state: _PreflightState) -> bool:
    origin = _session_engine_origin(db)
    return bool(origin is not None and origin[0] is state.root_engine and origin[1] is state.pool
                and db.info.get("authorization_runtime") is state.runtime)


def _invalid_binding() -> BadRequestError:
    # 不回显任何输入，避免损坏的服务端任务数据把配置与密钥材料带入响应或日志。
    return BadRequestError("模型绑定无效或已失效")


def _positive_db_int(value: object) -> bool:
    # PostgreSQL 中相关主键和 revision 均为 INTEGER；排除 bool 及溢出值。
    return type(value) is int and 0 < value <= 2**31 - 1


def _instance_key_valid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
        return str(parsed) == value and parsed.version == 4
    except ValueError:
        return False


def deserialize_project_model(
    payload: object, *, actor_id: int, project_id: int, project_owner_id: int, allow_legacy_owner: bool = False,
) -> ResolvedProjectModel | None:
    """读可信服务端任务数据；旧作者缺席与实际 null 分开，读取后仍须加锁重验。

    三个预期身份必须由可信数据库而非 payload 提供。仅已确认旧作者任务可显式
    开启兼容并传入 PROJECT_MODEL_ABSENT；客户端保留键绝不能直接传入本函数。
    """
    if not all(_positive_db_int(value) for value in (actor_id, project_id, project_owner_id)):
        raise _invalid_binding()
    if payload is PROJECT_MODEL_ABSENT and allow_legacy_owner is True and actor_id == project_owner_id:
        return None
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise _invalid_binding()
    if type(payload["version"]) is not int or payload["version"] != 2:
        raise _invalid_binding()
    for key, expected in (("actor_id", actor_id), ("project_id", project_id), ("project_owner_id", project_owner_id)):
        if not _positive_db_int(payload[key]) or payload[key] != expected:
            raise _invalid_binding()
    if not _positive_db_int(payload["config_id"]) or not _positive_db_int(payload["config_owner_id"]):
        raise _invalid_binding()
    model_type, source = payload["model_type"], payload["source"]
    if type(model_type) is not str or model_type not in ("text", "image", "embedding"):
        raise _invalid_binding()
    if type(source) is not str or source not in ("actor", "owner"):
        raise _invalid_binding()
    fingerprint = payload["config_fingerprint"]
    if type(fingerprint) is not str or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise _invalid_binding()
    instance_key = payload["config_instance_key"]
    if not _instance_key_valid(instance_key):
        raise _invalid_binding()
    raw_grant = payload["grant_identity"]
    grant: GrantIdentity | None = None
    if raw_grant is not None:
        if type(raw_grant) is not dict or set(raw_grant) != {"invitation_id", "revision"}:
            raise _invalid_binding()
        invitation, revision = raw_grant["invitation_id"], raw_grant["revision"]
        if type(invitation) is not str or not _positive_db_int(revision):
            raise _invalid_binding()
        try:
            if str(UUID(invitation)) != invitation:
                raise _invalid_binding()
        except ValueError:
            raise _invalid_binding() from None
        grant = (invitation, revision)
    owner = actor_id == project_owner_id
    if (owner and (source != "actor" or grant is not None)) or (not owner and grant is None):
        raise _invalid_binding()
    if payload["config_owner_id"] != (actor_id if source == "actor" else project_owner_id):
        raise _invalid_binding()
    if source == "owner" and owner:
        raise _invalid_binding()
    if model_type == "embedding" and not owner and source != "owner":
        raise _invalid_binding()
    return ResolvedProjectModel(
        project_id=project_id, actor_id=actor_id, project_owner_id=project_owner_id,
        model_type=model_type, source=source, config_id=payload["config_id"],
        config_owner_id=payload["config_owner_id"], grant_identity=grant, config_fingerprint=fingerprint,
        config_instance_key=instance_key,
    )


def serialize_project_model(binding: ResolvedProjectModel) -> dict[str, object]:
    """只供服务端持久化；产物含配置身份和指纹，禁止发给客户端或记录日志。"""
    if type(binding) is not ResolvedProjectModel:
        raise _invalid_binding()
    grant = binding.grant_identity
    if grant is not None and (type(grant) is not tuple or len(grant) != 2):
        raise _invalid_binding()
    payload = {
        "version": 2, "project_id": binding.project_id, "actor_id": binding.actor_id,
        "project_owner_id": binding.project_owner_id, "model_type": binding.model_type, "source": binding.source,
        "config_id": binding.config_id, "config_owner_id": binding.config_owner_id,
        "grant_identity": {"invitation_id": grant[0], "revision": grant[1]} if grant is not None else None,
        "config_fingerprint": binding.config_fingerprint, "config_instance_key": binding.config_instance_key,
    }
    deserialize_project_model(
        payload, actor_id=binding.actor_id, project_id=binding.project_id, project_owner_id=binding.project_owner_id,
    )
    return payload


def _read_shared_grant(value: object) -> GrantIdentity:
    """只接受任务中的完整、规范授权，禁止静默忽略多余字段或 null。"""
    if type(value) is not dict or set(value) != {"invitation_id", "revision"}:
        raise _invalid_binding()
    invitation, revision = value["invitation_id"], value["revision"]
    if type(invitation) is not str or not _positive_db_int(revision):
        raise _invalid_binding()
    try:
        if str(UUID(invitation)) != invitation:
            raise _invalid_binding()
    except ValueError:
        raise _invalid_binding() from None
    return invitation, revision


def deserialize_shared_embedding(
    payload: object, *, actor_id: int, project_id: int, project_owner_id: int, task_grant: object,
    allow_legacy_owner: bool = False,
) -> ResolvedSharedEmbedding | None:
    """读取内部选择；身份来自可信 DB，task_grant 是服务端任务中的原始保留键。

    缺失选择和缺失授权分别传 SHARED_EMBEDDING_ABSENT、TASK_GRANT_ABSENT；
    显式 null 一律不是缺席。结果不代表当前授权，使用前必须在屏障内重验。
    """
    if not all(_positive_db_int(value) for value in (actor_id, project_id, project_owner_id)):
        raise _invalid_binding()
    owner = actor_id == project_owner_id
    if owner:
        if task_grant is not TASK_GRANT_ABSENT:
            raise _invalid_binding()
        expected_grant = None
    else:
        expected_grant = _read_shared_grant(task_grant)
    if payload is SHARED_EMBEDDING_ABSENT and owner and allow_legacy_owner is True:
        return None
    if type(payload) is not dict or set(payload) != _SHARED_EMBEDDING_FIELDS:
        raise _invalid_binding()
    if type(payload["version"]) is not int or payload["version"] != 2:
        raise _invalid_binding()
    for key, expected in (("actor_id", actor_id), ("project_id", project_id), ("project_owner_id", project_owner_id)):
        if not _positive_db_int(payload[key]) or payload[key] != expected:
            raise _invalid_binding()
    raw_grant = payload["grant_identity"]
    grant = None if raw_grant is None else _read_shared_grant(raw_grant)
    if grant != expected_grant:
        raise _invalid_binding()
    mode, reason, raw_model = payload["mode"], payload["reason"], payload["model"]
    if type(mode) is not str or mode not in ("keyword", "vector"):
        raise _invalid_binding()
    if mode == "keyword":
        if (type(reason) is not str or reason not in ("owner_embedding_not_allowed", "embedding_config_missing")
                or raw_model is not None or (owner and reason == "owner_embedding_not_allowed")):
            raise _invalid_binding()
        model = None
    else:
        if reason is not None or type(raw_model) is not dict:
            raise _invalid_binding()
        model = deserialize_project_model(
            raw_model, actor_id=actor_id, project_id=project_id, project_owner_id=project_owner_id,
        )
        if (model is None or model.model_type != "embedding" or model.config_owner_id != project_owner_id
                or model.grant_identity != grant):
            raise _invalid_binding()
    return ResolvedSharedEmbedding(
        project_id=project_id, actor_id=actor_id, project_owner_id=project_owner_id,
        grant_identity=grant, mode=mode, reason=reason, model=model,
    )


def serialize_shared_embedding(selection: ResolvedSharedEmbedding) -> dict[str, object]:
    """服务端私有 JSON；向量绑定可能含作者配置身份和指纹，禁止对外输出或记日志。"""
    if type(selection) is not ResolvedSharedEmbedding:
        raise _invalid_binding()
    grant = selection.grant_identity
    if grant is not None and (type(grant) is not tuple or len(grant) != 2):
        raise _invalid_binding()
    raw_grant = {"invitation_id": grant[0], "revision": grant[1]} if grant is not None else None
    payload = {
        "version": 2, "project_id": selection.project_id, "actor_id": selection.actor_id,
        "project_owner_id": selection.project_owner_id, "grant_identity": raw_grant,
        "mode": selection.mode, "reason": selection.reason,
        "model": serialize_project_model(selection.model) if selection.model is not None else None,
    }
    deserialize_shared_embedding(
        payload, actor_id=selection.actor_id, project_id=selection.project_id,
        project_owner_id=selection.project_owner_id,
        task_grant=raw_grant if raw_grant is not None else TASK_GRANT_ABSENT,
    )
    return payload


# 标量投影避免刷新 ORM identity map 时覆盖调用方尚未 flush 的配置。
_CONFIG_FIELDS = (
    AIConfig.id, AIConfig.instance_key, AIConfig.user_id, AIConfig.model_type, AIConfig.provider, AIConfig.base_url,
    AIConfig.api_key_enc, AIConfig.model, AIConfig.thinking_level, AIConfig.context_window_k, AIConfig.max_output_k,
)


async def _access(
    db: AsyncSession, actor_id: int, project_id: int, *, for_update: bool,
) -> tuple[Project, ProjectCollaborator | None, GrantIdentity | None]:
    project, access, member = await project_access.resolve_access(db, actor_id, project_id, for_update=for_update)
    if not access.can_use_ai:
        if not access.is_collaborator:
            raise NotFoundError("项目不存在")
        raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})
    if project.user_id == actor_id:
        return project, None, None
    # 明确限定真实身份，管理员的人工权限不能代替 accepted editor。
    if member is None or member.status != "accepted" or member.role != "editor" or member.user_id != actor_id:
        raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    return project, member, (member.invitation_id, member.revision)


def _missing(model_type: ModelType) -> BadRequestError:
    return BadRequestError(f"尚未配置{MODEL_TYPE_LABELS[model_type]}模型，请先在「模型配置」中添加并设为默认")


async def _default_config(db: AsyncSession, user_id: int, model_type: ModelType):
    return (await db.execute(
        select(*_CONFIG_FIELDS).where(AIConfig.user_id == user_id, AIConfig.model_type == model_type)
        .order_by(AIConfig.is_default.desc(), AIConfig.id.desc()).limit(1)
    )).one_or_none()


async def _explicit_config(db: AsyncSession, user_id: int, config_id: int, model_type: TextImageType):
    if not _positive_db_int(config_id):
        raise NotFoundError("模型配置不存在")
    row = (await db.execute(
        select(*_CONFIG_FIELDS).where(AIConfig.id == config_id, AIConfig.user_id == user_id)
    )).one_or_none()
    if row is None:
        raise NotFoundError("模型配置不存在")
    if row.model_type != model_type:
        raise BadRequestError(f"所选配置不是{MODEL_TYPE_LABELS[model_type]}模型")
    return row


def _fingerprint(row) -> str:
    # 只绑定会影响提供方、模型、密钥或输出的字段；默认标记及名称变化不触发旧任务换钥匙。
    parts = (row.user_id, row.model_type, row.provider, row.base_url, row.api_key_enc, row.model,
             row.thinking_level, row.context_window_k, row.max_output_k)
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _validate_local(row, cipher: SecretCipher, *, allow_private: bool, source: ModelSource) -> None:
    """不联网校验；DNS、provider 构造及真实请求仍需出站侧完成。"""
    try:
        cipher.decrypt(row.api_key_enc)
        if row.provider not in get_args(ProviderName) or not row.model.strip():
            raise ValueError("模型提供方或模型名称无效")
        check_embedding_provider(row.provider, row.model_type)
        check_output_within_context(row.context_window_k, row.max_output_k)
        if row.base_url:
            parts = urlsplit(row.base_url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("Base URL 格式不正确，应以 http:// 或 https:// 开头")
            _ = parts.port  # 同时检查非法端口；不解析主机名。
            try:
                address = ipaddress.ip_address(parts.hostname.split("%", 1)[0])
            except ValueError:
                address = None
            if not allow_private and address is not None and is_blocked_ip(address):
                raise ValueError(f"Base URL 指向本地或内网地址。{PRIVATE_BLOCKED_HINT}")
    except ValueError as exc:
        # 作者配置的端点、名称、密钥状态不向协作者泄漏。
        detail = "作者共享模型配置不可用，请联系作者" if source == "owner" else str(exc)
        raise BadRequestError(detail) from None


def _bind(
    project: Project, actor_id: int, grant: GrantIdentity | None, row, *, source: ModelSource,
) -> ResolvedProjectModel:
    instance_key = getattr(row, "instance_key", None)
    if not _instance_key_valid(instance_key):
        raise _invalid_binding()
    return ResolvedProjectModel(
        project_id=project.id, actor_id=actor_id, project_owner_id=project.user_id,
        model_type=row.model_type, source=source, config_id=row.id, config_owner_id=row.user_id,
        grant_identity=grant, config_fingerprint=_fingerprint(row), config_instance_key=instance_key,
    )


async def _read_project_model_preflight(
    db: AsyncSession, actor: AuthenticatedActor, project_id: int, model_type: TextImageType, *,
    cipher: SecretCipher, config_id: int | None, allow_private: bool,
) -> ProjectModelPreflight:
    """一条只读 SQL 固定 actor、授权与两方候选，避免混合不同提交快照。"""
    personal, owner = aliased(AIConfig), aliased(AIConfig)
    personal_id = select(AIConfig.id).where(AIConfig.user_id == actor.id)
    if config_id is None:
        personal_id = personal_id.where(AIConfig.model_type == model_type).order_by(
            AIConfig.is_default.desc(), AIConfig.id.desc(),
        )
    else:
        personal_id = personal_id.where(AIConfig.id == config_id)
    personal_id = personal_id.limit(1).correlate(None).scalar_subquery()
    owner_id = (
        select(AIConfig.id).where(AIConfig.user_id == Project.user_id, AIConfig.model_type == model_type)
        .order_by(AIConfig.is_default.desc(), AIConfig.id.desc()).limit(1).correlate(Project).scalar_subquery()
    )
    columns = (
        "id", "user_id", "instance_key", "name", "provider", "base_url", "api_key_enc", "model",
        "model_type", "thinking_level", "context_window_k", "max_output_k",
    )
    statement = (
        select(
            User.is_active.label("actor_active"), User.token_version.label("actor_version"),
            Project.id.label("project_id"), Project.user_id.label("project_owner_id"),
            Project.deleted_at.label("project_deleted_at"), Project.deletion_started_at.label("project_deleting"),
            ProjectCollaborator.status.label("member_status"), ProjectCollaborator.role.label("member_role"),
            ProjectCollaborator.allow_owner_ai.label("allow_owner_ai"),
            ProjectCollaborator.invitation_id.label("invitation_id"),
            ProjectCollaborator.revision.label("invitation_revision"),
            *(getattr(personal, name).label(f"personal_{name}") for name in columns),
            *(getattr(owner, name).label(f"owner_{name}") for name in columns),
        )
        .select_from(User)
        .outerjoin(Project, Project.id == project_id)
        .outerjoin(ProjectCollaborator, and_(
            ProjectCollaborator.project_id == Project.id, ProjectCollaborator.user_id == User.id,
        ))
        .outerjoin(personal, personal.id == personal_id)
        .outerjoin(owner, owner.id == owner_id)
        .where(User.id == actor.id)
    )
    with db.no_autoflush:
        row = (await db.execute(statement)).mappings().one_or_none()
    if row is None or row["actor_active"] is not True or row["actor_version"] != actor.token_version:
        raise UnauthorizedError("未登录或登录已过期")
    if row["project_id"] is None:
        raise NotFoundError("项目不存在")
    is_owner = row["project_owner_id"] == actor.id
    if not is_owner and row["member_status"] != "accepted":
        raise NotFoundError("项目不存在")
    if row["project_deleted_at"] is not None:
        raise NotFoundError("项目不存在")
    if row["project_deleting"] is not None:
        raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
    if not is_owner and row["member_role"] != "editor":
        raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})
    grant = None if is_owner else (row["invitation_id"], row["invitation_revision"])
    source: ModelSource = "actor"
    prefix = "personal"
    if row["personal_id"] is None:
        if config_id is not None:
            raise NotFoundError("模型配置不存在")
        if not is_owner and row["allow_owner_ai"] is True and row["owner_id"] is not None:
            source, prefix = "owner", "owner"
        else:
            raise _missing(model_type)
    elif row["personal_model_type"] != model_type:
        raise BadRequestError(f"所选配置不是{MODEL_TYPE_LABELS[model_type]}模型")
    snapshot = ProjectModelConfigSnapshot(**{name: row[f"{prefix}_{name}"] for name in columns})
    _validate_local(snapshot, cipher, allow_private=allow_private, source=source)
    project = SimpleNamespace(id=row["project_id"], user_id=row["project_owner_id"])
    binding = _bind(project, actor.id, grant, snapshot, source=source)
    serialize_project_model(binding)  # 对来源、邀请和配置实例格式做同一严格校验。
    return ProjectModelPreflight(actor, config_id, binding, snapshot, allow_private)


async def preview_project_text_model(
    db: AsyncSession, actor_id: int, project_id: int, *, cipher: SecretCipher,
    config_id: int | None, allow_private: bool,
) -> tuple[ResolvedProjectModel, ProjectModelConfigSnapshot]:
    """只读估算使用同一候选选择；不做 DNS、受理或发送授权。"""
    actor = actor_snapshot(db, actor_id)
    if type(actor) is not AuthenticatedActor:
        raise UnauthorizedError("未登录或登录已过期")
    selection = await _read_project_model_preflight(
        db, actor, project_id, "text", cipher=cipher, config_id=config_id, allow_private=allow_private,
    )
    return selection.binding, selection.config


async def preflight_project_model(
    sessionmaker: async_sessionmaker[AsyncSession], actor: AuthenticatedActor, project_id: int,
    model_type: TextImageType, *, cipher: SecretCipher, config_id: int | None = None,
    allow_private: bool = False,
) -> ProjectModelPreflight:
    """自管理短只读 session；返回前已释放，调用方此后才可做 DNS/provider 预检。"""
    if (type(actor) is not AuthenticatedActor or not _positive_db_int(actor.id)
            or type(actor.token_version) is not int or not 0 <= actor.token_version <= 2**31 - 1):
        raise UnauthorizedError("未登录或登录已过期")
    if not _positive_db_int(project_id):
        raise NotFoundError("项目不存在")
    if model_type not in ("text", "image") or type(allow_private) is not bool:
        raise _invalid_binding()
    if config_id is not None and not _positive_db_int(config_id):
        raise NotFoundError("模型配置不存在")
    failed = False
    try:
        async with sessionmaker() as db:
            origin = _session_engine_origin(db)
            runtime = db.info.get("authorization_runtime")
            if origin is None or type(runtime) is not AuthorizationRuntime:
                raise ConflictError("模型预检不可用，请稍后重试", extra={"code": "collaboration_busy"})
            result = await _read_project_model_preflight(
                db, actor, project_id, model_type, cipher=cipher, config_id=config_id, allow_private=allow_private,
            )
            current_origin = _session_engine_origin(db)
            if current_origin is None or current_origin[0] is not origin[0] or current_origin[1] is not origin[1]:
                raise ConflictError("模型预检不可用，请稍后重试", extra={"code": "collaboration_busy"})
        _PREFLIGHTS[result] = _PreflightState(_preflight_evidence(result), origin[0], origin[1], runtime)
        return result
    except SQLAlchemyError:
        failed = True
    if failed:
        raise ConflictError("模型预检不可用，请稍后重试", extra={"code": "collaboration_busy"})
    raise _invalid_binding()


async def preflight_original_project_model(
    sessionmaker: async_sessionmaker[AsyncSession], actor: AuthenticatedActor, retry_source: object, *,
    cipher: SecretCipher, allow_private: bool = False,
) -> ProjectModelPreflight:
    """原任务重试的短读预检；只能沿用原配置，不按当前默认重新选择。"""
    from app.services.collaboration_task_access import GRANT_KEY, task_grant_identity
    from app.services.task_engine.admission import require_original_retry_source, require_retry_source_task

    if (type(actor) is not AuthenticatedActor or not _positive_db_int(actor.id)
            or type(actor.token_version) is not int or not 0 <= actor.token_version <= 2**31 - 1):
        raise UnauthorizedError("未登录或登录已过期")
    if type(allow_private) is not bool:
        raise _invalid_binding()
    source = require_original_retry_source(retry_source, actor)
    if not _positive_db_int(source.project_id):
        raise _invalid_binding()
    try:
        source_input = json.loads(source.input_json)
        payload = source_input["_project_model"]
        config_id = payload["config_id"]
        if type(source_input) is not dict or type(payload) is not dict or not _positive_db_int(config_id):
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError):
        raise _invalid_binding() from None

    columns = (
        "id", "user_id", "instance_key", "name", "provider", "base_url", "api_key_enc", "model",
        "model_type", "thinking_level", "context_window_k", "max_output_k",
    )
    statement = (
        select(
            Task, User.is_active.label("actor_active"), User.token_version.label("actor_version"),
            Project.id.label("project_id"), Project.user_id.label("project_owner_id"),
            Project.deleted_at.label("project_deleted_at"), Project.deletion_started_at.label("project_deleting"),
            ProjectCollaborator.status.label("member_status"), ProjectCollaborator.role.label("member_role"),
            ProjectCollaborator.allow_owner_ai.label("allow_owner_ai"),
            ProjectCollaborator.invitation_id.label("invitation_id"),
            ProjectCollaborator.revision.label("invitation_revision"),
            *(getattr(AIConfig, name).label(f"config_{name}") for name in columns),
        )
        .select_from(User)
        .outerjoin(Project, Project.id == source.project_id)
        .outerjoin(ProjectCollaborator, and_(
            ProjectCollaborator.project_id == Project.id, ProjectCollaborator.user_id == User.id,
        ))
        .outerjoin(Task, and_(Task.id == source.task_id, Task.user_id == User.id, Task.project_id == Project.id))
        .outerjoin(AIConfig, AIConfig.id == config_id)
        .where(User.id == actor.id)
    )
    failed = False
    try:
        async with sessionmaker() as db:
            origin = _session_engine_origin(db)
            runtime = db.info.get("authorization_runtime")
            if origin is None or type(runtime) is not AuthorizationRuntime:
                raise ConflictError("模型预检不可用，请稍后重试", extra={"code": "collaboration_busy"})
            with db.no_autoflush:
                row = (await db.execute(statement)).mappings().one_or_none()
            if row is None or row["actor_active"] is not True or row["actor_version"] != actor.token_version:
                raise UnauthorizedError("未登录或登录已过期")
            if row["project_id"] is None:
                raise NotFoundError("项目不存在")
            is_owner = row["project_owner_id"] == actor.id
            if not is_owner and row["member_status"] != "accepted":
                raise NotFoundError("项目不存在")
            if row["project_deleted_at"] is not None:
                raise NotFoundError("项目不存在")
            if row["project_deleting"] is not None:
                raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
            if not is_owner and row["member_role"] != "editor":
                raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})

            task = row["Task"]
            require_retry_source_task(source, task, actor)
            if (task.status not in ("failed", "cancelled", "interrupted") or task.execution_key is not None
                    or task.admin_cancel_operation_id is not None or task.admin_cancel_requested_at is not None
                    or type(task.input.get("_task_actor_token_version")) is not int
                    or task.input["_task_actor_token_version"] != actor.token_version):
                raise _invalid_binding()
            grant = None if is_owner else (row["invitation_id"], row["invitation_revision"])
            raw_grant = task.input.get(GRANT_KEY)
            if ((is_owner and GRANT_KEY in task.input)
                    or (not is_owner and (type(raw_grant) is not dict
                                          or set(raw_grant) != {"invitation_id", "revision"}
                                          or task_grant_identity(task.input) != grant))):
                raise _invalid_binding()
            binding = deserialize_project_model(
                task.input.get("_project_model"), actor_id=actor.id, project_id=source.project_id,
                project_owner_id=row["project_owner_id"],
            )
            if (binding is None or binding.model_type not in ("text", "image") or binding.grant_identity != grant
                    or (binding.source == "owner" and row["allow_owner_ai"] is not True)
                    or row["config_id"] is None):
                raise _invalid_binding()
            snapshot = ProjectModelConfigSnapshot(**{name: row[f"config_{name}"] for name in columns})
            project = SimpleNamespace(id=row["project_id"], user_id=row["project_owner_id"])
            if _bind(project, actor.id, grant, snapshot, source=binding.source) != binding:
                raise _invalid_binding()
            _validate_local(snapshot, cipher, allow_private=allow_private, source=binding.source)
            result = ProjectModelPreflight(actor, None, binding, snapshot, allow_private)
            current_origin = _session_engine_origin(db)
            if current_origin is None or current_origin[0] is not origin[0] or current_origin[1] is not origin[1]:
                raise ConflictError("模型预检不可用，请稍后重试", extra={"code": "collaboration_busy"})
        _PREFLIGHTS[result] = _PreflightState(_preflight_evidence(result), origin[0], origin[1], runtime, source)
        return result
    except SQLAlchemyError:
        failed = True
    if failed:
        raise ConflictError("模型预检不可用，请稍后重试", extra={"code": "collaboration_busy"})
    raise _invalid_binding()


def _preflight_evidence(preflight: ProjectModelPreflight) -> tuple[object, ...]:
    """复制原始标量，检测内部对象被替换或绕过 frozen 修改。"""
    config = preflight.config
    return (
        preflight.actor.id, preflight.actor.token_version, preflight.requested_config_id,
        json.dumps(serialize_project_model(preflight.binding), ensure_ascii=False, sort_keys=True),
        config.id, config.user_id, config.instance_key, config.name, config.provider, config.base_url,
        config.api_key_enc, config.model, config.model_type, config.thinking_level,
        config.context_window_k, config.max_output_k, preflight.allow_private,
    )


def require_original_preflight(preflight: object) -> ProjectModelPreflight:
    """DNS 前验证真实短读产生的原对象；不读取数据库，不构成最终受理或发送证明。"""
    try:
        if (type(preflight) is not ProjectModelPreflight or type(preflight.actor) is not AuthenticatedActor
                or type(preflight.binding) is not ResolvedProjectModel
                or type(preflight.config) is not ProjectModelConfigSnapshot
                or type(preflight.allow_private) is not bool):
            raise _invalid_binding()
        state = _PREFLIGHTS.get(preflight)
        if (state is None or state.evidence != _preflight_evidence(preflight)
                or state.root_engine.pool is not state.pool):
            raise _invalid_binding()
        if state.retry_source is not None:
            from app.services.task_engine.admission import require_original_retry_source

            require_original_retry_source(state.retry_source, preflight.actor)
        binding, config = preflight.binding, preflight.config
        if (binding.actor_id != preflight.actor.id or config.id != binding.config_id
                or config.user_id != binding.config_owner_id or config.instance_key != binding.config_instance_key
                or _fingerprint(config) != binding.config_fingerprint):
            raise _invalid_binding()
        return preflight
    except Exception:  # noqa: BLE001  对伪造对象或损坏字段只给固定安全错误。
        pass
    raise _invalid_binding()


def is_original_model_preflight(preflight: object) -> bool:
    original = require_original_preflight(preflight)
    return _PREFLIGHTS[original].retry_source is not None


def require_retry_model_preflight(preflight: object, retry_source: object) -> ProjectModelPreflight:
    """DNS 前核验预检绑定的就是同一原任务来源对象。"""
    original = require_original_preflight(preflight)
    if _PREFLIGHTS[original].retry_source is not retry_source or retry_source is None:
        raise _invalid_binding()
    return original


async def accept_preflight_project_model(
    db: AsyncSession, preflight: ProjectModelPreflight, *, cipher: SecretCipher, allow_private: bool = False,
) -> ResolvedProjectModel:
    """按 User → 发送门禁 → 项目 → 配置锁重选；与原预检不一致时回滚并失败关闭。"""
    preflight = require_original_preflight(preflight)
    state = _PREFLIGHTS.get(preflight)
    if state is None or state.retry_source is not None or not _same_preflight_origin(db, state):
        raise _invalid_binding()
    if (type(allow_private) is not bool or allow_private != preflight.allow_private
            or db.info.get("authenticated_actor") != preflight.actor):
        raise UnauthorizedError("未登录或登录已过期")
    binding = preflight.binding
    changed = False
    try:
        with db.no_autoflush:
            await project_access.require_project(
                db, preflight.actor.id, binding.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=binding.grant_identity,
            )
            transaction = _root_transaction(db)
            current = await resolve_project_model(
                db, preflight.actor.id, binding.project_id, binding.model_type, cipher=cipher,
                config_id=preflight.requested_config_id, allow_private=allow_private, for_update=True,
            )
            if current != binding or transaction is None or _root_transaction(db) is not transaction:
                changed = True
            else:
                return current
    except UnauthorizedError:
        await db.rollback()
        raise
    except (BadRequestError, ConflictError, ForbiddenError, NotFoundError, SQLAlchemyError):
        changed = True
    if changed:
        await db.rollback()
        raise ConflictError("模型授权或配置已变化，请重新发起", extra={"code": "collaboration_busy"})
    raise _invalid_binding()


async def accept_original_preflight_project_model(
    db: AsyncSession, preflight: ProjectModelPreflight, *, admission: object, cipher: SecretCipher,
    allow_private: bool = False,
) -> ResolvedProjectModel:
    """只接受已消费的原任务重试证明，锁内重验原实例，不按当前默认重新选择。"""
    from app.services.task_engine.admission import require_consumed_retry_admission

    preflight = require_original_preflight(preflight)
    state = _PREFLIGHTS.get(preflight)
    if state is None or state.retry_source is None or not _same_preflight_origin(db, state):
        raise _invalid_binding()
    if (type(allow_private) is not bool or allow_private != preflight.allow_private
            or db.info.get("authenticated_actor") != preflight.actor):
        raise UnauthorizedError("未登录或登录已过期")
    if admission is None:
        raise ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})
    binding = preflight.binding
    changed = False
    try:
        with db.no_autoflush:
            await project_access.require_project(
                db, preflight.actor.id, binding.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=binding.grant_identity,
            )
            source = await require_consumed_retry_admission(db, admission, preflight)
            if source is not state.retry_source:
                raise _invalid_binding()
            transaction = _root_transaction(db)
            await revalidate_project_model(db, binding, cipher=cipher, allow_private=allow_private, lock_config=True)
            if transaction is None or _root_transaction(db) is not transaction:
                changed = True
            else:
                return binding
    except UnauthorizedError:
        await db.rollback()
        raise
    except (BadRequestError, ConflictError, ForbiddenError, NotFoundError, SQLAlchemyError):
        changed = True
    if changed:
        await db.rollback()
        raise ConflictError("模型授权或配置已变化，请重新发起", extra={"code": "collaboration_busy"})
    raise _invalid_binding()


async def resolve_project_model(
    db: AsyncSession, actor_id: int, project_id: int, model_type: TextImageType, *, cipher: SecretCipher,
    config_id: int | None = None, allow_private: bool = False, for_update: bool = False,
) -> ResolvedProjectModel:
    """文本/图片：配置来源在同一屏障中选择；受理任务还须持项目父行锁。"""
    if model_type not in ("text", "image"):
        raise ValueError("共享向量必须使用独立的解析策略")
    with db.no_autoflush:
        project, member, grant = await _access(db, actor_id, project_id, for_update=for_update)
        owner_id = project.user_id
        # 多次读取本人及作者配置必须属于同一选择视图；写入按同一用户配置门禁排队。
        # 对需要固定任务绑定的调用，for_update=True 先取得认证/User 和项目父行锁。
        for config_owner_id in sorted({actor_id, owner_id}):
            await lock_config_owner(db, config_owner_id, write=False)
        # 等待配置门禁期间权限或作品作者可能变化；不能使用屏障前的成员快照。
        project, member, grant = await _access(db, actor_id, project_id, for_update=for_update)
        if project.user_id != owner_id:
            raise ConflictError("作品权限已变化，请重新选择模型", extra={"code": "collaboration_busy"})
        source: ModelSource = "actor"
        if config_id is not None:
            row = await _explicit_config(db, actor_id, config_id, model_type)
        else:
            row = await _default_config(db, actor_id, model_type)
            if row is None and member is not None and member.allow_owner_ai:
                row = await _default_config(db, project.user_id, model_type)
                source = "owner"
            if row is None:
                raise _missing(model_type)
        _validate_local(row, cipher, allow_private=allow_private, source=source)
        return _bind(project, actor_id, grant, row, source=source)


async def _validate_binding(
    db: AsyncSession, binding: ResolvedProjectModel, *, cipher: SecretCipher, allow_private: bool,
    for_update: bool, lock_config: bool,
) -> None:
    project, member, grant = await _access(db, binding.actor_id, binding.project_id, for_update=for_update)
    if project.user_id != binding.project_owner_id or grant != binding.grant_identity:
        raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
    if binding.source == "actor":
        valid_source = binding.config_owner_id == binding.actor_id
    elif binding.source == "owner":
        valid_source = bool(member is not None and member.allow_owner_ai and binding.config_owner_id == project.user_id)
    else:
        valid_source = False
    if not valid_source:
        raise ForbiddenError("模型授权已变化", extra={"code": "permission_denied"})
    if lock_config and binding.model_type == "embedding":
        # 必须先取作者配置选择门禁；当前配置行锁单独不能阻止新配置插入。
        await lock_config_owner(db, project.user_id, write=False)
    statement = select(*_CONFIG_FIELDS).where(
        AIConfig.id == binding.config_id, AIConfig.user_id == binding.config_owner_id,
        AIConfig.model_type == binding.model_type,
    )
    if lock_config and db.get_bind().dialect.name == "postgresql":
        # 发送期间共享锁防止配置在验证后被更新或删除；释放仍由调用方控制。
        statement = statement.with_for_update(read=True)
    elif lock_config and db.get_bind().dialect.name == "sqlite":
        try:
            # SQLite 没有行级共享锁；现有发送 lease 也使用同一数据库写屏障。
            await db.execute(update(AIConfig).where(AIConfig.id == binding.config_id).values(
                id=AIConfig.id, updated_at=AIConfig.updated_at,
            ).execution_options(synchronize_session=False))
        except OperationalError as exc:
            if ai_dispatch.is_sqlite_busy(exc):
                await db.rollback()
                raise ConflictError("模型配置正在变化，请重试", extra={"code": "collaboration_busy"}) from None
            raise
    elif lock_config:
        raise RuntimeError("受控模型配置不支持当前数据库")
    row = (await db.execute(statement)).one_or_none()
    if (row is None or row.instance_key != binding.config_instance_key
            or _fingerprint(row) != binding.config_fingerprint):
        raise BadRequestError("模型配置已失效，请重新选择")
    if binding.model_type == "embedding":
        if binding.config_owner_id != project.user_id:
            raise ForbiddenError("模型授权已变化", extra={"code": "permission_denied"})
        current = await _default_config(db, project.user_id, "embedding")
        if current is None or current.id != binding.config_id:
            raise BadRequestError("项目向量模型已变化，请重新选择")
    _validate_local(row, cipher, allow_private=allow_private, source=binding.source)


async def revalidate_project_model(
    db: AsyncSession, binding: ResolvedProjectModel, *, cipher: SecretCipher,
    allow_private: bool = False, for_update: bool = False, lock_config: bool = True,
) -> None:
    """检查原绑定；实际发送须在 lease 中启用 lock_config 并持锁至响应头。"""
    with db.no_autoflush:
        # 内部 dataclass 也不自动可信：防 bool ID、坏实例 UUID 和不一致来源绕过 JSON 校验。
        serialize_project_model(binding)
        await _validate_binding(
            db, binding, cipher=cipher, allow_private=allow_private, for_update=for_update, lock_config=lock_config,
        )


async def resolve_shared_embedding(
    db: AsyncSession, actor_id: int, project_id: int, *, cipher: SecretCipher, require_vector: bool = False,
    allow_private: bool = False, for_update: bool = False,
) -> ResolvedSharedEmbedding:
    """共享索引仅选择作者向量；无授权/无配置显式退回关键词。"""
    with db.no_autoflush:
        project, member, grant = await _access(db, actor_id, project_id, for_update=for_update)
        reason: EmbeddingReason | None = None
        row = None
        if member is not None and not member.allow_owner_ai:
            reason = "owner_embedding_not_allowed"
            if require_vector:
                raise ForbiddenError("作者未授权共享向量模型", extra={"code": reason})
        else:
            row = await _default_config(db, project.user_id, "embedding")
            if row is None:
                reason = "embedding_config_missing"
                if require_vector:
                    raise _missing("embedding")
        source: ModelSource = "owner" if member is not None else "actor"
        if row is not None:
            _validate_local(row, cipher, allow_private=allow_private, source=source)
        model = _bind(project, actor_id, grant, row, source=source) if row is not None else None
        return ResolvedSharedEmbedding(
            project_id=project.id, actor_id=actor_id, project_owner_id=project.user_id, grant_identity=grant,
            mode="vector" if model else "keyword", reason=reason, model=model,
        )


async def revalidate_shared_embedding(
    db: AsyncSession, selection: ResolvedSharedEmbedding, *, cipher: SecretCipher,
    allow_private: bool = False, for_update: bool = False, lock_config: bool = True,
) -> None:
    """保留原 vector/keyword 决策，不因配置或授权变化自动转换模式。"""
    with db.no_autoflush:
        serialize_shared_embedding(selection)
        if selection.mode == "vector" and selection.model is not None:
            model = selection.model
            if (model.project_id, model.actor_id, model.project_owner_id, model.grant_identity, model.model_type) != (
                selection.project_id, selection.actor_id, selection.project_owner_id, selection.grant_identity,
                "embedding",
            ) or model.config_owner_id != selection.project_owner_id:
                raise ForbiddenError("模型授权已变化", extra={"code": "permission_denied"})
            await _validate_binding(
                db, model, cipher=cipher, allow_private=allow_private, for_update=for_update, lock_config=lock_config,
            )
            return
        if selection.mode != "keyword" or selection.model is not None:
            raise ForbiddenError("模型授权已变化", extra={"code": "permission_denied"})
        project, member, grant = await _access(db, selection.actor_id, selection.project_id, for_update=for_update)
        if project.user_id != selection.project_owner_id or grant != selection.grant_identity:
            raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
        if selection.reason == "owner_embedding_not_allowed" and (member is None or member.allow_owner_ai):
            raise ForbiddenError("模型授权已变化", extra={"code": "permission_denied"})
        if selection.reason not in ("owner_embedding_not_allowed", "embedding_config_missing"):
            raise ForbiddenError("模型授权已变化", extra={"code": "permission_denied"})
