"""导入即注册内置任务类型。"""

from app.services.task_engine.handlers import (
    agent,
    analysis,
    batch,
    book_import,
    chapter,
    entities,
    kb,
    outline,
    wizard,
)

__all__ = ["agent", "analysis", "batch", "book_import", "chapter", "entities", "kb", "outline", "wizard"]
