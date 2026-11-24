"""为一个原始 HTTP 订阅选择执行者，并在内部连接消失后跟随其持久状态。"""

import asyncio
import logging
from contextlib import aclosing, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC
from time import monotonic

import anyio
from sqlalchemy import and_, select

from app.core.errors import NotFoundError
from app.models import Project, ProjectCollaborator, Task, TaskWorker, User
from app.models.entities import ACTIVE_TASK_STATUSES
from app.services import authorization_service, task_service
from app.services.collaboration_task_access import owner_ai_allowed, task_grant_identity, task_requires_owner_ai
from app.services.task_engine import workers
from app.services.task_engine.peer_protocol import PeerUnavailable
from app.services.task_engine.stream_evidence import stream_kind_open
from app.services.task_engine.stream_lease import _capture_stream_binding

logger = logging.getLogger(__name__)
_END = {"done", "error", "paused"}


def _time(value):
    if value is None:
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


def _binding_fields(binding):
    return {
        "actor": binding.actor.id, "token_version": binding.actor.token_version,
        "account_created": _time(binding.actor.editor_created_at),
        "task_id": binding.task_id, "task_key": binding.task_key, "queued_at": _time(binding.queued_at),
        "project_id": binding.project_id, "owner_id": binding.owner_id,
        "last_deleted": _time(binding.project_last_deleted_at),
        "member_grant": list(binding.member_grant) if binding.member_grant else None,
        "task_grant": list(binding.task_grant) if binding.task_grant else None,
        "active": binding.active_grant_required,
    }


def _events(engine, binding, **options):
    return engine.subscribe(
        binding.task_id, statistics_key=binding.task_key, actor_id=binding.actor.id,
        token_version=binding.actor.token_version, active_grant_required=binding.active_grant_required,
        initial_project_id=binding.project_id, initial_queued_at=binding.queued_at,
        initial_grant=binding.task_grant, guarded=binding.member_grant is not None, **options,
    )


def _frame(binding, event, data, proof=None):
    return (event, data, proof) if binding.member_grant is not None else (event, data)


def _denied(binding):
    return _frame(binding, "error", {"task": None, "detail": "任务访问权限已失效", "code": "permission_denied"})


async def _lost(engine, binding):
    if binding.project_id is None or binding.member_grant is not None or binding.actor.id != binding.owner_id:
        return _denied(binding)
    from app.services.task_engine.engine import deletion_cancel_event

    async with engine.ctx.sessionmaker() as db:
        result = (await db.execute(select(Project, Task).join(User, User.id == Project.user_id).outerjoin(Task, and_(
            Task.project_id == Project.id, Task.user_id == User.id, Task.id == binding.task_id,
            Task.statistics_key == binding.task_key, Task.queued_at == binding.queued_at, Task.status == "cancelled",
        )).where(
            Project.id == binding.project_id, Project.user_id == binding.actor.id,
            User.is_active.is_(True), User.token_version == binding.actor.token_version,
            User.created_at == binding.actor.editor_created_at,
            (Project.deleted_at.is_not(None) | Project.deletion_started_at.is_not(None)),
        ))).one_or_none()
        if result is None:
            return _denied(binding)
        project, cancelled = result
        if (project.deletion_started_at is not None and project.last_deleted_at == binding.project_last_deleted_at
                and cancelled is not None and task_grant_identity(cancelled.input) == binding.task_grant):
            return deletion_cancel_event(cancelled)
    return _frame(binding, "error", {"task": None, "detail": "任务不存在"})


async def _bound_task(engine, db, binding):
    """只按原账号、项目生命周期、邀请和入队轮次读取，不能把重邀或续跑接到旧流。"""
    db.info["authenticated_actor"] = binding.actor
    row = await engine._visible_task(
        db, binding.task_id, statistics_key=binding.task_key, actor_id=binding.actor.id,
        token_version=binding.actor.token_version, active_grant_required=binding.active_grant_required,
    )
    if (row is None or row.project_id != binding.project_id or row.queued_at != binding.queued_at
            or task_grant_identity(row.input) != binding.task_grant):
        return None
    if binding.actor.editor_created_at is not None:
        created = await db.scalar(select(User.created_at).where(User.id == binding.actor.id))
        if _time(created) != _time(binding.actor.editor_created_at):
            return None
    if binding.project_id is None:
        return row if binding.member_grant is None else None
    project = await db.get(Project, binding.project_id)
    if (project is None or project.deleted_at is not None or project.deletion_started_at is not None
            or project.last_deleted_at != binding.project_last_deleted_at or project.user_id != binding.owner_id):
        return None
    if binding.member_grant is None:
        return row if project.user_id == binding.actor.id else None
    invitation, revision = binding.member_grant
    role = await db.scalar(select(ProjectCollaborator.role).where(
        ProjectCollaborator.project_id == binding.project_id, ProjectCollaborator.user_id == binding.actor.id,
        ProjectCollaborator.status == "accepted", ProjectCollaborator.invitation_id == invitation,
        ProjectCollaborator.revision == revision,
    ))
    if role is None or (binding.active_grant_required and (
        role != "editor" or (task_requires_owner_ai(row.input)
                             and not await owner_ai_allowed(db, binding.project_id, binding.actor.id))
    )):
        return None
    return row


