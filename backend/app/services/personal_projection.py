"""按真实触发者投影项目私人引用；不会修改 ORM 中的内部快照。"""

import hashlib
import json
import re
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import DateTime, String, and_, exists, false, literal, or_, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    AIConfig,
    GenerationLog,
    McpPlugin,
    Project,
    ProjectCollaborator,
    Skill,
    Task,
    User,
    WritingStyle,
)
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.agent import ChangeOut, ConversationOut, MessageOut
from app.services import project_access
from app.services.agent.access import conversation_writable_fact
from app.services.ai_dispatch import visibility_sessionmaker
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import task_visible_to_actor

# 有些旧快照/工具结果会把这些引用嵌在 params、preview、result 或 JSON 字符串内。
# 使用同一个投影覆盖日志与智能体响应，不能只隐藏顶层字段。
_ID_KIND = {
    "ai_config_id": "configs", "config_id": "configs", "model_config_id": "configs",
    "owner_ai_config_id": "configs",
    "task_id": "tasks", "turn_task_id": "tasks", "retry_of_id": "tasks", "continue_from_task_id": "tasks",
    "retry_source_id": "tasks", "source_task_id": "tasks", "parent_task_id": "tasks",
    "generation_log_id": "logs", "log_id": "logs", "version_log_id": "logs",
    "style_id": "styles", "default_style_id": "styles", "style_choice": "styles",
    "skill_id": "skills", "mcp_plugin_id": "plugins",
}
_LIST_KIND = {
    "ai_config_ids": "configs", "task_ids": "tasks", "generation_log_ids": "logs", "log_ids": "logs",
    "style_ids": "styles", "skill_ids": "skills", "mcp_plugin_ids": "plugins",
}
_OBJECT_KIND = {
    "task": "tasks", "ai_config": "configs", "model_config": "configs", "config": "configs",
    "style": "styles", "skill": "skills",
}
_EFFECT_KIND = {"tasks": "tasks", "generation_logs": "logs"}
_INTERNAL_FIELDS = frozenset({
    "_project_model", "_shared_embedding", "_collaboration_grant", "_resolved_project_model", "_external_claim_id",
    "_external_task_key", "_external_outcome_unknown", "_external_execution", "_external_snapshot_mac",
    "_model_binding", "_model_source",
    "_model_config_owner_id", "_memory_ref_ids", "_memory_analysis_id", "_analysis_index_receipt",
    "_manual_memory_ref_ids",
    "_analysis_proposal_source", "_agent_task_origin",
    "_task_actor_token_version",
    "_agent_actor_token_version",
    "_wizard_actor_token_version", "_retry_source_key",
    "_chapter_claim", "_batch_completion", "_batch_attempt", "_batch_outcome_unknown", "_analysis_pending",
    "_analysis_completion",
    "_wizard_resume", "_wizard_pending", "_wizard_completion", "continue_from_task_key", "rich_analysis",
    "generation_claim_key",
    "generation_claim_task_key",
    "generation_claim_prev_status",
    "_admin_chapter_cancel", "_admin_chapter_receipt",
    "execution_key", "index_task_key", "index_task_queued_at", "index_claim_key",
    "change_key", "task_key", "turn_task_key", "model_config_owner_id", "config_owner_id", "config_fingerprint",
    "config_instance_key", "_config_instance_key", "model_config_instance_key",
    "api_key", "api_key_enc", "apikeyenc", "encrypted_api_key", "api_secret", "secret_key", "authorization",
    "apikey", "encryptedapikey",
    "apisecret", "secretkey", "configfingerprint", "configownerid", "modelconfigownerid",
    "_private_identity",
    "_private_log_references",
    "log_instance_key",
})
_MAX_DEPTH = 64
_MAX_INT = 2_147_483_647
_DROP = object()
_JSON_KEY = re.compile(r'"((?:\\.|[^"\\])*)"\s*:')


def _normalized(name: str) -> str:
    # 保留私有下划线前缀，避免把公开 model_source 当作内部 _model_source。
    return ("_" if name.startswith("_") else "") + "".join(c for c in name.casefold() if c.isalnum())


_PRIVATE_NORMALIZED = frozenset(map(_normalized, _INTERNAL_FIELDS))
_MODEL_METADATA_NORMALIZED = frozenset(map(_normalized, {
    "ai_config_id", "config_id", "model_config_id", "owner_ai_config_id", "ai_config_ids",
    "config", "ai_config", "model_config", "provider", "provider_name", "model", "model_name",
    "base_url", "api_key", "api_key_enc", "encrypted_api_key", "config_fingerprint", "headers", "authorization",
    "api_secret", "secret_key", "access_token", "password", "apikey", "baseurl", "modelname", "configid",
}))


def normalized_private_key(name: Any) -> bool:
    return isinstance(name, str) and _normalized(name) in _PRIVATE_NORMALIZED


def normalized_private_path(path: Any) -> bool:
    if not isinstance(path, str):
        return False
    parts = path.split(".")
    return any(normalized_private_key(".".join(parts[index:])) for index in range(len(parts)))


