"""导入即注册内置任务类型。"""

from app.services.task_engine.handlers import analysis, batch, chapter, entities, kb, outline, wizard

__all__ = ["analysis", "batch", "chapter", "entities", "kb", "outline", "wizard"]