async def _read(engine, binding):
    async with engine.ctx.sessionmaker() as db:
        return await _bound_task(engine, db, binding)


def _owns(engine, row):
    return engine.live_output_hub(
        row.id, task_key=row.statistics_key, status=row.status, queued_at=row.queued_at,
        execution_key=row.execution_key, worker_key=row.worker_key,
    ) is not None


def _claiming(engine, row):
    # 领取已提交、现场尚未登记的很短窗口。只认本机调度代次及其实际任务协程。
    runner = engine._running.get(row.id)
    return (row.status == "running" and row.execution_key is not None and engine.lease.valid
            and row.worker_key == engine.lease.worker_key and row.id in engine._claiming
            and engine._running_statistics_keys.get(row.id) == row.statistics_key
            and engine._running_rounds.get(row.id) == row.queued_at and runner is not None and not runner.done())


async def _claim_handoff(engine, row, worker, execution):
    if _claiming(engine, row) and row.worker_key == worker:
        # 两个实例可能在领取提交后同时发现行；等原领取者安装缓存，不凭 running 行构造前缀。
        hub, _ = engine.ensure_hub(row.id, statistics_key=row.statistics_key, status=row.status,
                                   queued_at=row.queued_at, execution_key=row.execution_key)
        await engine._await_claim_source(hub)
    return engine.peer_handoffs.find(row.id, row.statistics_key, row.queued_at,
                                     worker_key=worker, execution_key=execution)


async def _other_worker_exists(engine):
    """旧版或监听绑定失败的存活实例也可能领取正在排队的任务。"""
    own = [key for key in (engine.lease.worker_key, engine.request_worker.worker_key) if key is not None]
    async with engine.ctx.sessionmaker() as db:
        candidates = await db.scalars(select(TaskWorker.worker_key).where(
            TaskWorker.worker_key.not_in(own), TaskWorker.fenced_at.is_(None), TaskWorker.stopped_at.is_(None),
        ).order_by(TaskWorker.started_at.desc()))
        for key in candidates:
            if await workers.lock_live_worker(db, key, engine.lease_seconds, read_only=True):
                return True
        return False


async def _binding_at_source(engine, fields):
    # 重建原读者的真实数据库绑定。网络数据中的角色/授权/账号创建时间仅用来比对。
    if type(fields) is not dict or not all(type(fields.get(k)) is int for k in ("actor", "task_id", "token_version")):
        raise PeerUnavailable()
    if type(fields.get("active")) is not bool or type(fields.get("task_key")) is not str:
        raise PeerUnavailable()
    async with engine.ctx.sessionmaker() as db:
        user = await db.get(User, fields["actor"])
        if user is None or not user.is_active or user.token_version != fields["token_version"]:
            raise PeerUnavailable()
        actor = authorization_service.AuthenticatedActor(user.id, user.token_version, user.created_at)
        db.info["authenticated_actor"] = actor
        row = await task_service.get_owned_task(db, actor.id, fields["task_id"], expected_task_key=fields["task_key"])
        binding = await _capture_stream_binding(db, row, actor)
        if (not fields["active"] and binding.active_grant_required) or (
            binding.member_grant is not None and not stream_kind_open(row.kind)
        ):
            raise PeerUnavailable()
        binding = replace(binding, active_grant_required=fields["active"])
        if _binding_fields(binding) != fields or await _bound_task(engine, db, binding) is None:
            raise PeerUnavailable()
        return binding, row