def normalized_model_metadata_key(name: Any) -> bool:
    return isinstance(name, str) and _normalized(name) in _MODEL_METADATA_NORMALIZED


def has_private_guard(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key, guards in value.items():
        if not isinstance(key, str) or _normalized(key) != "guards" or not isinstance(guards, list):
            continue
        for item in guards:
            if isinstance(item, dict):
                names = {_normalized(name) for name in item if isinstance(name, str)}
                if {"table", "pk", "hash"}.issubset(names) or {"pluginid", "schemahash", "enabled"}.issubset(names):
                    return True
    return False


def _partial_keys(value: str) -> list[str]:
    keys = []
    # 不完整的外层 JSON 字符串仍可能包着多层转义 JSON。逐层只还原引号和 Unicode 转义，
    # 不把普通自由文本当作结构化数据；最大深度与常规投影一致地受限。
    fragment = value
    for _ in range(_MAX_DEPTH):
        for match in _JSON_KEY.finditer(fragment):
            try:
                keys.append(json.loads('"' + match.group(1) + '"'))
            except ValueError:
                continue
        restored = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), fragment)
        restored = restored.replace('\\"', '"').replace("\\\\", "\\")
        if restored == fragment:
            break
        fragment = restored
    else:
        # 超过可证明的最大层数时保持失败关闭，不能原样返回剩余转义内容。
        keys.extend(("_private_identity", "provider"))
    return keys


def partial_private(value: Any, *, in_preview: bool = False) -> bool:
    if not isinstance(value, str) or not value.lstrip().startswith(("{", "[", '"')):
        return False
    keys = _partial_keys(value)
    if any(normalized_private_key(key) for key in keys):
        return True
    normalized = {_normalized(key) for key in keys}
    return ("guards" in normalized and ("hash" in normalized or "schemahash" in normalized)
            and (in_preview or "diff" in normalized or "preview" in normalized))
_AGENT_TASK_EVIDENCE = (
    "id", "statistics_key", "user_id", "project_id", "chapter_id", "kind", "title", "input", "checkpoint",
    "output_text", "error", "retry_of_id", "queued_at", "step_cursor",
)


def agent_task_evidence(task: Task) -> dict[str, Any]:
    """冻结影响任务内容与私人引用的字段，不因仅进度变化而误报身份变化。"""
    return jsonable_encoder({name: getattr(task, name) for name in _AGENT_TASK_EVIDENCE})


def _valid_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= _MAX_INT


def _json(value: Any) -> Any | None:
    if not isinstance(value, str) or not value.lstrip().startswith(("{", "[", '"')):
        return None
    try:
        result = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str) and result.lstrip().startswith(("{", "[", '"')):
        return result
    return None


