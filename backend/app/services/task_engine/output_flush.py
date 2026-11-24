"""模型静默时也按最后一次写入的截止时间刷新；同一轮只有一个写入者。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.services.task_engine.hub import TaskHub


@dataclass(frozen=True, slots=True)
class OutputSnapshot:
    text: str
    progress: int
    stage: str
    source: object
    text_source: object

    @classmethod
    def capture(cls, hub: TaskHub) -> "OutputSnapshot":
        return cls(hub.text, hub.progress, hub.stored_stage, hub.source, hub.snapshot_source())


class OutputFlushTimer:
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._handle: asyncio.TimerHandle | None = None
        self._writer: asyncio.Task | None = None
        self._idle = False

    def arm(self, delay: float, write: Callable[[], Awaitable[bool]], retry_delay: float) -> None:
        """write 返回 False 表示存储暂时不可用；等待下个窗口后重验同一来源。"""
        self._idle = True

        async def run() -> None:
            if not await write() and self._idle:
                self._handle = self._loop.call_later(max(0.05, retry_delay), start)

        def start() -> None:
            self._handle = None
            self._writer = self._loop.create_task(run(), name="task-output-flush")

        self._handle = self._loop.call_later(delay, start)

    async def settle(self) -> None:
        """下一次发布或退出前停止计时，已开始的数据库操作须等到事务结束。"""
        self._idle = False
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._writer is not None:
            writer, self._writer = self._writer, None
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await writer
                raise
