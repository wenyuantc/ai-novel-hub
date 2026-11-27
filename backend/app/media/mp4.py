"""读取 MP4 电影头中的时长。跳过音视频数据和未知 box，不解码媒体。"""

from typing import BinaryIO


def _find_box(file: BinaryIO, start: int, end: int, name: bytes) -> tuple[int, int] | None:
    # 短视频的外层头部很少。限制解析工作量，避免大量微小的 box 占用线程。
    for _ in range(4096):
        if end - start < 8:
            return None
        file.seek(start)
        header = file.read(8)
        size = int.from_bytes(header[:4])
        if size == 1:
            if end - start < 16:
                return None
            size = int.from_bytes(file.read(8))
        elif size == 0:
            size = end - start
        next_start = start + size
        if not file.tell() <= next_start <= end:
            return None
        if header[4:] == name:
            return file.tell(), next_start
        start = next_start
    return None


def duration_ms(file: BinaryIO, size: int) -> int | None:
    movie = _find_box(file, 0, size, b"moov")
    if movie is None or (header := _find_box(file, *movie, b"mvhd")) is None:
        return None
    start, end = header
    file.seek(start)
    version = file.read(1)
    if version not in (b"\x00", b"\x01"):
        return None
    width = 4 if version == b"\x00" else 8
    timescale_offset = start + 4 + 2 * width
    if timescale_offset + 4 + width > end:
        return None
    file.seek(timescale_offset)
    scale = int.from_bytes(file.read(4))
    ticks = int.from_bytes(file.read(width))
    if not scale or not ticks or ticks == (1 << (8 * width)) - 1:
        return None
    millis = (ticks * 1000 + scale - 1) // scale
    return millis if millis < 2**63 else None
