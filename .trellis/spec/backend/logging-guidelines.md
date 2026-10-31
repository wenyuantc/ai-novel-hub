# 日志规范

> 本项目中日志的记录方式。

---

## 概览

- 库：仅使用标准库 `logging` 模块。不使用 structlog，也不使用 JSON formatter。
- 每个需要记录日志的模块都声明一个模块级 logger：

```python
logger = logging.getLogger(__name__)
```

  （见于 `backend/app/main.py`、`backend/app/core/errors.py`、
  `backend/app/services/generation_service.py`、`backend/app/services/wizard_service.py`）。
- 配置只在构建默认 ASGI 应用时执行一次
  （`backend/app/main.py`）：

```python
def _build_default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
```

  `create_app()` 本身不配置日志，因此测试中能保留 pytest 的日志捕获。
- Alembic 使用 `backend/alembic.ini` 中自己的 `fileConfig`（root WARNING、
  `sqlalchemy.engine` WARNING、`alembic` INFO）；当调用方设置
  `config.attributes["configure_logger"] = False` 时，`alembic/env.py` 会跳过它（在 `tests/test_migrations.py` 中如此设置）。
- 日志消息使用中文书写，与代码注释保持一致。

---

## 日志级别

目前主要使用以下级别；审计敏感边界有明确的安全例外：

| 级别 | 用途 | 真实示例 |
|-------|-----|--------------|
| `info` | 运维人员需要看到一次的启动/环境信息 | `logger.info("未找到前端构建产物 %s，跳过静态文件挂载", dist)`（`main.py`） |
| `exception`（ERROR + traceback） | 一般对客户端屏蔽的意外异常 | 见下文 |
| `error`（不带原始 traceback） | 审计敏感边界未知故障/失败审计不可写 | 只输出 operation_id 与 committed，不输出 SQL 参数或原异常 |

没有使用 `debug`/`warning`。预期内的业务失败（`AppError`、
`AIProviderError`、`JSONExtractError`）**不**记录日志 —— 它们会返回给
用户，属于正常运行的一部分。范围内管理员业务失败仍会写持久审计，这与逐次输出运维日志不同。

---

## 结构化日志

采用 `basicConfig` 格式的纯文本行
`%(asctime)s %(levelname)s %(name)s: %(message)s`。上下文通过
`%` 风格的惰性参数传入，绝不使用 f-string：

```python
logger.exception("章节 %s 生成失败", job.chapter_id)
logger.exception("向导生成失败 project=%s kind=%s", job.project_id, job.kind)
logger.exception("回退章节 %s 状态失败", chapter_id)
```

用数字 id（`chapter_id`、`project_id`）标识对象，用（`kind`）标识操作；
不要输出请求 body 或模型对象。

---

## 应记录的内容

一般吞掉异常并替换为通用消息时使用 `logger.exception(...)` 保留 traceback；下列审计敏感边界例外优先，不能为了遵循一般规则重新泄露 SQL 参数：

- 全局 500 handler（`backend/app/core/errors.py`）：在返回 `{"detail": "服务器内部错误"}` 之前
  调用 `logger.exception("未处理的异常", exc_info=exc)`。
- `generation_service.run_chapter_job` 和 `wizard_service.run_wizard_job` 中 SSE 运行阶段的
  通用 `except Exception`，在 yield `error {"detail": "生成失败：服务器内部错误"}` 之前。
- 不得抛出异常的尽力而为式清理：`generation_service._restore_status`
  用 `try/except Exception` 包裹状态回退并记录日志。

---

## 审计敏感边界（强制例外）

`app/services/audit_writer.py::AuditOperation` 持有业务事务，SQLAlchemy异常可能包含章节正文、密码哈希等SQL参数；禁止直接logger.exception或继续向全局未知异常handler传播。只用`logger.error`输出固定中文消息、operation_id/committed，然后`raise AuditOperationError("服务器内部错误") from None`，由AppError handler返回固定500。失败审计不可写时同样只记安全标识，不递归审计。已提交业务的响应投影失败也不补写业务失败。

该例外不取消其他模块现有traceback规则。回归 `test_audit_system_database_error_does_not_reach_raw_exception_logger` 同时检查响应、审计行和caplog；不能仅脱敏数据库记录而遗漏运维日志。完整契约见[管理员操作审计](./admin-audit.md)。

## 不应记录的内容

- **绝不记录 API key** —— 无论明文还是密文（`api_key_enc`）。key 仅在
  `ai_config_service.build_provider` / provider 实例内部以明文存在；
  不要记录 `cfg`、provider 对象或 SDK 客户端的 kwargs。
- 绝不记录密码、密码哈希、JWT 或 `access_token` cookie、`JWT_SECRET`、
  `ENCRYPTION_KEY`。
- 不要记录完整的 prompt 或生成的章节正文（用户内容，可能很大）。
- 不要记录带 `input` 的 `RequestValidationError.errors()` —— 它包含原始请求
  （422 handler 出于同样原因去掉了 `input`，见 `error-handling.md`）。
- 不要为每个 SSE 分块添加 `logger.info`。

当确实需要展示敏感信息时（仅限 UI，不用于日志），使用
`app.core.security.mask_secret`（`sk-***1234`）。
