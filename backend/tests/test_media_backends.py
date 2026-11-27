import asyncio
import io
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.media.backends import LocalBackend, MediaTooLarge, S3Backend


async def chunks(*parts):
    for part in parts:
        yield part


class BoundedBody(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 1024 * 1024
        return super().read(size)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.parts = []

    def close(self):
        pass

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Bucket, Key] = bytes(Body)

    def create_multipart_upload(self, **kwargs):
        self.parts = []
        return {"UploadId": "upload"}

    def upload_part(self, *, PartNumber, Body, **kwargs):
        self.parts.append(bytes(Body))
        return {"ETag": str(PartNumber)}

    def complete_multipart_upload(self, *, Bucket, Key, MultipartUpload, **kwargs):
        assert len(self.parts) == len(MultipartUpload["Parts"])
        self.objects[Bucket, Key] = b"".join(self.parts)
        self.parts = []

    def abort_multipart_upload(self, **kwargs):
        self.parts = []

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Bucket, Key])}

    def get_object(self, *, Bucket, Key, Range=None):
        data = self.objects[Bucket, Key]
        if Range:
            first, last = Range.removeprefix("bytes=").split("-")
            data = data[int(first):int(last) + 1 if last else None]
        return {"Body": BoundedBody(data)}

    def copy(self, source, bucket, key):
        self.objects[bucket, key] = self.objects[source["Bucket"], source["Key"]]

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


@pytest.fixture(params=["local", "s3"])
def backend(request, tmp_path, monkeypatch):
    if request.param == "local":
        return LocalBackend(tmp_path / "media")
    fake = FakeS3()
    store = S3Backend(endpoint="https://s3.test", region="us-east-1", bucket="media",
                      prefix="files", access_key="a", secret_key="s")
    monkeypatch.setattr(store, "_client", lambda: fake)
    return store


async def test_backend_contract(backend):
    data = b"a" * (9 * 1024**2) + b"end"
    assert await backend.put("ab/file", chunks(data[:-3], data[-3:])) == len(data)
    assert await backend.stat("ab/file") == len(data)
    assert b"".join([part async for part in backend.open("ab/file")]) == data
    await backend.copy("ab/file", "cd/copy")
    assert b"".join([part async for part in backend.open("cd/copy", start=len(data)-4, end=len(data)-2)]) == b"aen"
    await backend.delete("ab/file")
    await backend.delete("ab/file")
    assert await backend.stat("ab/file") is None
    await backend.check()


async def test_interrupted_put_is_atomic(backend):
    size = 9 * 1024**2
    with pytest.raises(MediaTooLarge):
        await backend.put("ab/file", chunks(b"a" * size, b"x"), max_bytes=size)
    assert await backend.stat("ab/file") is None
    if isinstance(backend, S3Backend):
        assert not backend._client().parts


@pytest.mark.parametrize("key", ["/absolute", "../escape", "dir/../escape", ".volume-id", "a\\b"])
async def test_invalid_keys(backend, key):
    with pytest.raises(ValueError):
        await backend.put(key, chunks(b"bad"))


async def test_local_refuses_link_outside_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "redirect").symlink_to(tmp_path, target_is_directory=True)
    backend = LocalBackend(root)
    with pytest.raises(ValueError):
        await backend.put("redirect/other", chunks(b"bad"))
    assert not (tmp_path / "other").exists()


async def test_delete_recovers_interrupted_local_upload_from_another_process(tmp_path):
    script = """
import asyncio, os, sys
from app.media.backends import LocalBackend

async def interrupted():
    yield b"incomplete"
    os._exit(7)

asyncio.run(LocalBackend(sys.argv[1]).put("aa/file", interrupted()))
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(tmp_path), cwd=Path(__file__).parents[1],
    )
    assert await asyncio.wait_for(child.wait(), 30) == 7
    directory = tmp_path / "aa"
    assert any(directory.iterdir())
    await LocalBackend(tmp_path).delete("aa/file")
    assert list(directory.iterdir()) == []


async def test_local_delete_defers_while_another_instance_writes(tmp_path):
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed():
        yield b"first"
        started.set()
        await proceed.wait()
        yield b"last"

    write = asyncio.create_task(LocalBackend(tmp_path).put("aa/file", delayed()))
    await started.wait()
    other = LocalBackend(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            await other.delete("aa/file")
    finally:
        proceed.set()
        await write
    assert await other.stat("aa/file") == 9
    await other.delete("aa/file")
    assert await other.stat("aa/file") is None