async def serve_subscription(engine, payload, channel):
    """A 只允许直连本代执行现场，包含已结束短任务的同轮交接。"""
    if type(payload) is not dict or set(payload) != {"binding", "execution_key", "handoff"}:
        raise PeerUnavailable()
    if (type(payload["handoff"]) is not bool or
            (payload["execution_key"] is not None and type(payload["execution_key"]) is not str)):
        raise PeerUnavailable()
    if not engine.lease.valid or channel.target_key != engine.lease.worker_key:
        return
    binding, row = await _binding_at_source(engine, payload["binding"])
    execution_key = payload["execution_key"]
    options = {"source_worker": channel.target_key, "source_execution": execution_key,
               "peer_owner_projection": binding.member_grant is None and binding.project_id is not None}
    if payload["handoff"]:
        handoff = await _claim_handoff(engine, row, channel.target_key, execution_key)
        if handoff is None:
            return
        options["handoff"] = handoff
    elif (execution_key is None or row.execution_key != execution_key
          or not (_owns(engine, row) or _claiming(engine, row))):
        return

    async def write(public_event, public_data, proof):
        # PeerPublisher 在重核时要求原序号及原公开内容；拒绝替换时 B 会自行发固定错误。
        await channel.send({"event": public_event, "data": public_data, "proof": proof})

    async with aclosing(_events(engine, binding, **options)) as events:
        async for item in events:
            event, data = item[:2]
            if not engine.lease.valid or engine.lease.worker_key != channel.target_key:
                return
            while True:
                if binding.member_grant is not None and event and not (
                    event == "error" and item[2] is None
                    and data in ({"task": None, "detail": "任务不存在"}, _denied(binding)[1])
                ):
                    from app.services.task_engine.stream_lease import send_peer_source_frame

                    outcome = await send_peer_source_frame(
                        engine.ctx.sessionmaker, binding, event, data, item[2], write, source_worker=channel.target_key,
                    )
                    if outcome == "abort":
                        raise PeerUnavailable()
                    if outcome == "error":
                        await channel.confirmation()
                        return
                else:
                    proof = None
                    if binding.member_grant is None and len(item) == 3:
                        async with engine.ctx.sessionmaker() as db:
                            row = await _bound_task(engine, db, binding)
                            if row is None:
                                raise NotFoundError("任务不存在")
                            proof = await _owner_projection(db, engine, binding, row, event, data, raw=item[2])
                        if proof is None:
                            break
                    await write(event, data, proof)
                if await channel.confirmation() == "ack":
                    break
                if binding.member_grant is None or not event:
                    raise PeerUnavailable()


async def _discard(pending):
    if pending is not None:
        pending.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(pending, return_exceptions=True)


def _watch_member(engine, binding):
    if binding.member_grant is None:
        return nullcontext(None)
    return engine.peer_wakes.watch_member(binding.project_id, binding.actor.id)


@dataclass
class _SourceWatch:
    worker: str
    execution: str | None
    handoff: bool
    member: asyncio.Event | None

    def check(self, row):
        if row.status == "queued":
            raise PeerUnavailable()
        if row.status == "running" and (
            row.recovered_at is not None or row.worker_key != self.worker
            or (row.execution_key != self.execution and not (self.handoff and self.execution is None))
        ):
            raise PeerUnavailable()


async def _receive(engine, binding, stream, watch):
    """定时或成员变更时重验，即使对端只发心跳或一直静默；不取消仍在读的半帧。"""
    pending = asyncio.create_task(stream.receive())
    wake = watch.member
    changed = asyncio.create_task(wake.wait()) if wake is not None else None
    next_check = monotonic() + engine.poll_interval
    try:
        while True:
            waiters = (pending, changed) if changed is not None else (pending,)
            ready, _ = await asyncio.wait(waiters, timeout=max(0.0, next_check - monotonic()),
                                          return_when=asyncio.FIRST_COMPLETED)
            if pending in ready:
                return pending.result()
            row = await _read(engine, binding)
            if row is None:
                raise NotFoundError("任务不存在")
            watch.check(row)
            if monotonic() >= next_check:
                if pending.done():
                    # 状态短读期间原执行者的末尾业务帧可能已经就绪，优先交还本连接。
                    return pending.result()
                if row.status not in ACTIVE_TASK_STATUSES:
                    # 认证连接持续静默且已有库内结局。额外的成员提示不能提前截断现场。
                    raise PeerUnavailable()
                next_check = monotonic() + engine.poll_interval
            if changed is not None and changed in ready:
                wake.clear()
                changed = asyncio.create_task(wake.wait())
    finally:
        await _discard(changed)
        await _discard(pending)


