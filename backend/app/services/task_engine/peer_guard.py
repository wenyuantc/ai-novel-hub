"""跨进程受控帧的两次独立读核验。传输证明不能转换为本地执行或写入权限。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.errors import NotFoundError
from app.models import AgentChange, Project, Task
from app.models.entities import FINISHED_TASK_STATUSES
from app.services.personal_projection import stream_reference_digest
from app.services.task_engine import stream_evidence as local
from app.services.task_engine.execution_identity import require_live_worker
from app.services.task_engine.peer_protocol import PeerFrame, PeerSubscriber, is_authenticated_frame

if TYPE_CHECKING:
    from app.services.task_engine.registry import TaskContext
    from app.services.task_engine.stream_lease import _StreamBinding

_PROOF_FIELDS = {"version", "binding", "frame", "source", "origin_worker", "origin_execution", "phase",
                 "task_state", "visibility", "business", "view", "projection"}


def _deny() -> NotFoundError:
    return NotFoundError("任务不存在")


class PeerFrameAdvanced(Exception):
    """同一执行可能合法推进；只能锁外重新请求 A 核验之前保留的原帧。"""


@dataclass(frozen=True, slots=True, repr=False)
class ReceivedFrame:
    packet: PeerFrame
    ctx: TaskContext
    subscriber: PeerSubscriber

    async def refresh(self) -> ReceivedFrame:
        old = self.packet
        # recheck 后协议层即撤销旧的能力。保存已认证原帧的身份和数据摘要供比较。
        if not self.current():
            raise _deny()
        expected = _packet_identity(old)
        await self.subscriber.recheck()
        new = await self.subscriber.receive()
        if not is_authenticated_frame(new) or expected != _packet_identity(new):
            raise _deny()
        return received_frame(new, ctx=self.ctx, subscriber=self.subscriber)

    def current(self) -> bool:
        return (self.ctx is not None and type(self.subscriber) is PeerSubscriber
                and self.subscriber.pending is self.packet
                and is_authenticated_frame(self.packet, ctx=self.ctx))


def _packet_identity(packet: PeerFrame) -> tuple:
    return (packet.source_key, packet.target_key, packet.subscription_id, packet.sequence,
            local._digest([packet.request, packet.event, packet.data]))


def received_frame(packet: PeerFrame, *, ctx: TaskContext, subscriber: PeerSubscriber) -> ReceivedFrame:
    """只用来自当前已认证连接的完整未确认帧；B 写出时还要再验。"""
    result = ReceivedFrame(packet, ctx, subscriber)
    if not result.current():
        raise _deny()
    return result


def _binding_hash(binding: _StreamBinding) -> str:
    from app.services.task_engine.peer_stream import _binding_fields

    return local._digest(_binding_fields(binding))


def _visibility(project: Project) -> list:
    return [project.id, project.ai_visibility_revision, project.ai_history_epoch, local._time(project.last_deleted_at)]


def _state(task: Task) -> str:
    if task.status in FINISHED_TASK_STATUSES or task.status == "paused":
        return local.capture_task_view(task).fingerprint
    # 活动输出会节流更新。仅核对决定领取/读权限的字段和公开身份，排除普通进度和正文。
    return local._digest([
        task.id, task.statistics_key, task.user_id, task.project_id, task.chapter_id, task.kind,
        task.title, local._time(task.created_at), task.resource_key, task.serial_key, task.retry_of_id,
        local._time(task.queued_at), task.worker_key, task.execution_key, task.status,
        task.step_cursor, task.input, task.checkpoint, task.admin_cancel_operation_id,
        local._time(task.admin_cancel_requested_at), local._time(task.recovered_at),
    ])


async def capture(db, binding, task, project, event, data, evidence, *, source_worker: str) -> dict:
    if (binding.member_grant is None or project is None or not local.stream_kind_open(task.kind)
            or not source_worker or task.status == "running" and task.worker_key != source_worker):
        raise _deny()
    await require_live_worker(db, source_worker, read_only=True)
    selected = []
    if not await local.verify_stream_frame(db, task, project, event, data, evidence, selected=selected):
        raise _deny()
    guard = None
    execution = None
    origin = evidence.source
    if type(origin) is local.PublicationSource:
        if len(selected) != 1:
            raise _deny()
        source = "publication"
        execution, guard = selected[0].execution, selected[0].guard
    elif type(origin) is local.ClaimSource:
        source, execution = "claim", origin.execution
    elif type(origin) is local.QueuedSource:
        source = "queued"
    elif type(origin) is local.TaskViewEvidence:
        source = "view"
    else:
        raise _deny()
    if execution is not None and (execution.worker_key != source_worker or not execution.execution_key):
        raise _deny()
    business = {}
    if source == "publication":
        from app.services.task_engine import peer_predicates

        business = await peer_predicates.capture(db, task, project, source=source, event=event, data=data, guard=guard)
    projection = None
    if source != "view":
        references = await stream_reference_digest(db, task.user_id, project.id, data, is_owner=False)
        review = local.rich_review_of(event, data)
        public_hash = local._digest(data)
        second_hash = await _projected_hash(db, task, project, event, data, data, review)
        if second_hash != public_hash:
            # 私人效果的一次投影会去掉实例凭据。只有用本帧原件在 A 锁内再次投影完全通过，
            # 才允许 B 用同一公开视图摘要及同一批可见引用实例证明这段正文。
            original = local.projection_input(evidence)
            if (original is None or
                    await _projected_hash(db, task, project, event, data, original, review) != public_hash):
                raise _deny()
        projection = [references, second_hash]
    return {
        "version": 1, "binding": _binding_hash(binding), "frame": local._digest([event, data]), "source": source,
        "origin_worker": execution.worker_key if execution else None,
        "origin_execution": execution.execution_key if execution else None,
        "view": [origin.status, origin.fingerprint] if source == "view" else None,
        "phase": task.status, "task_state": _state(task), "visibility": _visibility(project), "business": business,
        "projection": projection,
    }


def _possible_advance(task, proof) -> bool:
    # 原绑定、账号和成员已在本次事务重验。这里只允许重新核验，绝不凭状态推移放行。
    if proof["phase"] == "queued":
        return task.status != "queued"
    if proof["phase"] != "running":
        return False
    if task.status != "running":
        return task.status in FINISHED_TASK_STATUSES or task.status == "paused"
    if task.worker_key != proof["origin_worker"] or task.execution_key != proof["origin_execution"]:
        return False
    if proof["source"] == "claim" or task.kind in {"chapter.batch", "agent.turn"}:
        return True
    from app.services.collaboration_ai_guard import _WIZARD_KINDS, _WIZARD_SUCCESSOR_KINDS

    if task.kind in _WIZARD_SUCCESSOR_KINDS:
        return proof["business"]["business"].get("branch") == "pending"
    if task.kind in _WIZARD_KINDS:
        return (proof["business"]["business"].get("branch") == "pending"
                and isinstance(task.checkpoint, dict) and "_wizard_completion" in task.checkpoint)
    # 单章保存/分析完成和取消意图可能先于终态提交。A 仍须等自己的终态收据后重验原来源。
    if task.admin_cancel_operation_id is not None and task.admin_cancel_requested_at is not None:
        return True
    if task.kind in {"chapter.generate", "chapter.polish", "chapter.revise"}:
        return isinstance(task.input, dict) and "_chapter_claim" not in task.input
    return task.kind == "chapter.analyze" and isinstance(task.checkpoint, dict) and (
        "_analysis_completion" in task.checkpoint and "_analysis_pending" not in task.checkpoint
    )


async def _projected_hash(db, task, project, event, data, payload, review):
    """在最终写出前核对私人引用与动态提案；已脱敏的空值不会还原成原始内容。"""
    from app.services.personal_projection import project_payload

    expected = await project_payload(db, task.user_id, project.id, payload)
    if not isinstance(expected, dict):
        raise _deny()
    if event in ("proposal", "executed") and task.kind == "agent.turn":
        from app.services.agent import service as agent_service

        if not await local._verify_agent_change_frame(db, task, event, data):
            raise _deny()
        change = await db.scalar(select(AgentChange).where(AgentChange.id == data["change"]["id"]))
        expected["change"] = (await agent_service.public_changes(
            db, task.user_id, project.id, [change],
        ))[0].model_dump(mode="json")
    if not local.finalize_projected_frame(
        event, expected, task_id=task.id, task_key=task.statistics_key, original_review=review,
    ):
        raise _deny()
    return local._digest(expected)


async def validate(db, binding, task, project, event, data, evidence: ReceivedFrame) -> None:
    packet = evidence.packet
    if not evidence.current() or packet.event != event or local._digest(packet.data) != local._digest(data):
        raise _deny()
    proof = packet.proof
    if (binding.member_grant is None or project is None or not local.stream_kind_open(task.kind)
            or type(proof) is not dict or set(proof) != _PROOF_FIELDS or type(proof["version"]) is not int
            or proof["version"] != 1 or proof["binding"] != _binding_hash(binding)
            or proof["frame"] != local._digest([event, data])):
        raise _deny()
    source = proof["source"]
    request = packet.request
    if (set(request) != {"binding", "execution_key", "handoff"} or type(request["handoff"]) is not bool
            or proof["binding"] != local._digest(request["binding"])
            or ((not request["handoff"] or request["execution_key"] is not None)
                and type(request["execution_key"]) is not str)):
        raise _deny()
    if source in ("claim", "publication"):
        if packet.source_key != proof["origin_worker"] or not proof["origin_execution"]:
            raise _deny()
        if request["execution_key"] is not None and request["execution_key"] != proof["origin_execution"]:
            raise _deny()
        if task.status == "running" and (
            task.worker_key != packet.source_key or task.execution_key != proof["origin_execution"]
        ):
            raise _deny()
    elif (source not in ("queued", "view")
          or proof["origin_worker"] is not None or proof["origin_execution"] is not None):
        raise _deny()
    await require_live_worker(db, packet.source_key, read_only=True)
    if proof["phase"] != task.status or proof["task_state"] != _state(task):
        if _possible_advance(task, proof):
            raise PeerFrameAdvanced
        raise _deny()
    if proof["visibility"] != _visibility(project):
        raise _deny()
    review = local.rich_review_of(event, data)
    if not await local._bound_task_visible(db, task, project, review) or not await local._owner_ai_current(db, task):
        raise _deny()
    if source == "queued" and not local.queued_progress_shape(event, data):
        raise _deny()
    if source == "claim" and not (local._claim_payload_shape(event, data) or (
        task.status in FINISHED_TASK_STATUSES and event == "error" and set(data) == {"task", "detail"}
    )):
        raise _deny()
    if source == "view":
        view = proof["view"]
        if not isinstance(view, list) or len(view) != 2 or any(type(field) is not str for field in view):
            raise _deny()
        # 这是来自签名包的旧行摘要，不注册为任何本地来源。是否允许该帧由 B 重新投影决定。
        expected = local.TaskViewEvidence(task.id, binding.task_key, local._time(binding.queued_at), *view)
        if not await local.verify_view_frame(db, task, project, event, data, expected):
            raise _deny()
    elif proof["view"] is not None:
        raise _deny()
    if source == "publication":
        from app.services.task_engine import peer_predicates

        if not await peer_predicates.verify(
            db, task, project, source=source, event=event, data=data, conditions=proof["business"],
            cipher=evidence.ctx.cipher, allow_private=evidence.ctx.settings.allow_private_ai_endpoints,
        ):
            raise _deny()
    elif proof["business"] != {}:
        raise _deny()
    if source != "view":
        references = await stream_reference_digest(db, task.user_id, project.id, data, is_owner=False)
        second_hash = await _projected_hash(db, task, project, event, data, data, review)
        if proof["projection"] != [references, second_hash]:
            raise _deny()
    elif proof["projection"] is not None:
        raise _deny()
    if not evidence.current():
        raise _deny()
