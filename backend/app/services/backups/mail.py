"""TLS SMTP失败通知；sent仅表示服务器受理，超时不自动重发。"""

import contextlib
import re
import smtplib
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Literal
from uuid import UUID

from app.services.backups.network import NetworkPolicy
from app.services.backups.process import BackupAdapterError, check_running


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    security: Literal["ssl", "starttls"]
    from_address: str
    recipients: tuple[str, ...]
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class FailureNotice:
    execution_key: str
    run_id: int
    status: Literal["partial", "failed", "interrupted", "succeeded"]
    occurred_at: datetime
    # 只接受目的地枚举，不能把异常/连接地址/配置拼进邮件。
    failed_destinations: tuple[str, ...] = ()
    cleanup_failed: bool = False


@dataclass(frozen=True)
class MailResult:
    status: Literal["sent", "failed", "unknown"]
    error_code: str | None = None
    error_message: str | None = None


def _message(config: MailConfig, notice: FailureNotice) -> EmailMessage:
    execution = str(UUID(notice.execution_key))
    if notice.status not in ("partial", "failed", "interrupted", "succeeded"):
        raise BackupAdapterError("mail_failed")
    if notice.run_id < 1 or not isinstance(notice.cleanup_failed, bool):
        raise BackupAdapterError("mail_failed")
    if any(value not in ("local", "s3") for value in notice.failed_destinations):
        raise BackupAdapterError("mail_failed")
    addresses = (config.from_address, *config.recipients)
    if not 1 <= len(config.recipients) <= 10 or any(
        not value or any(char in value for char in ("\r", "\n", "\x00")) for value in addresses
    ):
        raise BackupAdapterError("mail_failed")
    if any(value is not None and re.search(r"[\r\n\x00]", value) for value in (config.username, config.password)):
        raise BackupAdapterError("mail_failed")
    message = EmailMessage()
    message["Subject"] = "知页：数据库备份异常提醒"
    message["From"] = config.from_address
    message["To"] = ", ".join(config.recipients)
    message["Message-ID"] = f"<backup-{execution}@ainovel.invalid>"
    labels = {"partial": "部分成功", "failed": "失败", "interrupted": "中断", "succeeded": "备份成功"}
    destinations = "、".join("本地" if item == "local" else "S3" for item in notice.failed_destinations) or "无"
    message.set_content(
        f"数据库备份 #{notice.run_id}：{labels[notice.status]}\n"
        f"执行标识：{execution}\n时间（UTC）：{notice.occurred_at.isoformat()}\n"
        f"异常存储：{destinations}\n保留清理异常：{'是' if notice.cleanup_failed else '否'}\n"
        "请登录管理后台查看安全记录，并核对可用副本。此邮件不附备份文件或恢复密码。\n"
    )
    return message


@contextlib.contextmanager
def _interruptible_connection(client: smtplib.SMTP, deadline: float, stop: threading.Event) -> Iterator[None]:
    """send_message内含多轮命令；绝对截止/停止信号必须中断实际socket而非只取消等待。"""
    finished = threading.Event()

    def interrupt():
        while not finished.wait(0.025):
            if stop.is_set() or time.monotonic() >= deadline:
                connection = getattr(client, "sock", None)
                if connection is not None:
                    # 不能在另一个线程close文件包装器：它可能正持有阻塞读的内部锁。
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)

    watcher = threading.Thread(target=interrupt, name="backup-smtp-deadline")
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        watcher.join()


def _limit_connection(client: smtplib.SMTP, deadline: float, stop: threading.Event) -> None:
    check_running(deadline, stop)
    connection = getattr(client, "sock", None)
    if connection is not None:
        connection.settimeout(max(0.001, min(15, deadline - time.monotonic())))


def send_failure_notification(
    config: MailConfig, notice: FailureNotice, policy: NetworkPolicy, *, deadline: float,
    stop_event: threading.Event,
) -> MailResult:
    """调用前应持久记sending，返回后另存通知状态；不改变备份真实结果。"""
    client: smtplib.SMTP | None = None
    sending = False
    try:
        check_running(deadline, stop_event)
        message = _message(config, notice)
        policy.validate_host(config.host, config.port)
        context = policy.ssl_context()
        connection_timeout = max(0.1, min(15, deadline - time.monotonic()))
        if config.security == "ssl":
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=connection_timeout, context=context)
        elif config.security == "starttls":
            client = smtplib.SMTP(config.host, config.port, timeout=connection_timeout)
        else:
            raise BackupAdapterError("mail_failed")
        with _interruptible_connection(client, deadline, stop_event):
            if config.security == "starttls":
                _limit_connection(client, deadline, stop_event)
                client.ehlo()
                _limit_connection(client, deadline, stop_event)
                client.starttls(context=context)
                _limit_connection(client, deadline, stop_event)
                client.ehlo()
            _limit_connection(client, deadline, stop_event)
            if config.username:
                client.login(config.username, config.password or "")
            _limit_connection(client, deadline, stop_event)
            sending = True
            refused = client.send_message(message, from_addr=config.from_address, to_addrs=list(config.recipients))
        # 部分收件人受理后不能宣称全失败，更不能盲目重发给已受理的收件人。
        if refused:
            return MailResult("unknown", "mail_unknown", "部分收件人的邮件受理结果需核对")
        return MailResult("sent")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException):
        return MailResult("failed", "mail_failed", BackupAdapterError("mail_failed").detail)
    except (TimeoutError, smtplib.SMTPServerDisconnected, OSError):
        code = "mail_unknown" if sending else "mail_failed"
        return MailResult("unknown" if sending else "failed", code, BackupAdapterError(code).detail)
    except Exception:
        # SMTP明确返回拒绝可判失败；其余发送中未知系统异常保守记unknown。
        code = "mail_unknown" if sending else "mail_failed"
        return MailResult("unknown" if sending else "failed", code, BackupAdapterError(code).detail)
    finally:
        if client is not None:
            # 已确认的SMTP受理事实不因本地关闭失败而改写，也不外泄底层异常。
            with contextlib.suppress(OSError):
                client.close()
