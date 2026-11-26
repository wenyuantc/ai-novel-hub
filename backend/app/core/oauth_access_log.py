"""Uvicorn 在响应开始时记录完整请求目标；OAuth 回调仅记录路径。"""
import logging


def redact_oauth_callback(record: logging.LogRecord) -> bool:
    args = record.args
    if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
        path, query, _ = args[2].partition("?")
        if query and "/auth/oauth/" in path and path.rstrip("/").endswith("/callback"):
            record.args = (*args[:2], path, *args[3:])
    return True
