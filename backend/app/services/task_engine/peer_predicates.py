"""两跳任务帧的业务读条件。原 guard 留在签发进程，接收侧只持 DB 读锁至物理发送。"""

from __future__ import annotations

import re
from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Task
from app.services import (
    ai_dispatch,
    analysis_service,
    chapter_claim,
    project_access,
    project_ai_config,
    scene_ai_context,
    wizard_service,
)
from app.services import (
    collaboration_ai_guard as local,
)
from app.services import entity_ai_service as entities
from app.services.authorization_service import AuthenticatedActor, require_authenticated_for_send
from app.services.collaboration_task_access import GRANT_KEY, task_grant_identity
from app.services.task_engine.execution_identity import require_live_worker
from app.services.task_engine.peer_protocol import digest
from app.services.task_engine.stream_lease import require_task_stream_physical_lease

_SINGLE = {"chapter.generate", "chapter.polish", "chapter.revise"}
_ENTITY = {"character.generate", "organization.generate", "career.generate", "relations.generate", "relations.parse"}
_SIMPLE = {"outline.continue", "outline.expand", "consistency.check", "consistency.impact", "agent.turn"}
_SUPPORTED = (_SINGLE | _ENTITY | _SIMPLE | local._WIZARD_KINDS
              | {"chapter.batch", "chapter.analyze", "scene.restructure"})
_NO_PUBLICATION = {"claim", "queued", "view"}
_TERMINAL = {"succeeded", "failed", "interrupted", "cancelled", "paused"}


def _hash(value) -> str:
    return digest(value).hex()


def _require(valid) -> None:
    if not valid:
        raise local.CollaborationSendBlocked()


def _stamp_hash(stamp):
    return _hash([stamp.project_id, stamp.revision, stamp.epoch,
                  stamp.last_deleted_at.isoformat() if stamp.last_deleted_at else None])


def _models_hash(primary, extra):
    _require(type(primary) is project_ai_config.ResolvedProjectModel and primary.model_type == "text")
    _require(len(extra) <= 1)
    return _hash([project_ai_config.serialize_project_model(primary),
                  *[project_ai_config.serialize_shared_embedding(selection) for selection in extra]])


def _read_models(task, project):
    payload = task.input
    expected = {"actor_id": task.user_id, "project_id": task.project_id, "project_owner_id": project.user_id}
    primary = project_ai_config.deserialize_project_model(payload[local.MODEL_KEY], **expected)
    _require(primary is not None and local._matches_task_binding(payload, primary))
    extra = ()
    if local.EMBEDDING_KEY in payload:
        selection = project_ai_config.deserialize_shared_embedding(
            payload[local.EMBEDDING_KEY], **expected,
            task_grant=payload.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        )
        _require(selection is not None and selection.grant_identity == primary.grant_identity)
        extra = (selection,)
    return primary, extra


def _entity_groups(raw, kind):
    core = ["character", "world", "organization", "career", "stage", "member"]
    if kind == "relations.generate":
        core += ["relation", "type"]
    _require(type(raw) is list and len(raw) <= len(core) + 3)
    kinds = [item["kind"] for item in raw]
    allowed = ([core] if kind == "relations.parse" else
               [core + ["kb_settings"], core + ["kb_settings", "document", "chunk"]])
    _require(kinds in allowed)
    groups = []
    for item in raw:
        _require(type(item) is dict and set(item) == {"kind", "parents", "extra", "prior_ids", "fingerprint"})
        parents, extra, prior_ids = item["parents"], item["extra"], item["prior_ids"]
        for ids in (parents, extra, prior_ids):
            _require(type(ids) is list and all(type(n) is int and 0 < n <= 2**63 - 1 for n in ids))
        _require(not prior_ids or item["kind"] in ("relation", "type"))
        fingerprint = item["fingerprint"]
        _require(type(fingerprint) is str and re.fullmatch("[0-9a-f]{64}", fingerprint))
        groups.append(entities.EntityStreamGroup(item["kind"], tuple(parents), tuple(extra), tuple(prior_ids),
                                                fingerprint))
    return tuple(groups)


