"""内部通道 v1：有界的认证记录及一个未确认逻辑帧的流控。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from json.encoder import encode_basestring
from weakref import WeakKeyDictionary, ref

CONTROL_BYTES = 64 * 1024
FRAME_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
IO_SECONDS = 5.0
# 等待下个逻辑帧允许跨过引擎的 15 秒心跳。收到首字节后不再续期。
IDLE_SECONDS = 22.0


class PeerUnavailable(Exception):
    def __init__(self):
        super().__init__("内部任务通道不可用")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def bounded_record(value, limit):
    """先按实际 UTF-8 JSON 字节扣除预算，超大正文也不会先生成整包副本。"""
    remaining = limit

    def charge(size):
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise PeerUnavailable()

    def visit(item):
        if isinstance(item, str):
            # 每个字符至少占一字节。转义与 UTF-8 的确切大小只在有可能装下时分段计算。
            if len(item) > remaining:
                raise PeerUnavailable()
            charge(2)
            for offset in range(0, len(item), CHUNK_BYTES):
                fragment = encode_basestring(item[offset:offset + CHUNK_BYTES])
                charge(len(fragment.encode()) - 2)
        elif isinstance(item, (dict, list, tuple)):
            charge(2 + max(0, len(item) - 1))
            if isinstance(item, dict):
                charge(len(item))  # 键值之间的冒号
                for key, child in item.items():
                    if not isinstance(key, str):
                        charge(2)  # json 为非字符串标量键加上引号
                    visit(key)
                    visit(child)
            else:
                for child in item:
                    visit(child)
        else:
            charge(len(canonical(item)))

    visit(value)
    return canonical(value)


def digest(value):
    return hashlib.sha256(canonical(value)).digest()


def derive(key: bytes, label: bytes) -> bytes:
    return hmac.digest(key, label, "sha256")


async def write_record(writer, key, value, limit):
    await _write_bytes(writer, key, bounded_record(value, limit))


async def _write_bytes(writer, key, data):
    async with asyncio.timeout(IO_SECONDS):
        writer.write(struct.pack("!I", len(data)) + derive(key, data))
        view = memoryview(data)
        for offset in range(0, len(data), CHUNK_BYTES):
            writer.write(view[offset:offset + CHUNK_BYTES])
            await writer.drain()


async def read_record(reader, key, limit, *, idle=IO_SECONDS):
    async with asyncio.timeout(idle):
        first = await reader.readexactly(1)
    async with asyncio.timeout(IO_SECONDS):
        length = struct.unpack("!I", first + await reader.readexactly(3))[0]
        if not 0 < length <= limit:
            raise PeerUnavailable()
        mac = await reader.readexactly(32)
        data = await reader.readexactly(length)
        if not hmac.compare_digest(mac, derive(key, data)):
            raise PeerUnavailable()
        try:
            record = json.loads(data)
        except (UnicodeError, ValueError, RecursionError):
            raise PeerUnavailable() from None
        if type(record) is not dict:
            raise PeerUnavailable()
        return record


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class PeerFrame:
    event: str
    data: dict
    proof: dict | None
    request: dict
    source_key: str
    target_key: str
    sequence: int
    subscription_id: str

    @property
    def conditions(self):
        return self.proof

    def _digest(self):
        return digest([self.event, self.data, self.proof, self.request,
                       self.source_key, self.target_key, self.sequence, self.subscription_id])


# 只登记在真实认证连接读到的帧；单次 ACK、重核、断开使该记录失效。
_received = WeakKeyDictionary()


def is_authenticated_frame(frame: object, *, ctx=None) -> bool:
    if type(frame) is not PeerFrame:
        return False
    receipt = _received.get(frame)
    if receipt is None:
        return False
    from app.services.task_engine.peer_transport import _active_subscription

    client_ref, original = receipt
    client = client_ref()
    try:
        return (client is not None and client.pending is frame and _active_subscription(client, frame, ctx)
                and client.available() and
                hmac.compare_digest(original, frame._digest()))
    except (ValueError, TypeError, RecursionError):
        return False


class PeerPublisher:
    """send 在本帧来源租约内调用；confirmation 只能在归还该租约后调用。"""

    def __init__(self, writer, controls, key, request, available):
        self.writer, self.controls = writer, controls
        self.key = derive(key, b"publish")
        self.source_key, self.target_key = request["source"], request["target"]
        self.subscription_id = request["nonce"]
        self.available = available
        self.seq = 1
        self._outstanding = False
        self._original = None
        self._deadline = 0.0

    async def send(self, frame: dict):
        if not self.available() or self._outstanding or set(frame) != {"event", "data", "proof"}:
            raise PeerUnavailable()
        if type(frame["event"]) is not str or type(frame["data"]) is not dict:
            raise PeerUnavailable()
        loop = asyncio.get_running_loop()
        if self._original is None:
            self._deadline = loop.time() + IO_SECONDS * 2
        record = {"kind": "frame", "seq": self.seq, "frame": frame}
        encoded = bounded_record(record, FRAME_BYTES)
        public = digest([frame["event"], frame["data"]])
        if self._original is not None and public != self._original:
            raise PeerUnavailable()
        if loop.time() >= self._deadline:
            raise PeerUnavailable()
        self._original = public
        self._outstanding = True
        async with asyncio.timeout_at(self._deadline):
            await _write_bytes(self.writer, self.key, encoded)

    async def confirmation(self) -> str:
        if not self._outstanding:
            raise PeerUnavailable()
        async with asyncio.timeout_at(self._deadline):
            record = await self.controls.get()
        kind = record.get("kind")
        if (not self.available() or kind not in ("ack", "recheck") or type(record.get("seq")) is not int
                or record != {"kind": kind, "seq": self.seq}):
            raise PeerUnavailable()
        self._outstanding = False
        if kind == "ack":
            self.seq += 1
            self._original = None
        return kind

    async def finish(self):
        if self._outstanding or self._original is not None or not self.available():
            raise PeerUnavailable()
        await write_record(self.writer, self.key, {"kind": "end", "seq": self.seq}, CONTROL_BYTES)


class PeerSubscriber:
    def __init__(self, reader, writer, key, request, available):
        self.reader, self.writer = reader, writer
        self.key, self.control_key = derive(key, b"publish"), derive(key, b"control")
        # 仅保留封存的请求；旧帧引用不能篡改下一帧的原始绑定。
        self._payload = canonical(request["payload"])
        self._source, self._target, self._nonce = request["target"], request["source"], request["nonce"]
        self.available = available
        self.seq = 1
        self.pending = None
        self._original = None
        self._ended = False

    async def receive(self) -> PeerFrame | None:
        if not self.available() or self.pending is not None or self._ended:
            raise PeerUnavailable()
        record = await read_record(self.reader, self.key, FRAME_BYTES, idle=IDLE_SECONDS)
        if not self.available() or type(record.get("seq")) is not int or record.get("seq") != self.seq:
            raise PeerUnavailable()
        if record == {"seq": self.seq, "kind": "end"} and self._original is None:
            self._ended = True
            return None
        frame = record.get("frame")
        if (set(record) != {"seq", "kind", "frame"} or record["kind"] != "frame" or type(frame) is not dict
                or set(frame) != {"event", "data", "proof"}
                or type(frame["event"]) is not str or type(frame["data"]) is not dict
                or (frame["proof"] is not None and type(frame["proof"]) is not dict)):
            raise PeerUnavailable()
        public = digest([frame["event"], frame["data"]])
        if self._original is not None and public != self._original:
            raise PeerUnavailable()
        self._original = public
        self.pending = PeerFrame(frame["event"], frame["data"], frame["proof"], json.loads(self._payload),
                                 self._source, self._target, self.seq, self._nonce)
        _received[self.pending] = (ref(self), self.pending._digest())
        return self.pending

    def invalidate(self):
        if self.pending is not None:
            _received.pop(self.pending, None)
            self.pending = None

    async def _confirm(self, kind):
        if not is_authenticated_frame(self.pending):
            raise PeerUnavailable()
        self.invalidate()
        await write_record(self.writer, self.control_key, {"kind": kind, "seq": self.seq}, CONTROL_BYTES)
        if kind == "ack":
            self._original = None
            self.seq += 1

    async def ack(self):
        await self._confirm("ack")

    async def recheck(self):
        await self._confirm("recheck")
