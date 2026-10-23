"""TaskHub：单个排队 / 运行中任务的内存状态——已输出文本缓冲与订阅者队列。

不重复也不遗漏的关键：subscribe() 在同一个同步临界区内复制缓冲区并注册订阅队列（中间没有 await），
因此快照之后收到的第一个 delta 恰好接在快照末尾。
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

# 每个订阅者最多积压的帧数；溢出时断开该订阅者，由前端重连后用快照补齐
SUBSCRIBER_QUEUE_LIMIT = 1000

Frame = tuple[str, dict[str, Any]]


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue[Frame] = field(default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT))
    # 积压溢出：消费完已入队的帧后结束本次订阅
    overflowed: bool = False


class TaskHub:
    def __init__(self, task_id: int, *, status: str = "queued", text: str = "", progress: int = 0, stage: str = ""):
        self.task_id = task_id
        self.status = status
        self.text = text
        self.progress = progress
        self.stage = stage
        self.queue_position: int | None = None
        # runner 产出的 done 载荷（结果已落库）
        self.result: dict[str, Any] | None = None
        self.closed = False
        # 自上次刷库后是否有新的输出 / 进度
        self.dirty = False
        self._subscribers: set[Subscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> tuple[str, Subscriber]:
        """同步完成快照与注册。"""
        sub = Subscriber()
        self._subscribers.add(sub)
        return self.text, sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait((event, data))
            except asyncio.QueueFull:
                sub.overflowed = True
                self._subscribers.discard(sub)

    def append(self, text: str) -> None:
        if not text:
            return
        self.text += text
        self.dirty = True
        self.publish("delta", {"text": text})

    def progress_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status, "progress": self.progress, "stage": self.stage}
        if self.status == "queued":
            payload["queue_position"] = self.queue_position
        return payload

    def set_progress(self, *, status: str | None = None, progress: int | None = None, stage: str | None = None) -> None:
        """有变化时广播 progress 事件。"""
        changed = False
        if status is not None and status != self.status:
            self.status, changed = status, True
        if progress is not None and progress != self.progress:
            self.progress, changed = progress, True
        if stage is not None and stage != self.stage:
            self.stage, changed = stage, True
        if changed:
            self.dirty = True
            self.publish("progress", self.progress_payload())

    def set_queue_position(self, position: int | None) -> None:
        if position != self.queue_position:
            self.queue_position = position
            if self.status == "queued":
                self.publish("progress", self.progress_payload())

    def close(self, event: str, data: dict[str, Any]) -> None:
        """广播终止事件（done / error）；之后不再接受订阅。"""
        if self.closed:
            return
        self.closed = True
        self.publish(event, data)
        self._subscribers.clear()
