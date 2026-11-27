"""媒体文件后端。键由业务层生成，所有范围均含首尾两个字节。"""

import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.services.backups.network import NetworkPolicy

READ_SIZE = 256 * 1024
PART_SIZE = 8 * 1024**2


class MediaTooLarge(ValueError):
    pass


class Backend(Protocol):
    async def put(self, key: str, source: AsyncIterator[bytes], *, size_hint: int | None = None,
                  max_bytes: int | None = None) -> int: ...
    def open(self, key: str, *, start: int | None = None, end: int | None = None) -> AsyncIterator[bytes]: ...
    async def stat(self, key: str) -> int | None: ...
    async def delete(self, key: str) -> None: ...
    async def copy(self, src: str, dst: str) -> None: ...
    async def presign_get(self, key: str, *, expires: int) -> str | None: ...
    async def check(self) -> None: ...


def _validate_key(key: str) -> str:
    if not all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part) for part in key.split("/")):
        raise ValueError("无效的媒体存储键")
    return key


def _validate_range(start: int | None, end: int | None) -> int:
    first = start or 0
    if first < 0 or (end is not None and end < first):
        raise ValueError("无效的字节范围")
    return first


def _check_size(size: int | None, limit: int | None) -> None:
    if limit is not None and size is not None and size > limit:
        raise MediaTooLarge("文件超过大小上限")


class _ConnectionProbe:
    async def check(self: Backend) -> None:
        key = f"probe/{uuid4().hex}"
        probe = os.urandom(32)

        async def source():
            yield probe

        try:
            await self.put(key, source())
            # 即使远端响应异常，也只读取探针大小。
            data = b"".join([chunk async for chunk in self.open(key, end=len(probe)-1)])
            if data != probe:
                raise OSError("媒体存储探针内容不一致")
        finally:
            await self.delete(key)


def _lock_upload(stream) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Windows 的普通文件句柄本身不共享删除权限。


