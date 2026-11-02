"""备份专用的安全错误、阻塞工作排空与受控子进程。

第三方异常、stderr和命令参数可能含秘密；调用方只收到预先定义的错误码与摘要。
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

_MESSAGES = {
    "interrupted": "备份执行已中断",
    "timeout": "备份操作超时",
    "tool_unavailable": "备份工具不可用，请检查部署配置",
    "tool_failed": "备份工具执行失败，请检查工具版本及数据库权限",
    "database_unsupported": "仅支持 PostgreSQL 和文件 SQLite 数据库",
    "snapshot_failed": "数据库快照失败",
    "invalid_file": "备份文件无效或无法访问",
    "encryption_failed": "备份文件加密失败",
    "network_blocked": "备份连接地址被部署出站策略拒绝",
    "network_unavailable": "无法解析备份连接地址",
    "mail_failed": "失败通知未被邮件服务器受理",
    "mail_unknown": "邮件受理结果未知，请核对邮件服务器记录",
    "storage_failed": "备份存储操作失败，请检查连接和权限",
    "storage_missing": "备份副本已不存在",
    "storage_identity": "备份副本身份校验失败",
    "cleanup_failed": "备份保留清理失败",
}


class BackupAdapterError(Exception):
    """只保留受限错误代码，不链出第三方原文。"""

    def __init__(self, code: str):
        self.code = code if code in _MESSAGES else "tool_failed"
        self.detail = _MESSAGES[self.code]
        super().__init__(self.detail)


def check_running(deadline: float, stop_event: threading.Event) -> None:
    if stop_event.is_set():
        raise BackupAdapterError("interrupted")
    if time.monotonic() >= deadline:
        raise BackupAdapterError("timeout")


async def run_blocking[T](worker: Callable[[threading.Event], T], stop_event: threading.Event | None = None) -> T:
    """取消仅发停止信号；真正线程结束后才向调用者抛出取消。"""
    stop = stop_event if stop_event is not None else threading.Event()
    task = asyncio.create_task(asyncio.to_thread(worker, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with contextlib.suppress(BaseException):
            task.result()
        raise


def _reap(proc: subprocess.Popen) -> None:
    """终止独占进程组并回收，不能只取消等待它的协程。"""
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def run_command(
    argv: list[str], *, deadline: float, stop_event: threading.Event,
    env: dict[str, str] | None = None, input_bytes: bytes | None = None, capture_output: bool = False,
) -> bytes:
    """argv来自部署与服务端，绝不经shell；stderr始终丢弃，版本输出有界。"""
    check_running(deadline, stop_event)
    captured = tempfile.TemporaryFile() if capture_output else None
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=captured if captured is not None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True,
        )
    except (OSError, ValueError):
        if captured is not None:
            captured.close()
        raise BackupAdapterError("tool_unavailable") from None
    try:
        pending_input = input_bytes
        while True:
            check_running(deadline, stop_event)
            try:
                proc.communicate(pending_input, timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                # communicate在超时后继续时不能再次传input，否则可能重复发送密码。
                pending_input = None
        if proc.returncode != 0:
            raise BackupAdapterError("tool_failed")
        if captured is None:
            return b""
        if captured.tell() > 65536:
            raise BackupAdapterError("tool_failed")
        captured.seek(0)
        return captured.read(65536)
    finally:
        _reap(proc)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()
        if captured is not None:
            captured.close()


def tool_environment() -> dict[str, str]:
    """保留工具查找和系统环境，不继承数据库/代理/应用密钥等隐式配置。"""
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "LANG", "LC_ALL")
    return {key: os.environ[key] for key in allowed if key in os.environ}