def _business_conditions(task, guard):
    kind = task.kind
    _require(kind in _SUPPORTED and task.status in _TERMINAL | {"running"})
    running = task.status == "running"
    conditions = {}
    if kind == "chapter.batch":
        conditions["branch"] = guard._batch_stream_shape()
        if running and conditions["branch"] == "attempt":
            conditions["scene"] = None
    if kind in local._WIZARD_KINDS:
        conditions["branch"] = guard._wizard_stream_shape()
        conditions["resume"] = _hash(asdict(guard._resume)) if guard._resume is not None else None
        conditions["resume_epoch"] = guard._resume_epoch
    if guard._scene is not None and (running or guard._chapter is None):
        conditions["scene"] = _hash(guard._scene.log_data())
    if guard._chapter is not None and running:
        claim = guard._chapter
        conditions["chapter"] = _hash({"chapter_id": claim.chapter_id, "claim_key": claim.claim_key,
                                       "prev_status": claim.prev_status})
    if guard._analysis_pending is not None and running:
        conditions["pending"] = _hash(analysis_service._pending_document(guard._analysis_pending))
    if guard._analysis_reference is not None and running:
        conditions["reference"] = _hash(guard._analysis_reference.as_dict())
    if guard._entity_sources is not None:
        conditions["groups"] = []
        for group in guard._entity_sources.groups:
            packed = entities.EntityStreamGroup.from_source(group)
            conditions["groups"].append({
                "kind": packed.kind, "parents": list(packed.parents), "extra": list(packed.extra),
                "prior_ids": list(packed.prior_ids), "fingerprint": packed.fingerprint,
            })
    return conditions


async def capture(db: AsyncSession, task: Task, project: Project, *, source, event, data, guard) -> dict:
    """仅转录本次本地真实帧核验已经选中的 guard；不从数据库签发替代来源。"""
    if source in _NO_PUBLICATION:
        _require(guard is None)
        return {}
    _require(source == "publication" and type(guard) is local.SendGuard and guard._stream_readable()
             and guard._task.task_id == task.id and guard._task.task_key == task.statistics_key
             and guard._task.kind == task.kind and guard._actor.id == task.user_id
             and guard._binding.project_id == project.id == task.project_id)
    await require_task_stream_physical_lease(db, task.user_id, project.id,
                                             task_id=task.id, task_key=task.statistics_key)
    return {
        "kind": task.kind, "token": guard._actor.token_version, "visibility": _stamp_hash(guard._stamp),
        "models": _models_hash(guard._binding, guard._extra_bindings), "business": _business_conditions(task, guard),
    }


async def _check_chapter(db, task, pointer, expected):
    _require(type(pointer) is dict and set(pointer) == {"chapter_id", "claim_key", "prev_status"}
             and type(pointer["chapter_id"]) is int and pointer["chapter_id"] > 0
             and pointer["prev_status"] in ("draft", "done") and _hash(pointer) == expected)
    row = await chapter_claim._chapter(db, pointer["chapter_id"], write=False)
    _require(chapter_claim.chapter_has_claim_pointer(row, project_id=task.project_id, task_key=task.statistics_key,
                                                     **pointer))
    return row.id