async def _owner_projection(db, engine, binding, row, event, data, *, raw=None, proof=None):
    """两端都核对当前可见引用。原件在 A 重投影；公开帧已去除私人凭据，不能作为原件再过滤。"""
    from app.services.personal_projection import project_payload, stream_reference_digest
    from app.services.task_engine.stream_evidence import (
        finalize_projected_frame,
        rich_review_of,
        rich_review_pending_conditions,
        rich_review_shape_ok,
    )

    digest = await stream_reference_digest(db, binding.actor.id, row.project_id, data, is_owner=True)
    if raw is None and proof != {"references": digest}:
        return None
    shown = await project_payload(db, binding.actor.id, row.project_id, raw) if raw is not None else dict(data)
    if event in ("proposal", "executed"):
        from app.models import AgentChange
        from app.services.agent.service import public_changes

        original = data.get("change")
        if not isinstance(original, dict):
            return None
        current = await db.scalar(select(AgentChange).where(
            AgentChange.id == original.get("id"), AgentChange.change_key == original.get("change_key"),
            AgentChange.project_id == row.project_id, AgentChange.turn_task_id == row.id,
            AgentChange.turn_task_key == row.statistics_key,
        ))
        if current is None:
            return None
        projected = (await public_changes(db, binding.actor.id, row.project_id, [current]))[0].model_dump(mode="json")
        if original != projected:
            return None
        shown["change"] = projected
    review = rich_review_of(event, data)
    if review is not None:
        from app.models import SceneReviewProposal

        if not rich_review_shape_ok(review, binding.task_key):
            return None
        current = await db.scalar(select(Task.id).join(
            SceneReviewProposal, SceneReviewProposal.task_id == Task.id,
        ).where(*rich_review_pending_conditions(
            task_id=row.id, task_key=binding.task_key, project_id=row.project_id,
            queued_at=binding.queued_at, review=review,
        )))
        if current is None:
            return None
    if await _bound_task(engine, db, binding) is None:
        raise NotFoundError("任务不存在")
    if not finalize_projected_frame(event, shown, task_id=row.id, task_key=binding.task_key, original_review=review):
        return None
    return {"references": digest} if shown == data else None


async def _remote(engine, binding, target, execution_key, handoff, discovery_deadline):
    payload = {"binding": _binding_fields(binding), "execution_key": execution_key, "handoff": handoff}
    async with (asyncio.timeout_at(discovery_deadline) as lookup, engine.peer.subscribe(target, payload) as stream):
        with _watch_member(engine, binding) as wake:
            watch = _SourceWatch(target.worker_key, execution_key, handoff, wake)
            while True:
                frame = await _receive(engine, binding, stream, watch)
                if frame is None:
                    return
                if lookup.when() is not None:
                    # 收到第一个认证帧即结束发现预算，后续活流按各帧自己的截止时间处理。
                    lookup.reschedule(None)
                if not frame.event or binding.member_grant is None:
                    publish = True
                    async with engine.ctx.sessionmaker() as db:
                        row = await _bound_task(engine, db, binding)
                        if row is None:
                            raise NotFoundError("任务不存在")
                        watch.check(row)
                        if not frame.event and row.status not in ACTIVE_TASK_STATUSES:
                            raise PeerUnavailable()
                        if row.project_id is not None and frame.event and not (
                            frame.event == "error" and frame.data in (_denied(binding)[1],
                                                                     {"task": None, "detail": "任务不存在"})
                        ):
                            publish = await _owner_projection(db, engine, binding, row, frame.event, frame.data,
                                                              proof=frame.proof) is not None
                    if not publish:
                        await stream.ack()
                        continue
                    event = _frame(binding, frame.event, frame.data)
                else:
                    from app.services.task_engine.peer_guard import received_frame

                    event = _frame(binding, frame.event, frame.data,
                                   received_frame(frame, ctx=engine.ctx, subscriber=stream))
                yield event
                await stream.ack()
                if frame.event in _END:
                    return


async def _stored(engine, binding):
    """重复短读，终态回放复用原投影流程；协作者运行中文本缺少现场证据。"""
    last = None
    last_heartbeat = monotonic()
    while True:
        row = await _read(engine, binding)
        if row is None:
            raise NotFoundError("任务不存在")
        if row.status == "running":
            if binding.member_grant is not None:
                yield _denied(binding)
                return
            async with engine.ctx.sessionmaker() as db:
                if await task_service.result_unproven(db, row):
                    yield _frame(binding, "error", {"task": None, "detail": "任务保存结果尚未确认，请稍后核对"})
                    return
        async with aclosing(_events(engine, binding, db_snapshot_only=True)) as snapshots:
            async for item in snapshots:
                if item[0] == "snapshot":
                    if row.status in ACTIVE_TASK_STATUSES and last == item[1]:
                        continue
                    last = item[1]
                yield item
                if item[0] in _END:
                    return
        if monotonic() - last_heartbeat >= engine.heartbeat_seconds:
            yield _frame(binding, "", {})
            last_heartbeat = monotonic()
        await asyncio.sleep(min(engine.flush_interval, engine.poll_interval, engine.heartbeat_seconds))


