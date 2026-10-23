"""统一持久化后台任务引擎（数据库即队列 + 进程内调度协程）。

使用方显式导入子模块：engine.TaskEngine、registry.TaskHandler / TaskContext；handlers 导入即注册内置类型。
"""