async def _check_business(db, task, business, grant, epoch):
    kind = task.kind
    running = task.status == "running"
    _require(type(business) is dict)
    fields = set()
    scene_chapter_id = None
    if kind == "chapter.batch":
        fields.add("branch")
        branch = business["branch"]
        _require(branch in {"between", "attempt", "analysis"})
        if running and branch == "attempt":
            fields.add("chapter")
            current = task.checkpoint["current"]
            chapter_id = await _check_chapter(db, task, current["_chapter_claim"], business["chapter"])
            _require(type(current["chapter_id"]) is int and current["chapter_id"] == chapter_id)
            fields.add("scene")
            if business["scene"] is not None:  # 本章构建完成的签名条件带场景戳。
                scene_chapter_id = chapter_id
        if running and branch == "analysis":
            fields.add("reference")
            reference = analysis_service.capture_analysis_reference(task.checkpoint["rich_analysis"])
            _require(_hash(reference.as_dict()) == business["reference"])
            await analysis_service.require_batch_analysis_stream_rows(db, task, reference)
    elif kind in _SINGLE and running:
        fields.add("chapter")
        scene_chapter_id = await _check_chapter(db, task, task.input["_chapter_claim"], business["chapter"])
        _require(scene_chapter_id == task.chapter_id)
    elif kind == "scene.restructure":
        scene_chapter_id = task.chapter_id
        _require(type(scene_chapter_id) is int)
    elif kind == "chapter.analyze" and running:
        fields.add("pending")
        _require("_analysis_completion" not in task.checkpoint
                 and _hash(task.checkpoint["_analysis_pending"]) == business["pending"])
        if "_analysis_proposal_source" in task.input:
            from app.services.rich_document_review import require_applied_analysis_stream_rows

            await require_applied_analysis_stream_rows(db, task)
    elif kind in _ENTITY:
        fields.add("groups")
        groups = _entity_groups(business["groups"], kind)
        await entities._require_source_groups(db, task.project_id, groups, for_update=False, allow_changes=True)
    elif kind in local._WIZARD_KINDS:
        from app.schemas.chapter import WizardRequest

        fields.update(("branch", "resume", "resume_epoch"))
        branch, expected_epoch = business["branch"], business["resume_epoch"]
        _require(branch in {"pending", "completed"})
        binding = wizard_service.parse_resume_binding(
            task.input, WizardRequest.model_validate(task.input["request"]), user_id=task.user_id,
            project_id=task.project_id, kind=kind.removeprefix("wizard."), grant_identity=grant,
        )
        _require(business["resume"] == (_hash(asdict(binding)) if binding else None))
        if binding is None or branch == "pending":
            _require(expected_epoch is None)
        else:
            _require(type(expected_epoch) is int and expected_epoch >= 0)
        if binding is not None:
            await wizard_service.verify_resume_source(db, binding, for_update=True, expected_epoch=expected_epoch)
    elif kind == "agent.turn":
        cid = task.input["request"]["conversation_id"]
        _require(type(cid) is int and cid > 0 and task.resource_key == f"agent:{cid}"
                 and type(task.input["history_epoch"]) is int and task.input["history_epoch"] == epoch)
        await local.require_agent_stream_conversation(db, task.user_id, task.project_id, cid, grant, epoch)
    if scene_chapter_id is not None:
        fields.add("scene")
        actual = await scene_ai_context.read_stamp(db, task.project_id, scene_chapter_id)
        _require(_hash(actual.log_data()) == business["scene"])
    _require(set(business) == fields)


async def verify(
    db: AsyncSession, task: Task, project: Project, *, source, event, data, conditions,
    cipher=None, allow_private=False,
) -> bool:
    """只认可已认证帧的封闭字段；任何选项、查询或绑定异常均在交付业务内容前失败关闭。"""
    if source in _NO_PUBLICATION:
        return type(conditions) is dict and not conditions
    try:
        _require(source == "publication" and type(conditions) is dict
                 and set(conditions) == {"kind", "token", "visibility", "models", "business"}
                 and conditions["kind"] == task.kind and task.kind in _SUPPORTED and project.id == task.project_id
                 and task.status in _TERMINAL | {"running"} and type(allow_private) is bool)
        keys = {"task_id": task.id, "task_key": task.statistics_key}
        await require_task_stream_physical_lease(db, task.user_id, project.id, **keys)
        with db.no_autoflush:
            token = conditions["token"]
            _require(type(token) is int and type(task.input.get("_task_actor_token_version")) is int
                     and token == task.input["_task_actor_token_version"])
            await require_authenticated_for_send(db, AuthenticatedActor(task.user_id, token))
            stamp = await ai_dispatch.read_visibility_stamp(db, project.id)
            _require(conditions["visibility"] == _stamp_hash(stamp))
            primary, extra = _read_models(task, project)
            _require(conditions["models"] == _models_hash(primary, extra))
            grant = task_grant_identity(task.input)
            _require(grant == primary.grant_identity)
            await project_access.require_project(db, task.user_id, project.id, "ai.use", grant_identity=grant)
            if task.status == "running":
                await require_live_worker(db, task.worker_key, read_only=True)
            await _check_business(db, task, conditions["business"], grant, stamp.epoch)
            await local.require_model_bindings(db, primary, extra, cipher=cipher, allow_private=allow_private)
        await require_task_stream_physical_lease(db, task.user_id, project.id, **keys)
        return True
    except Exception:
        return False
