"""Uvicorn 在响应开始时记录完整请求目标；隐藏回调参数与媒体外链凭据。"""
import logging


def redact_oauth_callback(record: logging.LogRecord) -> bool:
    args = record.args
    if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
        path, query, _ = args[2].partition("?")
        prefix, marker, _ = path.partition("/media/external/")
        if marker:
            record.args = (*args[:2], f"{prefix}{marker}[redacted]", *args[3:])
        elif query and "/auth/oauth/" in path and path.rstrip("/").endswith("/callback"):
            record.args = (*args[:2], path, *args[3:])
    return True