async def _handoff_targets(engine, row):
    if row.worker_key:
        target = await engine.peer.target(row.worker_key)
        if target is not None:
            return [target]
        if row.status == "running":
            return []
    return await engine.peer.targets(all_generations=True)


async def _routed(engine, binding):
    row = await _read(engine, binding)
    if row is None:
        raise NotFoundError("任务不存在")
    # 同一库仅有本机执行的普通订阅走原始本地生成器（测试直接启停引擎时也适用）。
    if row.status not in ACTIVE_TASK_STATUSES:
        async with aclosing(_events(engine, binding, db_snapshot_only=True)) as events:
            async for item in events:
                yield item
        return
    if _owns(engine, row) or _claiming(engine, row):
        try:
            async with aclosing(_events(engine, binding, source_worker=row.worker_key,
                                        source_execution=row.execution_key)) as events:
                async for item in events:
                    yield item
            return
        except PeerUnavailable:
            row = await _read(engine, binding)
            if row is None:
                raise NotFoundError("任务不存在") from None
    queued_seen = False
    if row.status == "queued" and not await _other_worker_exists(engine):
        try:
            async with aclosing(_events(engine, binding, follow_local_queue=True)) as events:
                async for item in events:
                    if item[0] == "snapshot" and (item[1].get("task") or {}).get("status") == "queued":
                        queued_seen = True
                    yield item
            return
        except PeerUnavailable:
            row = await _read(engine, binding)
            if row is None:
                raise NotFoundError("任务不存在") from None
    previous = None
    ping_at = monotonic()
    while row.status == "queued":
        async with aclosing(_events(engine, binding, db_snapshot_only=True)) as snapshots:
            async for item in snapshots:
                if item[0] != "snapshot":
                    # 在读排队行之后已完成，先按原排队轮次查交接，再由持久回放收尾。
                    if item[0] == "error" and item[1].get("task") is None:
                        yield item
                        return
                    break
                if (item[1].get("task") or {}).get("status") == "queued":
                    if previous != item[1]:
                        previous = item[1]
                        queued_seen = True
                        yield item
        if monotonic() - ping_at >= engine.heartbeat_seconds:
            yield _frame(binding, "", {})
            ping_at = monotonic()
        await asyncio.sleep(min(0.2, engine.poll_interval, engine.heartbeat_seconds))
        row = await _read(engine, binding)
        if row is None:
            raise NotFoundError("任务不存在")
    if queued_seen and engine.lease.valid:
        entry = await _claim_handoff(engine, row, engine.lease.worker_key, row.execution_key)
        if entry is not None:
            from app.services.task_engine.peer_handoff import HandoffUnavailable

            try:
                async with aclosing(_events(engine, binding, handoff=entry)) as events:
                    async for item in events:
                        yield item
                        if item[0] in _END:
                            return
            except HandoffUnavailable:
                pass
    if row.status == "running" or queued_seen:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + engine.peer_handoffs.retention_seconds if row.status != "running" else None
        try:
            async with asyncio.timeout_at(deadline):
                targets = await _handoff_targets(engine, row)
        except TimeoutError:
            targets = []
        for target in targets:
            if deadline is not None and loop.time() >= deadline:
                break
            received = finished = False
            try:
                remote = _remote(engine, binding, target, row.execution_key, queued_seen, deadline)
                async with aclosing(remote) as events:
                    async for item in events:
                        received = True
                        finished = item[0] in _END
                        yield item
                    # 恢复 _remote 到正常退出才会在最后一次真实发送后 ACK，不重复回放已公开的终态。
                    if finished:
                        return
                    if received:
                        break
            except (PeerUnavailable, TimeoutError, OSError, EOFError):
                if finished:
                    return
                # 只有握手/首帧前的交接搜索可尝试其他候选。已交付的前缀绝不能再播。
                if received or not queued_seen:
                    break
    async with aclosing(_stored(engine, binding)) as events:
        async for item in events:
            yield item


async def subscribe(engine, binding):
    try:
        async with aclosing(_routed(engine, binding)) as events:
            async for item in events:
                if (item[0] == "error" and binding.member_grant is None and binding.project_id is not None
                        and item[1] in (_denied(binding)[1], {"task": None, "detail": "任务不存在"})):
                    # 删除可在原生生成器的最后一次读取期间开始，此时也重查原作者的结局。
                    item = await _lost(engine, binding)
                yield item
    except NotFoundError:
        yield await _lost(engine, binding)
    except Exception as exc:
        logger.warning("任务订阅关闭 error_type=%s", type(exc).__name__)
        yield _denied(binding)
