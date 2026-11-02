"""标准GnuPG对称文件加密；恢复密码只通过专用stdin传入。"""

import hashlib
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from app.services.backups.process import BackupAdapterError, check_running, run_command, tool_environment

_OWNER_FILE = ".ainovel-backup-owner"


def _home_identity(instance_key: str, execution_key: str) -> tuple[Path, bytes]:
    try:
        if str(UUID(instance_key)) != instance_key or str(UUID(execution_key)) != execution_key:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BackupAdapterError("encryption_failed") from None
    owner = f"{instance_key}:{execution_key}\n".encode("ascii")
    digest = hashlib.sha256(owner).hexdigest()[:24]
    # 系统/tmp根在macOS为受控symlink，解析一次；不使用可能很长的TMPDIR。
    return Path("/tmp").resolve() / f".ainovel-gpg-{digest}", owner


def _create_home(home: Path, owner: bytes) -> None:
    root = os.open(home.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = False
    try:
        os.mkdir(home.name, 0o700, dir_fd=root)
        created = True
        directory = os.open(home.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            os.fchmod(directory, 0o700)
            marker = os.open(_OWNER_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            with os.fdopen(marker, "wb") as output:
                output.write(owner)
                output.flush()
                os.fsync(output.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if created:
            shutil.rmtree(home.name, dir_fd=root)
        raise BackupAdapterError("encryption_failed") from None
    finally:
        os.close(root)


def cleanup_encryption_home(
    instance_key: str, execution_key: str, *, gpgconf_path: str = "gpgconf",
    deadline: float, stop_event: threading.Event,
) -> None:
    """只核对并回收已知执行的短home；不存在时无操作，绝不扫描其他实例。"""
    home, owner = _home_identity(instance_key, execution_key)
    root = None
    directory = None
    try:
        root = os.open(home.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.open(home.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except FileNotFoundError:
            return
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BackupAdapterError("encryption_failed")
        marker = os.open(_OWNER_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(marker, "rb") as source:
            marker_info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.getuid()
                or stat.S_IMODE(marker_info.st_mode) != 0o600 or source.read(128) != owner
            ):
                raise BackupAdapterError("encryption_failed")
        run_command(
            [gpgconf_path, "--homedir", str(home), "--kill", "gpg-agent"],
            deadline=deadline, stop_event=stop_event, env=tool_environment(),
        )
        shutil.rmtree(home.name, dir_fd=root)
    except OSError:
        raise BackupAdapterError("encryption_failed") from None
    finally:
        if directory is not None:
            os.close(directory)
        if root is not None:
            os.close(root)


def encrypt_file(
    source: Path, destination: Path, password: str, *, gpg_path: str = "gpg", gpgconf_path: str = "gpgconf",
    deadline: float, stop_event: threading.Event, instance_key: str | None = None, execution_key: str | None = None,
) -> None:
    """成功后删除原始明文；失败保留给运行器统一清理，不覆盖已有输出。"""
    if not 16 <= len(password) <= 1024 or any(char in password for char in ("\r", "\n", "\x00")):
        raise BackupAdapterError("encryption_failed")
    check_running(deadline, stop_event)
    if (instance_key is None) != (execution_key is None):
        raise BackupAdapterError("encryption_failed")
    instance_key, execution_key = instance_key or str(uuid4()), execution_key or str(uuid4())
    home, owner = _home_identity(instance_key, execution_key)
    created = False
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        created = True
        _create_home(home, owner)
        try:
            run_command(
                [gpg_path, "--no-options", "--homedir", str(home), "--batch", "--yes", "--no-tty",
                 "--pinentry-mode", "loopback", "--passphrase-fd", "0", "--no-symkey-cache",
                 "--no-random-seed-file", "--cipher-algo", "AES256", "--compress-algo", "none",
                 "--output", str(destination), "--symmetric", str(source)],
                deadline=deadline, stop_event=stop_event, env=tool_environment(),
                input_bytes=password.encode("utf-8") + b"\n",
            )
            if not destination.stat().st_size:
                raise BackupAdapterError("encryption_failed")
            os.chmod(destination, 0o600)
            source.unlink()
        finally:
            # 独占home的精确身份也供重启恢复消费；清理有独立上限，不受执行停止信号影响。
            cleanup_encryption_home(
                instance_key, execution_key, gpgconf_path=gpgconf_path,
                deadline=time.monotonic() + 5, stop_event=threading.Event(),
            )
    except BackupAdapterError:
        if created:
            destination.unlink(missing_ok=True)
        raise
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise BackupAdapterError("encryption_failed") from None
