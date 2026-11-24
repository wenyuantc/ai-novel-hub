"""私有 UDS 所属记录。旧进程退出前留下的 inode 可供后续进程核对。"""

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path


def _owner_path(path):
    return path.with_suffix(".owner")


def save_identity(path: Path, identity: tuple[int, int]) -> None:
    # 目录仅服务账号可写，仍采用 O_EXCL 避免替换另一代留下的文件。
    marker = _owner_path(path)
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    owner = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(_identity_bytes(path, identity))
    except BaseException:
        # 即使磁盘只写下一半，也只移除这次创建的原 marker。
        with suppress(FileNotFoundError):
            current = marker.lstat()
            if (current.st_dev, current.st_ino) == (owner.st_dev, owner.st_ino):
                marker.unlink()
        raise


def _identity_bytes(path, identity):
    return hashlib.sha256(f"{path.name}/{identity[0]}/{identity[1]}".encode()).hexdigest().encode()


def matches(path, identity):
    marker = _owner_path(path)
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        return False
    with marker.open("rb") as f:
        return f.read(65) == _identity_bytes(path, identity)


def remove_if_owned(path, identity, *, current_identity):
    if current_identity(path) == identity and matches(path, identity):
        path.unlink()
        _owner_path(path).unlink()


def release_current(path, identity):
    """内存中保留的原 inode 足以清理部分启动失败；旧 marker 也单独核对。"""
    with suppress(FileNotFoundError):
        info = path.lstat()
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and (info.st_dev, info.st_ino) == identity:
            path.unlink()
    with suppress(FileNotFoundError):
        if matches(path, identity):
            _owner_path(path).unlink()
