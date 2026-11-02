"""仅访问本实例拥有的备份；阻塞文件/S3操作必须通过 run_blocking 消费。"""

import contextlib
import hashlib
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from app.services.backups.network import NetworkPolicy
from app.services.backups.process import BackupAdapterError, check_running

CHUNK_SIZE = 256 * 1024
_EXTENSIONS = (".dump", ".sqlite3", ".dump.gpg", ".sqlite3.gpg")


def _uuid(value: str) -> str:
    try:
        parsed = str(UUID(value))
        if parsed != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError, AttributeError):
        raise BackupAdapterError("storage_identity") from None


def artifact_filename(execution_key: str, extension: str) -> str:
    if extension not in _EXTENSIONS:
        raise BackupAdapterError("storage_identity")
    return f"ainovel-{_uuid(execution_key)}{extension}"


def file_digest(path: Path, *, deadline: float, stop_event: threading.Event) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise BackupAdapterError("storage_identity")
            while block := source.read(CHUNK_SIZE):
                check_running(deadline, stop_event)
                digest.update(block)
                size += len(block)
        return size, digest.hexdigest()
    except BackupAdapterError:
        raise
    except OSError:
        raise BackupAdapterError("storage_failed") from None


class LocalStore:
    def __init__(self, root: Path, instance_key: str):
        self.root = root.absolute()
        self.instance_key = _uuid(instance_key)

    def _root_fd(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        # 目录由部署指定，但运行中不能让其他系统用户遍历数据库文件。
        os.fchmod(fd, 0o700)
        return fd

    def _directory_fd(self) -> int:
        root = self._root_fd()
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(self.instance_key, 0o700, dir_fd=root)
            fd = os.open(self.instance_key, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            os.fchmod(fd, 0o700)
            return fd
        finally:
            os.close(root)

    def ensure_directory(self) -> None:
        try:
            os.close(self._directory_fd())
        except OSError:
            raise BackupAdapterError("storage_failed") from None

    def _filename(self, locator: dict) -> str:
        instance, execution, name = locator.get("instance_key"), locator.get("execution_key"), locator.get("filename")
        if instance != self.instance_key or not isinstance(name, str):
            raise BackupAdapterError("storage_identity")
        if not any(name == artifact_filename(execution, ext) for ext in _EXTENSIONS):
            raise BackupAdapterError("storage_identity")
        return name

    def intended_locator(self, execution_key: str, filename: str) -> dict:
        locator = {"instance_key": self.instance_key, "execution_key": _uuid(execution_key), "filename": filename}
        self._filename(locator)
        return locator

    def save(
        self, source: Path, execution_key: str, filename: str, *, deadline: float, stop_event: threading.Event,
    ) -> dict:
        locator = self.intended_locator(execution_key, filename)
        directory = None
        temporary = f".{filename}.{uuid4()}.partial"
        try:
            check_running(deadline, stop_event)
            directory = self._directory_fd()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as destination, source.open("rb") as origin:
                while block := origin.read(CHUNK_SIZE):
                    check_running(deadline, stop_event)
                    destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
                identity = os.fstat(destination.fileno())
            check_running(deadline, stop_event)
            # link为原子不覆盖发布；同一执行意图不能覆盖身份不明的旧文件。
            os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            return {**locator, "device": identity.st_dev, "inode": identity.st_ino}
        except BackupAdapterError:
            raise
        except OSError:
            raise BackupAdapterError("storage_failed") from None
        finally:
            if directory is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=directory)
                os.close(directory)

    @staticmethod
    def _identity_matches(info: os.stat_result, locator: dict) -> bool:
        return stat.S_ISREG(info.st_mode) and (
            "inode" not in locator or (info.st_ino == locator["inode"] and info.st_dev == locator.get("device"))
        )

    def open(self, locator: dict, size_bytes: int | None = None) -> BinaryIO:
        directory = None
        fd = None
        try:
            name = self._filename(locator)
            directory = self._directory_fd()
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            info = os.fstat(fd)
            if not self._identity_matches(info, locator) or (size_bytes is not None and info.st_size != size_bytes):
                raise BackupAdapterError("storage_identity")
            result = os.fdopen(fd, "rb")
            fd = None
            return result
        except FileNotFoundError:
            raise BackupAdapterError("storage_missing") from None
        except OSError:
            raise BackupAdapterError("storage_failed") from None
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)

    def verify(self, locator: dict, size_bytes: int | None, checksum: str | None) -> bool:
        return self.verified_locator(locator, size_bytes, checksum) is not None

    def verified_locator(self, locator: dict, size_bytes: int | None, checksum: str | None) -> dict | None:
        if size_bytes is None or not checksum:
            return None
        try:
            with self.open(locator, size_bytes) as source:
                digest = hashlib.sha256()
                while block := source.read(CHUNK_SIZE):
                    digest.update(block)
                info = os.fstat(source.fileno())
                if digest.hexdigest() != checksum:
                    raise BackupAdapterError("storage_identity")
                return {**locator, "device": info.st_dev, "inode": info.st_ino}
        except BackupAdapterError as exc:
            if exc.code == "storage_missing":
                return None
            raise

    def delete(self, locator: dict) -> None:
        directory = None
        try:
            name = self._filename(locator)
            directory = self._directory_fd()
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not self._identity_matches(info, locator):
                raise BackupAdapterError("storage_identity")
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        except OSError:
            raise BackupAdapterError("cleanup_failed") from None
        finally:
            if directory is not None:
                os.close(directory)

    def temporary_directory(self, execution_key: str) -> Path:
        execution = _uuid(execution_key)
        directory = None
        try:
            directory = self._directory_fd()
            os.mkdir(f".work-{execution}", 0o700, dir_fd=directory)
            fd = os.open(f".work-{execution}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(fd)
            return self.root / self.instance_key / f".work-{execution}"
        except OSError:
            raise BackupAdapterError("storage_failed") from None
        finally:
            if directory is not None:
                os.close(directory)

    def clean_temporary(self, execution_key: str) -> None:
        execution = _uuid(execution_key)
        directory = None
        try:
            directory = self._directory_fd()
            name = f".work-{execution}"
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISDIR(info.st_mode):
                    raise BackupAdapterError("storage_identity")
                # fd版rmtree在支持dir_fd的平台抵抗目录替换，并且不追随目录内symlink。
                shutil.rmtree(name, dir_fd=directory)
            # 崩溃可能发生在原子发布前；只清该实例、该次执行的受控中间文件。
            pattern = re.compile(
                rf"^\.ainovel-{re.escape(execution)}\.(?:dump|sqlite3)(?:\.gpg)?\."
                r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.partial$",
            )
            for entry in os.listdir(directory):
                if pattern.fullmatch(entry):
                    os.unlink(entry, dir_fd=directory)
        except OSError:
            raise BackupAdapterError("cleanup_failed") from None
        finally:
            if directory is not None:
                os.close(directory)


@dataclass(frozen=True)
class S3Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


@dataclass(frozen=True)
class S3Location:
    endpoint: str
    region: str
    bucket: str
    key: str
    instance_key: str
    execution_key: str

    def validate(self) -> None:
        _uuid(self.instance_key)
        _uuid(self.execution_key)
        if (
            not self.bucket or not self.key or ".." in self.key.split("/") or self.key.startswith("/")
            or "\\" in self.key or not re.search(rf"(?:^|/){re.escape(self.instance_key)}/", self.key)
            or not any(self.key.endswith("/" + artifact_filename(self.execution_key, ext)) for ext in _EXTENSIONS)
        ):
            raise BackupAdapterError("storage_identity")

    def locator(self) -> dict:
        self.validate()
        return dict(endpoint=self.endpoint, region=self.region, bucket=self.bucket, key=self.key,
                    instance_key=self.instance_key, execution_key=self.execution_key)

    @classmethod
    def from_locator(cls, locator: dict) -> "S3Location":
        try:
            value = cls(**{name: locator[name] for name in cls.__dataclass_fields__})
            value.validate()
            return value
        except (KeyError, TypeError, ValueError):
            raise BackupAdapterError("storage_identity") from None


class S3Store:
    def __init__(self, location: S3Location, credentials: S3Credentials, policy: NetworkPolicy):
        location.validate()
        self.location, self.credentials, self.policy = location, credentials, policy

    def _client(self, deadline: float, stop_event: threading.Event):
        # 显式凭据和禁代理，不触发环境/metadata凭据链；每次实际发送都重做出站校验。
        import boto3
        from botocore.config import Config

        origin = self.policy.validate_endpoint(self.location.endpoint)
        check_running(deadline, stop_event)
        remaining = max(0.1, min(15.0, deadline - time.monotonic()))
        client = boto3.client(
            "s3", endpoint_url=self.location.endpoint, region_name=self.location.region,
            aws_access_key_id=self.credentials.access_key, aws_secret_access_key=self.credentials.secret_key,
            verify=self.policy.ca_bundle or True,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, proxies={},
                          connect_timeout=min(5.0, remaining), read_timeout=remaining,
                          retries={"mode": "standard", "max_attempts": 2}),
        )

        def guard(request, **_kwargs):
            check_running(deadline, stop_event)
            self.policy.validate_request(request.url, origin)

        client.meta.events.register("before-send.s3", guard)
        return client

    @staticmethod
    def _error(exc: Exception) -> BackupAdapterError:
        if isinstance(exc, BackupAdapterError):
            return exc
        response = getattr(exc, "response", {})
        code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        missing = code in ("NoSuchKey", "NoSuchVersion", "404")
        return BackupAdapterError("storage_missing" if missing else "storage_failed")

    def _identity(self, response: dict, size_bytes: int, checksum: str) -> None:
        metadata = response.get("Metadata", {})
        if (response.get("ContentLength") != size_bytes or metadata.get("sha256") != checksum
                or metadata.get("execution") != self.location.execution_key
                or metadata.get("instance") != self.location.instance_key):
            raise BackupAdapterError("storage_identity")

    def save(
        self, source: Path, size_bytes: int, checksum: str, *, deadline: float, stop_event: threading.Event,
        upload_started: Callable[[str], None] | None = None,
    ) -> str | None:
        from boto3.s3.transfer import TransferConfig

        client = None
        upload_id = None
        try:
            client = self._client(deadline, stop_event)

            def multipart_created(parsed, **_kwargs):
                nonlocal upload_id
                value = parsed.get("UploadId")
                if value:
                    upload_id = value
                    if upload_started is not None:
                        upload_started(value)

            client.meta.events.register("after-call.s3.CreateMultipartUpload", multipart_created)

            def transferred(_amount):
                check_running(deadline, stop_event)

            client.upload_file(
                str(source), self.location.bucket, self.location.key,
                ExtraArgs={"ContentType": "application/octet-stream", "Metadata": {
                    "sha256": checksum, "execution": self.location.execution_key,
                    "instance": self.location.instance_key,
                }}, Callback=transferred,
                Config=TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024,
                                      max_concurrency=2, max_io_queue=4, io_chunksize=CHUNK_SIZE,
                                      use_threads=True, preferred_transfer_client="classic"),
            )
            check_running(deadline, stop_event)
            head = client.head_object(Bucket=self.location.bucket, Key=self.location.key)
            self._identity(head, size_bytes, checksum)
            return head.get("VersionId")
        except Exception as exc:
            if client is not None and upload_id:
                # managed uploader也会尝试abort；只补尝试这个已知upload，不扫描bucket。
                with contextlib.suppress(Exception):
                    client.abort_multipart_upload(
                        Bucket=self.location.bucket, Key=self.location.key, UploadId=upload_id,
                    )
            raise self._error(exc) from None
        finally:
            if client is not None:
                client.close()

    def head(
        self, size_bytes: int, checksum: str, version_id: str | None, *, deadline: float,
        stop_event: threading.Event,
    ) -> str | None:
        client = None
        try:
            client = self._client(deadline, stop_event)
            args = self._object_args(version_id)
            response = client.head_object(**args)
            self._identity(response, size_bytes, checksum)
            return response.get("VersionId")
        except Exception as exc:
            raise self._error(exc) from None
        finally:
            if client is not None:
                client.close()

    def _object_args(self, version_id: str | None) -> dict:
        args = {"Bucket": self.location.bucket, "Key": self.location.key}
        if version_id is not None:
            args["VersionId"] = version_id
        return args

    def open(
        self, size_bytes: int, checksum: str, version_id: str | None, *, deadline: float,
        stop_event: threading.Event,
    ):
        client = None
        try:
            client = self._client(deadline, stop_event)
            response = client.get_object(**self._object_args(version_id))
            body = response["Body"]
            try:
                self._identity(response, size_bytes, checksum)
            except BaseException:
                body.close()
                raise
            return _S3Body(body, client)
        except Exception as exc:
            if client is not None:
                client.close()
            raise self._error(exc) from None

    def delete(self, version_id: str | None, *, deadline: float, stop_event: threading.Event) -> None:
        client = None
        try:
            client = self._client(deadline, stop_event)
            args = self._object_args(version_id)
            client.delete_object(**args)
            try:
                client.head_object(**args)
            except Exception as exc:
                if self._error(exc).code == "storage_missing":
                    return
                raise
            # 非版本桶的删除亦需确认；仍存在时不能宣称已删。
            raise BackupAdapterError("cleanup_failed")
        except Exception as exc:
            error = self._error(exc)
            if error.code != "storage_missing":
                raise error from None
        finally:
            if client is not None:
                client.close()

    def abort(self, upload_id: str, *, deadline: float, stop_event: threading.Event) -> None:
        client = None
        try:
            client = self._client(deadline, stop_event)
            client.abort_multipart_upload(Bucket=self.location.bucket, Key=self.location.key, UploadId=upload_id)
        except Exception as exc:
            # NoSuchUpload代表已清理/已完成；对象是否完成另由head判断。
            response = getattr(exc, "response", {})
            if not isinstance(response, dict) or response.get("Error", {}).get("Code") != "NoSuchUpload":
                raise self._error(exc) from None
        finally:
            if client is not None:
                client.close()


class _S3Body:
    def __init__(self, body, client):
        self.body, self.client = body, client

    def read(self, amount: int) -> bytes:
        try:
            return self.body.read(amount)
        except Exception:
            raise BackupAdapterError("storage_failed") from None

    def close(self) -> None:
        try:
            self.body.close()
        finally:
            self.client.close()