def _hide_model_metadata(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [_hide_model_metadata(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: None if normalized_model_metadata_key(key)
                else _hide_model_metadata(item, depth + 1) for key, item in value.items()}
    parsed = _json(value)
    if parsed is not None:
        projected = _hide_model_metadata(parsed, depth + 1)
        return value if projected == parsed else json.dumps(projected, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("{", "[", '"')):
        if any(normalized_model_metadata_key(key) for key in _partial_keys(value)):
            return "[模型信息已隐藏]"
    return value


def _project_log_sources(
    payload: list[dict], provenance: list[tuple[Any, Any]], *, user_id: int, is_owner: bool,
) -> list[dict]:
    if len(payload) != len(provenance):
        raise NotFoundError("生成记录不存在")
    result = []
    for item, (source, config_owner_id) in zip(payload, provenance, strict=True):
        item = dict(item)
        actor_model = source == "actor" and _valid_id(config_owner_id) and config_owner_id == user_id
        owner_model = source == "owner" and _valid_id(config_owner_id) and config_owner_id != user_id
        legacy_author = source is None and config_owner_id is None and is_owner
        item["model_source"] = "actor" if actor_model else "owner" if owner_model else None
        if not actor_model and not legacy_author:
            item["ai_config_id"], item["provider"], item["model"] = None, "", ""
            item["params"] = _hide_model_metadata(item["params"])
            item["messages"] = _hide_model_metadata(item["messages"])
            if item["error"]:
                item["error"] = "模型调用未完成，请查看任务状态"
        result.append(item)
    return result


def _field_kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _ID_KIND.get(value.rsplit(".", 1)[-1])


def _collect(value: Any, refs: dict[str, set[int]], depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, refs, depth + 1)
        return
    if not isinstance(value, dict):
        parsed = _json(value)
        if parsed is not None:
            _collect(parsed, refs, depth + 1)
        return
    if "task_key" in value:
        task_id = value.get("task_id")
        if task_id is None and all(key in value for key in ("id", "kind", "status", "project_id")):
            task_id = value.get("id")
        if _valid_id(task_id):
            refs["tasks"].add(task_id)
    kind = _EFFECT_KIND.get(value.get("table")) if isinstance(value.get("table"), str) else None
    pk = value.get("pk")
    if kind and isinstance(pk, dict) and _valid_id(pk.get("id")):
        refs[kind].add(pk["id"])
    field_kind = _field_kind(value.get("field"))
    if field_kind:
        for key in ("before", "after"):
            if _valid_id(value.get(key)):
                refs[field_kind].add(value[key])
    for key, item in value.items():
        if normalized_private_key(key):
            continue
        kind = _ID_KIND.get(key)
        if kind and _valid_id(item):
            refs[kind].add(item)
        kind = _LIST_KIND.get(key)
        if kind and isinstance(item, list):
            refs[kind].update(v for v in item if _valid_id(v))
        kind = _OBJECT_KIND.get(key)
        if kind and isinstance(item, dict) and _valid_id(item.get("id")):
            refs[kind].add(item["id"])
        parsed = _json(item)
        _collect(parsed if parsed is not None else item, refs, depth + 1)


def _reference(value: Any, kind: str, visible: dict[str, set[int]], *, style_choice: bool = False) -> Any:
    if value is None or (style_choice and isinstance(value, str) and value in {"inherit", "none"}):
        return value
    return value if _valid_id(value) and value in visible[kind] else None


def _proven_link(
    kind: str, item_id: Any, proof: Any, user_id: int, identities: dict[str, dict[int, Any]],
) -> bool:
    if not _valid_id(item_id) or not isinstance(proof, dict) or type(proof.get("actor_user_id")) is not int:
        return False
    if proof["actor_user_id"] != user_id:
        return False
    if kind == "tasks":
        value = proof.get("statistics_key")
        return isinstance(value, str) and bool(value) and identities[kind].get(item_id) == value
    current = identities[kind].get(item_id)
    if not isinstance(current, tuple):
        return False
    if "log_instance_key" in proof:
        # 0052 起的证明只按服务端日志实例键识别，不回退到 actor/时间。
        value = proof["log_instance_key"]
        return isinstance(value, str) and bool(value) and current[0] == value
    # 0052 前效果只有创建时间证明；只对仍无实例键的旧行有效。日志实例键不可变，
    # 有键的行必然是 0052 后插入的（可能复用了旧 ID），旧证明不能冒认它。
    if current[0] is not None:
        return False
    value = proof.get("created_at")
    return isinstance(value, str) and bool(value) and current[1] == value


def _project(
    value: Any, visible: dict[str, set[int]], identities: dict[str, dict[int, Any]], user_id: int,
    project_id: int, is_owner: bool, depth: int = 0, inherited_task_key: str | None = None,
    in_preview: bool = False,
) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [
            mapped for item in value
            if (mapped := _project(item, visible, identities, user_id, project_id, is_owner, depth + 1,
                                   inherited_task_key, in_preview)) is not _DROP
        ]
    if not isinstance(value, dict):
        parsed = _json(value)
        if parsed is not None:
            projected = _project(parsed, visible, identities, user_id, project_id, is_owner, depth + 1,
                                 inherited_task_key, in_preview)
            if projected is _DROP:
                return "[内部内容已隐藏]"
            return value if projected == parsed else json.dumps(projected, ensure_ascii=False)
        # 截断的 JSON 无法结构化投影；对可能包含内部预览 hash 的片段失败关闭。
        if partial_private(value, in_preview=in_preview):
            return "[内部内容已隐藏]"
        return value
    # 内部图片绑定效果只供撤销：不能经 ChangeOut、聊天或历史投影泄露资源身份。
    if value.get("table") == "setting_thumbnail_bindings":
        return _DROP
    field = value.get("field")
    if isinstance(field, str) and field.startswith("setting_thumbnail_bindings."):
        return _DROP
    if normalized_private_path(field):
        return _DROP
    if isinstance(field, str) and field.startswith("projects.") and field.endswith(".style"):
        parts = field.split(".")
        if not is_owner or len(parts) != 3 or parts[1] != str(project_id):
            return _DROP
    # 旧 Agent 预览把私人表的整数主键编码在 field 字符串中，却没有留归属证据。
    if isinstance(field, str) and field.split(".", 1)[0] in _EFFECT_KIND and "." in field:
        return _DROP
    kind = _EFFECT_KIND.get(value.get("table")) if isinstance(value.get("table"), str) else None
    pk = value.get("pk")
    if kind:
        if not isinstance(pk, dict) or _reference(pk.get("id"), kind, visible) is None \
                or not _proven_link(kind, pk.get("id"), value.get("_private_identity"), user_id, identities):
            return _DROP
    field_kind = _field_kind(field)
    result = {}
    known_guard = has_private_guard(value)
    linked_task_id = value.get("turn_task_id") if "turn_task_key" in value else value.get("task_id")
    if linked_task_id is None and "task_key" in value \
            and all(key in value for key in ("id", "kind", "status", "project_id")):
        linked_task_id = value.get("id")
    claimed_key = value.get("turn_task_key") if "turn_task_key" in value else value.get("task_key")
    proven_key = None
    if (_reference(linked_task_id, "tasks", visible) is not None and isinstance(claimed_key, str)
            and identities["tasks"].get(linked_task_id) == claimed_key):
        proven_key = claimed_key
    elif linked_task_id is None and isinstance(claimed_key, str) and claimed_key == inherited_task_key:
        proven_key = inherited_task_key
    nested_task_key = proven_key if linked_task_id is not None else (proven_key or inherited_task_key)
    for key, item in value.items():
        if normalized_private_key(key):
            continue
        if isinstance(key, str) and _normalized(key) == "guards" and known_guard:
            continue
        if field_kind and key in {"before", "after"}:
            if isinstance(field, str) and field.startswith("chapter_versions.") and field_kind == "logs":
                # 预览 diff 没有记录生成日志当时的 actor/创建时间，不能因 ID 复用显示旧引用。
                result[key] = None
            else:
                result[key] = _reference(item, field_kind, visible, style_choice=field == "style_choice")
        elif key in _ID_KIND:
            result[key] = _reference(item, _ID_KIND[key], visible, style_choice=key == "style_choice")
        elif key in _LIST_KIND:
            result[key] = ([v for v in item if _reference(v, _LIST_KIND[key], visible) is not None]
                           if isinstance(item, list) else [])
        elif key in _OBJECT_KIND and isinstance(item, dict) and (
            _reference(item.get("id"), _OBJECT_KIND[key], visible) is None
        ):
            result[key] = None
        else:
            if kind and key in {"before", "after"} and isinstance(item, dict):
                # 私人表的 Agent 效果只用于恢复章节关联，旧/伪造的完整行不能成为公开记录。
                item = {name: item[name] for name in ("id", "project_id", "chapter_id") if name in item}
            if value.get("table") == "projects" and key in {"before", "after"} and isinstance(item, dict):
                # 迁移 0013 保留 projects.style 原文；效果的完整行须保留在库内作撤销。
                if not (is_owner and isinstance(pk, dict) and pk.get("id") == project_id):
                    item = dict(item)
                    if item.get("style") not in (None, ""):
                        item["style"] = None
            if value.get("table") == "chapter_versions" and key in {"before", "after"} and isinstance(item, dict):
                item = dict(item)
                log_id = item.get("generation_log_id")
                proofs = value.get("_private_log_references")
                proof = proofs.get(key) if isinstance(proofs, dict) else None
                if log_id is not None and not _proven_link("logs", log_id, proof, user_id, identities):
                    item["generation_log_id"] = None
            parsed = _json(item)
            if parsed is not None:
                projected = _project(parsed, visible, identities, user_id, project_id, is_owner, depth + 1,
                                     nested_task_key, key == "preview")
                if projected is _DROP:
                    projected = None
                result[key] = item if projected == parsed else json.dumps(projected, ensure_ascii=False)
            else:
                projected = _project(item, visible, identities, user_id, project_id, is_owner, depth + 1,
                                     nested_task_key, key == "preview")
                result[key] = None if projected is _DROP else projected
    for key in ("task_key", "turn_task_key"):
        if key in value:
            result[key] = proven_key
    if "turn_task_key" in value and proven_key is None and "turn_task_id" in result:
        result["turn_task_id"] = None
    return result


async def require_personal_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """私人资料须是作者或 accepted 成员；管理员人工访问不能旁路。"""
    with db.no_autoflush:
        project, _, member = await project_access.resolve_access(db, user_id, project_id)
        if project.user_id != user_id and (member is None or member.status != "accepted"):
            raise NotFoundError("项目不存在")
        return project


async def _final_visibility(
    db: AsyncSession, user_id: int, project_id: int, visible: dict[str, set[int]],
    identities: dict[str, dict[int, Any]], task_rounds: dict[int, tuple[Any, str]],
    agent_proofs: dict[str, Any] | None = None,
) -> bool:
    """在新的数据库快照中一次核验登录、成员和所有已有私人引用。"""
    original = db.info.get("authenticated_actor")
    if isinstance(original, AuthenticatedActor) and original.id != user_id:
        raise UnauthorizedError("未登录或登录已过期")
    from app.services.task_engine.stream_lease import is_sqlite_shared_frame_lease

    async def run(verify: AsyncSession):
        member = ProjectCollaborator
        auth = select(
            User.is_active.label("active"), User.token_version.label("token_version"),
            Project.id.label("project_id"), Project.user_id.label("owner_id"),
            Project.deleted_at.label("deleted_at"), Project.deletion_started_at.label("deletion_started_at"),
            Project.ai_history_epoch.label("history_epoch"),
            member.status.label("member_status"), member.role.label("member_role"),
            member.invitation_id.label("member_invitation_id"),
        ).select_from(User).outerjoin(Project, Project.id == project_id).outerjoin(member, and_(
            member.project_id == Project.id, member.user_id == user_id,
        )).where(User.id == user_id).cte("personal_auth")

        # 与授权事实位于同一条 SQL：结束中的原活动任务仍要求旧 grant，且不能变更任务 UUID/入队轮次。
        active_ids = {item_id for item_id, (_, status) in task_rounds.items() if status in OPEN_TASK_STATUSES}
        queries = []
        for kind, values in visible.items():
            ordered = sorted(values)
            for offset in range(0, len(ordered), 400):
                batch = ordered[offset:offset + 400]
                empty_key = literal(None, type_=String())
                empty_time = literal(None, type_=DateTime(timezone=True))
                if kind == "tasks":
                    statement = select(literal(kind).label("kind"), Task.id.label("ref_id"),
                                       Task.statistics_key.label("identity"), Task.queued_at.label("stamp"))
                    statement = statement.where(
                        Task.id.in_(batch), Task.project_id == project_id, task_visible_to_actor(verify, user_id),
                        or_(Task.id.not_in(active_ids), task_visible_to_actor(verify, user_id,
                                                                              active_grant_required=True)),
                    )
                elif kind == "logs":
                    statement = select(literal(kind).label("kind"), GenerationLog.id.label("ref_id"),
                                       GenerationLog.log_instance_key.label("identity"),
                                       GenerationLog.created_at.label("stamp")).where(
                        GenerationLog.id.in_(batch), GenerationLog.actor_user_id == user_id,
                        GenerationLog.project_id == project_id,
                    )
                else:
                    model, condition = {
                        "configs": (AIConfig, AIConfig.user_id == user_id),
                        "styles": (WritingStyle, or_(WritingStyle.user_id == user_id, WritingStyle.user_id.is_(None))),
                        "skills": (Skill, or_(Skill.owner_user_id == user_id, Skill.owner_user_id.is_(None))),
                        "plugins": (McpPlugin, McpPlugin.user_id == user_id),
                    }[kind]
                    statement = select(literal(kind).label("kind"), model.id.label("ref_id"),
                                       empty_key.label("identity"), empty_time.label("stamp")).where(
                        model.id.in_(batch), condition,
                    )
                queries.append(statement)
        if not queries:
            queries.append(select(literal("").label("kind"), literal(0).label("ref_id"),
                                  literal(None, type_=String()).label("identity"),
                                  literal(None, type_=DateTime(timezone=True)).label("stamp")).where(false()))
        if agent_proofs is not None:
            ids = list(agent_proofs["changes"])
            for offset in range(0, len(ids), 400):
                queries.append(select(
                    literal("_agent_change").label("kind"), AgentChange.id.label("ref_id"),
                    literal(None, type_=String()).label("identity"),
                    literal(None, type_=DateTime(timezone=True)).label("stamp"),
                ).where(AgentChange.id.in_(ids[offset:offset + 400])))
            message_ids = list(agent_proofs.get("messages", {}))
            for offset in range(0, len(message_ids), 400):
                queries.append(select(
                    literal("_agent_message").label("kind"), AgentMessage.id.label("ref_id"),
                    literal(None, type_=String()).label("identity"),
                    literal(None, type_=DateTime(timezone=True)).label("stamp"),
                ).where(AgentMessage.id.in_(message_ids[offset:offset + 400])))
            turn = agent_proofs.get("turn")
            if turn is not None:
                queries.append(select(
                    literal("_agent_turn").label("kind"), Task.id.label("ref_id"),
                    Task.statistics_key.label("identity"), Task.queued_at.label("stamp"),
                ).where(Task.id == turn["id"], Task.project_id == project_id,
                        task_visible_to_actor(verify, user_id)))
            conversation = agent_proofs.get("conversation")
            if conversation is not None:
                queries.append(select(
                    literal("_agent_conversation").label("kind"), AgentConversation.id.label("ref_id"),
                    literal(None, type_=String()).label("identity"),
                    literal(None, type_=DateTime(timezone=True)).label("stamp"),
                ).where(AgentConversation.id == conversation["id"]))
        refs = union_all(*queries).subquery("personal_refs")
        statement = select(auth, refs.c.kind, refs.c.ref_id, refs.c.identity, refs.c.stamp).select_from(
            auth.outerjoin(refs, true())
        )
        if agent_proofs is not None:
            linked_task = aliased(Task)
            root_task = aliased(Task, name="agent_root_task")
            history_message = aliased(AgentMessage, name="personal_history_message")
            history_change = aliased(AgentChange, name="personal_history_change")
            has_history = or_(
                AgentConversation.last_message_at.is_not(None), AgentConversation.summarized_until.is_not(None),
                AgentConversation.summary != "",
                exists(select(history_message.id).where(history_message.conversation_id == AgentConversation.id)),
                exists(select(history_change.id).where(history_change.conversation_id == AgentConversation.id)),
            )
            statement = statement.add_columns(
                AgentChange, AgentMessage, AgentConversation, has_history.label("agent_has_history"),
                AgentConversation.id.label("agent_conversation_id"),
                AgentConversation.project_id.label("agent_conversation_project_id"),
                AgentConversation.user_id.label("agent_conversation_user_id"),
                AgentConversation.instance_key.label("agent_conversation_instance_key"),
                linked_task.id.label("linked_task_id"), linked_task.statistics_key.label("linked_task_key"),
                linked_task.project_id.label("linked_task_project_id"),
                linked_task.user_id.label("linked_task_user_id"), root_task,
            ).outerjoin(AgentChange, and_(refs.c.kind == "_agent_change", AgentChange.id == refs.c.ref_id)) \
                .outerjoin(AgentMessage, and_(refs.c.kind == "_agent_message", AgentMessage.id == refs.c.ref_id)) \
                .outerjoin(AgentConversation, or_(
                    and_(refs.c.kind == "_agent_change", AgentConversation.id == AgentChange.conversation_id),
                    and_(refs.c.kind == "_agent_message", AgentConversation.id == AgentMessage.conversation_id),
                    and_(refs.c.kind == "_agent_conversation", AgentConversation.id == refs.c.ref_id),
                )) \
                .outerjoin(linked_task, linked_task.id == AgentChange.turn_task_id) \
                .outerjoin(root_task, and_(refs.c.kind == "_agent_turn", root_task.id == refs.c.ref_id))
        return (await verify.execute(statement.execution_options(populate_existing=True))).all()

    if await is_sqlite_shared_frame_lease(db):
        # 帧租约在 SQLite 回滚日志模式下已持 SHARED 锁，期间写方无法 COMMIT、库快照不会漂移；
        # 禁止再开新连接，否则当写方恰在 COMMIT 持 PENDING 锁时会形成三方死锁。
        with db.no_autoflush:
            rows = await run(db)
    else:
        factory = visibility_sessionmaker(db)
        async with factory() as verify:
            if isinstance(original, AuthenticatedActor):
                verify.info["authenticated_actor"] = original
            rows = await run(verify)

    if not rows or not rows[0].active or (
        isinstance(original, AuthenticatedActor) and rows[0].token_version != original.token_version
    ):
        raise UnauthorizedError("未登录或登录已过期")
    fact = rows[0]
    if fact.project_id is None or fact.deleted_at is not None or (
        fact.owner_id != user_id and fact.member_status != "accepted"
    ):
        raise NotFoundError("项目不存在")
    if fact.deletion_started_at is not None:
        raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
    if agent_proofs is not None:
        # 人工 Agent 当前仍只对作品作者开放；不能将管理员权限或协作者身份当作 Agent 授权。
        if agent_proofs.get("owner_only", True) and fact.owner_id != user_id:
            raise NotFoundError("修改提案不存在")
        found = set()
        for row in rows:
            if row.kind != "_agent_change":
                continue
            expected = agent_proofs["changes"].get(row.ref_id)
            change = row.AgentChange
            if (expected is None or change is None
                    or ChangeOut.model_validate(change).model_dump(mode="json") != expected
                    or row.agent_conversation_id != expected["conversation_id"]
                    or row.agent_conversation_project_id != project_id or row.agent_conversation_user_id != user_id
                    or not row.agent_conversation_instance_key
                    or row.agent_conversation_instance_key != agent_proofs["conversations"].get(
                        expected["conversation_id"],
                    )):
                raise NotFoundError("修改提案不存在")
            task_id = expected["turn_task_id"]
            if task_id is not None:
                task_key = expected.get("turn_task_key")
                if task_key is None:
                    if agent_proofs.get("require_task_proof", True):
                        raise NotFoundError("修改提案不存在")
                elif agent_proofs.get("require_task_proof", True) and (
                    row.linked_task_id != task_id or row.linked_task_project_id != project_id
                    or row.linked_task_user_id != user_id or row.linked_task_key != task_key
                    or row.linked_task_key != agent_proofs["tasks"].get(task_id)
                ):
                    raise NotFoundError("修改提案不存在")
            found.add(row.ref_id)
        if found != set(agent_proofs["changes"]):
            raise NotFoundError("修改提案不存在")
        messages = agent_proofs.get("messages", {})
        found_messages = set()
        for row in rows:
            if row.kind != "_agent_message":
                continue
            expected = messages.get(row.ref_id)
            message = row.AgentMessage
            if (expected is None or message is None
                    or MessageOut.model_validate(message).model_dump(mode="json") != expected
                    or row.agent_conversation_id != expected["conversation_id"]
                    or row.agent_conversation_project_id != project_id or row.agent_conversation_user_id != user_id):
                raise NotFoundError("会话不存在")
            found_messages.add(row.ref_id)
        if found_messages != set(messages):
            raise NotFoundError("会话不存在")
        conversation = agent_proofs.get("conversation")
        if conversation is not None:
            current = next((row for row in rows if row.kind == "_agent_conversation"), None)
            if (current is None or current.AgentConversation is None
                    or current.agent_conversation_id != conversation["id"]
                    or current.agent_conversation_project_id != project_id
                    or current.agent_conversation_user_id != user_id
                    or not current.agent_conversation_instance_key
                    or current.agent_conversation_instance_key != conversation["instance_key"]):
                raise NotFoundError("会话不存在")
            if "snapshot" in conversation:
                # writable 按本次核验快照的作者 / 成员事实重算（与 agent.access.conversation_writable 同口径）。
                snapshot = ConversationOut.model_validate(current.AgentConversation).model_copy(update={
                    "writable": conversation_writable_fact(
                        user_id=user_id, owner_id=fact.owner_id, member_status=fact.member_status,
                        member_role=fact.member_role, member_invitation_id=fact.member_invitation_id,
                        conversation_invitation_id=current.AgentConversation.collaboration_invitation_id,
                    ),
                }).model_dump(mode="json")
                sealed = (current.AgentConversation.ai_history_epoch != current.history_epoch
                          and bool(current.agent_has_history))
                if snapshot != conversation["snapshot"] or sealed != conversation["sealed"]:
                    raise NotFoundError("会话不存在")
        turn = agent_proofs.get("turn")
        if turn is not None and not any(
            row.kind == "_agent_turn" and row.ref_id == turn["id"] and row.identity == turn["statistics_key"]
            and row.stamp == turn["queued_at"] and row.agent_root_task is not None
            and agent_task_evidence(row.agent_root_task) == turn["snapshot"] for row in rows
        ):
            raise NotFoundError("智能体轮次不存在")
    allowed = {kind: set() for kind in visible}
    for row in rows:
        if row.kind is None or row.kind not in allowed or row.ref_id is None:
            continue
        if row.kind == "tasks" and (
            identities["tasks"].get(row.ref_id) != row.identity
            or task_rounds.get(row.ref_id, (None, ""))[0] != row.stamp
        ):
            continue
        if row.kind == "logs" and identities["logs"].get(row.ref_id) != (
            row.identity, jsonable_encoder(row.stamp),
        ):
            continue
        allowed[row.kind].add(row.ref_id)
    for kind in visible:
        visible[kind].intersection_update(allowed[kind])
    return fact.owner_id == user_id


async def project_payload(db: AsyncSession, user_id: int, project_id: int, payload: Any) -> Any:
    """批量过滤私人引用，返回独立结构；当前项目的本人任务与日志才可见。"""
    return await _project_payload(db, user_id, project_id, payload)


async def project_agent_payload(
    db: AsyncSession, user_id: int, project_id: int, payload: Any, proofs: dict[str, Any],
) -> Any:
    """已提交提案在最终一次数据库快照核对完整内容及会话、任务身份。"""
    return await _project_payload(db, user_id, project_id, payload, proofs)


async def project_log_payload(
    db: AsyncSession, user_id: int, project_id: int, payload: list[dict], provenance: list[tuple[Any, Any]],
) -> Any:
    """模型来源与作者身份共同在最终可见性快照之后投影。"""
    return await _project_payload(db, user_id, project_id, payload, log_provenance=provenance)


async def project_document_payload_in_snapshot(
    db: AsyncSession, user_id: int, project_id: int, payload: Any, *, is_owner: bool,
) -> tuple[Any, dict[str, dict[int, Any]]]:
    """在调用方已授权的同一只读快照中投影文档聊天，按语句分批，不另开验证会话。

    调用方必须先在该快照验证 actor、作品生命周期及原协作授权，然后核验每个所选
    会话/消息的完整冻结证据。该函数不是单独的授权入口。
    """
    visible, identities, fingerprints = await _snapshot_references(db, user_id, project_id, payload)
    projected = _project(payload, visible, identities, user_id, project_id, is_owner)
    return (None if projected is _DROP else projected), fingerprints


async def _snapshot_references(
    db: AsyncSession, user_id: int, project_id: int, payload: Any, *, lock_rows: bool = False,
) -> tuple[dict[str, set[int]], dict[str, dict[int, Any]], dict[str, dict[int, Any]]]:
    refs: dict[str, set[int]] = {
        kind: set() for kind in ("configs", "tasks", "logs", "styles", "skills", "plugins")
    }
    _collect(payload, refs)
    visible = {kind: set() for kind in refs}
    identities: dict[str, dict[int, Any]] = {"tasks": {}, "logs": {}}
    fingerprints: dict[str, dict[int, Any]] = {"tasks": {}, "logs": {}}

    def locked(statement):
        return statement.with_for_update(read=True) if lock_rows else statement

    with db.no_autoflush:
        for kind, ids in refs.items():
            values = sorted(ids)
            for offset in range(0, len(values), 200):
                batch = values[offset:offset + 200]
                if kind == "tasks":
                    statement = select(Task.id, Task.statistics_key, Task.queued_at, Task.status).where(
                        Task.id.in_(batch), Task.project_id == project_id, task_visible_to_actor(db, user_id),
                        or_(Task.status.not_in(OPEN_TASK_STATUSES),
                            task_visible_to_actor(db, user_id, active_grant_required=True)),
                    )
                    for row in await db.execute(locked(statement)):
                        visible[kind].add(row.id)
                        identities[kind][row.id] = row.statistics_key
                        fingerprints[kind][row.id] = (row.statistics_key, jsonable_encoder(row.queued_at), row.status)
                    continue
                if kind == "logs":
                    statement = select(
                        GenerationLog.id, GenerationLog.log_instance_key, GenerationLog.created_at,
                    ).where(
                        GenerationLog.id.in_(batch), GenerationLog.actor_user_id == user_id,
                        GenerationLog.project_id == project_id,
                    )
                    for row in await db.execute(locked(statement)):
                        visible[kind].add(row.id)
                        identity = (row.log_instance_key, jsonable_encoder(row.created_at))
                        identities[kind][row.id] = identity
                        fingerprints[kind][row.id] = identity
                    continue
                model, condition = {
                    "configs": (AIConfig, AIConfig.user_id == user_id),
                    "styles": (WritingStyle, or_(WritingStyle.user_id == user_id, WritingStyle.user_id.is_(None))),
                    "skills": (Skill, or_(Skill.owner_user_id == user_id, Skill.owner_user_id.is_(None))),
                    "plugins": (McpPlugin, McpPlugin.user_id == user_id),
                }[kind]
                statement = select(model.id).where(model.id.in_(batch), condition)
                visible[kind].update(await db.scalars(locked(statement)))
    return visible, identities, fingerprints


async def stream_reference_digest(
    db: AsyncSession, user_id: int, project_id: int, payload: Any, *, is_owner: bool,
) -> str:
    """只取公开帧实际引用的可见行并加共享锁。跨端只比较摘要，私人行和凭据留在本机。"""
    visible, _, instances = await _snapshot_references(db, user_id, project_id, payload, lock_rows=True)
    # 状态可在同一任务轮次合法推进。查询已据当前活动态权限筛选可见任务。
    tasks = {key: value[:2] for key, value in instances["tasks"].items()}
    values = [is_owner, {kind: sorted(ids) for kind, ids in visible.items()}, tasks, instances["logs"]]
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _project_payload(
    db: AsyncSession, user_id: int, project_id: int, payload: Any, agent_proofs: dict[str, Any] | None = None,
    *, log_provenance: list[tuple[Any, Any]] | None = None,
) -> Any:
    with db.no_autoflush:
        await require_personal_project(db, user_id, project_id)
        refs: dict[str, set[int]] = {
            kind: set() for kind in ("configs", "tasks", "logs", "styles", "skills", "plugins")
        }
        _collect(payload, refs)
        visible: dict[str, set[int]] = {kind: set() for kind in refs}
        identities: dict[str, dict[int, Any]] = {"tasks": {}, "logs": {}}
        task_rounds: dict[int, tuple[Any, str]] = {}
        for kind, ids in refs.items():
            values = list(ids)
            for offset in range(0, len(values), 400):
                batch = values[offset:offset + 400]
                if kind == "configs":
                    stmt = select(AIConfig.id).where(AIConfig.id.in_(batch), AIConfig.user_id == user_id)
                elif kind == "tasks":
                    stmt = select(Task.id, Task.statistics_key, Task.queued_at, Task.status).where(
                        Task.id.in_(batch), Task.project_id == project_id, task_visible_to_actor(db, user_id),
                    )
                    for row in await db.execute(stmt):
                        visible[kind].add(row.id)
                        identities[kind][row.id] = row.statistics_key
                        task_rounds[row.id] = (row.queued_at, row.status)
                    continue
                elif kind == "logs":
                    stmt = select(GenerationLog.id, GenerationLog.log_instance_key, GenerationLog.created_at).where(
                        GenerationLog.id.in_(batch), GenerationLog.actor_user_id == user_id,
                        GenerationLog.project_id == project_id,
                    )
                    for row in await db.execute(stmt):
                        visible[kind].add(row.id)
                        identities[kind][row.id] = (row.log_instance_key, jsonable_encoder(row.created_at))
                    continue
                elif kind == "styles":
                    stmt = select(WritingStyle.id).where(WritingStyle.id.in_(batch),
                                                         or_(WritingStyle.user_id == user_id,
                                                             WritingStyle.user_id.is_(None)))
                elif kind == "skills":
                    stmt = select(Skill.id).where(Skill.id.in_(batch),
                                                  or_(Skill.owner_user_id == user_id, Skill.owner_user_id.is_(None)))
                else:
                    stmt = select(McpPlugin.id).where(McpPlugin.id.in_(batch), McpPlugin.user_id == user_id)
                visible[kind].update(await db.scalars(stmt))
        is_owner = await _final_visibility(db, user_id, project_id, visible, identities, task_rounds, agent_proofs)
        if log_provenance is not None:
            payload = _project_log_sources(payload, log_provenance, user_id=user_id, is_owner=is_owner)
        projected = _project(payload, visible, identities, user_id, project_id, is_owner)
        return None if projected is _DROP else projected
