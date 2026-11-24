"""执行者短期保留自领取开始的原事件，供已看到排队快照的读者无缝接入。"""

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from itertools import chain
from sys import getsizeof
from typing import Any

from app.core.security import SecretCipher
from app.models import Task
from app.models.entities import FINISHED_TASK_STATUSES
from app.services.task_engine.hub import SUBSCRIBER_QUEUE_LIMIT, Subscriber, TaskHub
from app.services.task_engine.stream_evidence import capture_task_view, registered_terminal


class HandoffUnavailable(RuntimeError):
    """缺少完整的原轮次前缀，订阅方应按原身份走库内降级。"""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _retained_size(value: Any, limit: int, seen: set[int], depth: int = 0) -> int:
    """估算本轮额外保活的数据，包括执行输入与 guard。超预算或未知的对象图不留存。"""
    size = getsizeof(value)
    if id(value) in seen:
        return 0
    if depth > 64 or size > limit:
        raise HandoffUnavailable()
    seen.add(id(value))
    if type(value) in (str, bytes, int, float, bool, type(None), datetime) or type(value) is SecretCipher:
        # cipher 是应用生命周期内共享的实例；保留一个既有实例引用不额外延长其内部对象生命周期。
        return size
    if isinstance(value, dict):
        children = chain.from_iterable(value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        children = iter(value)
    elif is_dataclass(value) and not isinstance(value, type):
        children = (getattr(value, member.name) for member in fields(value))
        if hasattr(value, "__dict__"):
            children = chain(children, (vars(value),))
    else:
        raise HandoffUnavailable()
    for child in children:
        size += _retained_size(child, limit - size, seen, depth + 1)
    return size


@dataclass(eq=False, slots=True, repr=False)
class ClaimHandoff:
    hub: TaskHub
    worker_key: str
    execution_key: str
    deadline: float
    frames: list[tuple[str, dict, Any]] = field(default_factory=list)
    retained_bytes: int = 0
    expiry: asyncio.TimerHandle | None = None

    def matches_row(self, row: Task) -> bool:
        """接管者、另一次执行及未获原 hub 证明的终态都不能使用此前缀。"""
        if (row.id != self.hub.task_id or row.statistics_key != self.hub.statistics_key
                or row.queued_at is None or _utc(row.queued_at) != _utc(self.hub.queued_at)):
            return False
        if row.status == "running":
            return (not self.hub.closed and row.worker_key == self.worker_key
                    and row.execution_key == self.execution_key and row.recovered_at is None)
        terminal = self.hub.terminal_source
        return (row.status in (*FINISHED_TASK_STATUSES, "paused") and self.hub.closed
                and row.worker_key in (None, self.worker_key) and row.execution_key in (None, self.execution_key)
                and registered_terminal(terminal) and terminal.execution.worker_key == self.worker_key
                and terminal.execution.execution_key == self.execution_key and terminal.view == capture_task_view(row))


class PeerHandoffs:
    # 32 个并发轮次，每个至多 2 MiB；到期的轮次无需再有请求即可释放。
    max_rounds = 32
    max_round_bytes = 2 * 1024 * 1024
    max_frames = min(512, SUBSCRIBER_QUEUE_LIMIT)
    retention_seconds = 15.0

    def __init__(self):
        self._rounds: OrderedDict[tuple[int, str, datetime], ClaimHandoff] = OrderedDict()

    def begin(self, hub: TaskHub, worker_key: str) -> None:
        if not hub.statistics_key or not hub.execution_key or hub.queued_at is None or hub.closed:
            return
        key = (hub.task_id, hub.statistics_key, _utc(hub.queued_at))
        if key in self._rounds:
            self._drop(key)
        while len(self._rounds) >= self.max_rounds:
            self._drop(next(iter(self._rounds)))
        loop = asyncio.get_running_loop()
        claim = ClaimHandoff(hub, worker_key, hub.execution_key, loop.time() + self.retention_seconds)
        self._rounds[key] = claim
        claim.expiry = loop.call_at(claim.deadline, self._drop, key)
        hub.capture_handoff = lambda event, data: self._append(key, claim, event, data)

    def _drop(self, key: tuple[int, str, datetime]) -> None:
        claim = self._rounds.pop(key, None)
        if claim is not None:
            claim.hub.capture_handoff = None
            claim.frames.clear()
            if claim.expiry is not None:
                claim.expiry.cancel()

    def clear(self) -> None:
        for key in tuple(self._rounds):
            self._drop(key)

    def _append(self, key, claim: ClaimHandoff, event: str, data: dict) -> None:
        if len(claim.frames) >= self.max_frames or asyncio.get_running_loop().time() >= claim.deadline:
            self._drop(key)
            return
        hub = claim.hub
        # 来源在普通 delta 之间保持原对象。仅在来源变化时核算其输入、断点、模型与投影凭据。
        source = hub.source
        cost = getsizeof((event, data, source)) + getsizeof(event) + 16
        remaining = self.max_round_bytes - claim.retained_bytes - getsizeof(hub.text) - cost
        try:
            source_size = 0
            if not claim.frames or source is not claim.frames[-1][2]:
                source_size = _retained_size(source, remaining, set())
            data_size = _retained_size(data, remaining - source_size, set())
            if hub.closed:
                _retained_size((hub.paused, hub.result, hub.terminal_source, hub.text_source),
                               remaining - source_size - data_size, set())
        except HandoffUnavailable:
            self._drop(key)
            return
        claim.frames.append((event, deepcopy(data), source))
        claim.retained_bytes += cost + source_size + data_size

    def find(
        self, task_id: int, task_key: str, queued_at: datetime, *, worker_key: str, execution_key: str | None = None,
    ) -> ClaimHandoff | None:
        if queued_at is None:
            return None
        key = (task_id, task_key, _utc(queued_at))
        claim = self._rounds.get(key)
        if claim is None:
            return None
        if asyncio.get_running_loop().time() >= claim.deadline:
            self._drop(key)
            return None
        if claim.worker_key != worker_key or (execution_key is not None and execution_key != claim.execution_key):
            return None
        return claim

    def attach(self, claim: ClaimHandoff, row: Task, *, worker_key: str, guarded: bool) -> Subscriber:
        """同一无 await 临界区中取序号 1..n 的原前缀并接入从 n+1 开始的实时队列。"""
        if (self.find(row.id, row.statistics_key, row.queued_at, worker_key=worker_key) is not claim
                or not claim.matches_row(row)):
            raise HandoffUnavailable()
        hub = claim.hub
        sub = Subscriber(guarded=guarded) if hub.closed else hub.subscribe(guarded=guarded)[1]
        for event, data, source in claim.frames:
            sub.queue.put_nowait((event, data, source) if guarded else (event, data))
        return sub