def _create_upload(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    stream = os.fdopen(fd, "wb")
    try:
        _lock_upload(stream)
        return stream
    except BaseException:
        stream.close()
        raise


def _delete_local(path: Path) -> None:
    temporary = path.with_name(f".upload-{path.name}")
    if os.name == "posix":
        try:
            fd = os.open(temporary, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, "rb+") as stream:
                # 崩溃会释放锁；仍在写入的对象留待下轮清理。
                _lock_upload(stream)
                temporary.unlink(missing_ok=True)
    else:
        try:
            temporary.unlink(missing_ok=True)
        except PermissionError as exc:
            if getattr(exc, "winerror", None) == 32:
                raise BlockingIOError("媒体文件正在写入") from exc
            raise
    path.unlink(missing_ok=True)


class LocalBackend(_ConnectionProbe):
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / _validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("媒体路径超出根目录")
        return path

    async def put(self, key: str, source: AsyncIterator[bytes], *, size_hint: int | None = None,
                  max_bytes: int | None = None) -> int:
        _check_size(size_hint, max_bytes)
        path = await asyncio.to_thread(self._path, key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        temporary = path.with_name(f".upload-{path.name}")
        temp = await asyncio.to_thread(_create_upload, temporary)
        size = 0
        try:
            async for chunk in source:
                size += len(chunk)
                _check_size(size, max_bytes)
                await asyncio.to_thread(temp.write, chunk)
            await asyncio.to_thread(temp.flush)
            if os.name != "posix":
                await asyncio.to_thread(temp.close)
            await asyncio.to_thread(os.replace, temporary, path)
            return size
        except BaseException:
            if os.name != "posix":
                await asyncio.to_thread(temp.close)
            await asyncio.to_thread(temporary.unlink, missing_ok=True)
            raise
        finally:
            await asyncio.to_thread(temp.close)

    async def open(self, key: str, *, start: int | None = None, end: int | None = None) -> AsyncIterator[bytes]:
        first = _validate_range(start, end)
        path = await asyncio.to_thread(self._path, key)
        stream = await asyncio.to_thread(path.open, "rb")
        remaining = None if end is None else end - first + 1
        try:
            await asyncio.to_thread(stream.seek, first)
            while remaining is None or remaining:
                amount = READ_SIZE if remaining is None else min(READ_SIZE, remaining)
                chunk = await asyncio.to_thread(stream.read, amount)
                if not chunk:
                    break
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)
        finally:
            await asyncio.to_thread(stream.close)

    async def stat(self, key: str) -> int | None:
        path = await asyncio.to_thread(self._path, key)
        try:
            return (await asyncio.to_thread(path.stat)).st_size
        except FileNotFoundError:
            return None

    async def delete(self, key: str) -> None:
        path = await asyncio.to_thread(self._path, key)
        await asyncio.to_thread(_delete_local, path)

    async def copy(self, src: str, dst: str) -> None:
        await self.put(dst, self.open(src))

    async def presign_get(self, key: str, *, expires: int) -> None:
        return None


class S3Backend(_ConnectionProbe):
    def __init__(self, *, endpoint: str, region: str, bucket: str, access_key: str, secret_key: str,
                 prefix: str = "", path_style: bool = True, policy: NetworkPolicy | None = None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
            raise ValueError("S3 桶名无效")
        if not (access_key and secret_key):
            raise ValueError("S3 凭据未设置")
        self.endpoint, self.region, self.bucket = endpoint, region, bucket
        self.access_key, self.secret_key = access_key, secret_key
        prefix = prefix.strip("/")
        self.prefix = _validate_key(prefix) if prefix else ""
        self.path_style = path_style
        self.policy = policy or NetworkPolicy()

    def _client(self):
        origin = self.policy.validate_endpoint(self.endpoint)
        client = boto3.client(
            "s3", endpoint_url=self.endpoint, region_name=self.region,
            aws_access_key_id=self.access_key, aws_secret_access_key=self.secret_key,
            verify=self.policy.ca_bundle or True,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path" if self.path_style else "virtual"},
                          proxies={}, connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
        )
        # 仅允许配置的端点和显式选择的虚拟主机名。在每次发送（含重试）前重新检查 DNS。
        def guard(request, **_kwargs):
            expected = origin
            if not self.path_style and urlsplit(request.url).hostname != urlsplit(origin).hostname:
                expected = f"https://{self.bucket}.{urlsplit(origin).netloc}"
            self.policy.validate_request(request.url, expected)

        client.meta.events.register("before-send.s3", guard)
        return client

    @asynccontextmanager
    async def _connection(self):
        client = await asyncio.to_thread(self._client)
        try:
            yield client
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "404"}:
                raise FileNotFoundError("找不到媒体对象") from None
            raise
        finally:
            await asyncio.to_thread(client.close)

    def _args(self, key: str) -> dict:
        key = _validate_key(key)
        return {"Bucket": self.bucket, "Key": f"{self.prefix}/{key}" if self.prefix else key}

    async def put(self, key: str, source: AsyncIterator[bytes], *, size_hint: int | None = None,
                  max_bytes: int | None = None) -> int:
        _check_size(size_hint, max_bytes)
        args = self._args(key)
        size = 0
        parts = []
        buffer = bytearray()
        upload_id = None
        async with self._connection() as client:
            async def send_part():
                part_number = len(parts) + 1
                result = await asyncio.to_thread(client.upload_part, **args, UploadId=upload_id,
                                                 PartNumber=part_number, Body=bytes(buffer))
                parts.append({"ETag": result["ETag"], "PartNumber": part_number})
                buffer.clear()

            try:
                async for chunk in source:
                    size += len(chunk)
                    _check_size(size, max_bytes)
                    view = memoryview(chunk)
                    while view:
                        take = min(len(view), PART_SIZE - len(buffer))
                        buffer.extend(view[:take])
                        view = view[take:]
                        if len(buffer) == PART_SIZE:
                            if upload_id is None:
                                upload = await asyncio.to_thread(client.create_multipart_upload, **args)
                                upload_id = upload["UploadId"]
                            await send_part()
                if upload_id is None:
                    await asyncio.to_thread(client.put_object, **args, Body=bytes(buffer))
                else:
                    if buffer:
                        await send_part()
                    await asyncio.to_thread(client.complete_multipart_upload, **args, UploadId=upload_id,
                                            MultipartUpload={"Parts": parts})
                    upload_id = None
                return size
            finally:
                if upload_id is not None:
                    await asyncio.to_thread(client.abort_multipart_upload, **args, UploadId=upload_id)

    async def open(self, key: str, *, start: int | None = None, end: int | None = None) -> AsyncIterator[bytes]:
        args = self._args(key)
        first = _validate_range(start, end)
        if start is not None or end is not None:
            args["Range"] = f"bytes={first}-{end if end is not None else ''}"
        async with self._connection() as client:
            body = (await asyncio.to_thread(client.get_object, **args))["Body"]
            try:
                while chunk := await asyncio.to_thread(body.read, READ_SIZE):
                    yield chunk
            finally:
                await asyncio.to_thread(body.close)

    async def stat(self, key: str) -> int | None:
        args = self._args(key)
        try:
            async with self._connection() as client:
                return (await asyncio.to_thread(client.head_object, **args))["ContentLength"]
        except FileNotFoundError:
            return None

    async def delete(self, key: str) -> None:
        args = self._args(key)
        try:
            async with self._connection() as client:
                await asyncio.to_thread(client.delete_object, **args)
        except FileNotFoundError:
            pass

    async def copy(self, src: str, dst: str) -> None:
        source, target = self._args(src), self._args(dst)
        async with self._connection() as client:
            await asyncio.to_thread(client.copy, source, self.bucket, target["Key"])

    async def presign_get(self, key: str, *, expires: int) -> str:
        args = self._args(key)
        async with self._connection() as client:
            return await asyncio.to_thread(client.generate_presigned_url, "get_object", Params=args, ExpiresIn=expires)
