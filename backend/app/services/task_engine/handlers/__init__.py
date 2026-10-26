"""导入即注册内置任务类型。"""

from app.services.task_engine.handlers import analysis, chapter, entities, outline, wizard

__all__ = ["analysis", "chapter", "entities", "outline", "wizard"]
