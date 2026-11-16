"""模型文本流的有界合并；业务事件与授权仍由调用方处理。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress


async def coalesce_buffered_text(stream: AsyncIterator[str], *, max_chars: int = 256) -> AsyncIterator[str]:
    """首片立即交付，后批最多合并 50ms 或 max_chars；有界生产者负责关闭原流。

    队列最多保留 64 个原始片段，不切分单个上游片段；只读取同一次模型响应，
    不跨业务事件。已读取的文本先交付，再传递原异常或取消。
    """
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("片段大小无效")
    queue: asyncio.Queue[tuple[str, str | Exception | None]] = asyncio.Queue(maxsize=64)
    closing = False

    async def produce() -> None:
        try:
            async with aclosing(stream):
                async for piece in stream:
                    await queue.put(("piece", piece))
        except asyncio.CancelledError:
            # 模型自行取消也必须唤醒消费者；消费者关闭时则不向已无人读取的队列写终态。
            if not closing:
                await queue.put(("cancelled", None))
            raise
        except Exception as exc:  # noqa: BLE001  将模型异常交回原执行体处理。
            await queue.put(("error", exc))
        else:
            await queue.put(("done", None))

    producer = asyncio.create_task(produce())
    first = True
    loop = asyncio.get_running_loop()
    try:
        while True:
            kind, value = await queue.get()
            if kind == "done":
                return
            if kind == "cancelled":
                raise asyncio.CancelledError
            if kind == "error":
                assert isinstance(value, Exception)
                raise value
            assert isinstance(value, str)
            if not value:
                continue
            batch, size = [value], len(value)
            terminal: tuple[str, str | Exception | None] | None = None
            if not first and size < max_chars:
                deadline = loop.time() + 0.05
                try:
                    # 每批只设一个截止时间，已有缓冲直接读取，避免逐片创建等待任务。
                    async with asyncio.timeout_at(deadline):
                        while size < max_chars and loop.time() < deadline:
                            try:
                                next_kind, next_value = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                next_kind, next_value = await queue.get()
                            if next_kind != "piece":
                                terminal = (next_kind, next_value)
                                break
                            assert isinstance(next_value, str)
                            batch.append(next_value)
                            size += len(next_value)
                except TimeoutError:
                    pass
            first = False
            yield "".join(batch)
            if terminal is not None:
                kind, value = terminal
                if kind == "done":
                    return
                if kind == "cancelled":
                    raise asyncio.CancelledError
                assert isinstance(value, Exception)
                raise value
    finally:
        closing = True
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
