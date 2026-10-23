# Logging Guidelines

> How logging is done in this project.

---

## Overview

- Library: the standard library `logging` module only. No structlog, no JSON formatter.
- Each module that logs declares a module-level logger:

```python
logger = logging.getLogger(__name__)
```

  (present in `backend/app/main.py`, `backend/app/core/errors.py`,
  `backend/app/services/generation_service.py`, `backend/app/services/wizard_service.py`).
- Configuration happens once, only when the default ASGI app is built
  (`backend/app/main.py`):

```python
def _build_default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
```

  `create_app()` itself does not configure logging, so tests keep pytest's capture.
- Alembic uses its own `fileConfig` from `backend/alembic.ini` (root WARNING,
  `sqlalchemy.engine` WARNING, `alembic` INFO); `alembic/env.py` skips it when the
  caller sets `config.attributes["configure_logger"] = False` (done in `tests/test_migrations.py`).
- Log messages are written in Chinese, matching code comments.

---

## Log Levels

Only two levels are used in application code today:

| Level | Use | Real example |
|-------|-----|--------------|
| `info` | startup/environment facts an operator should see once | `logger.info("未找到前端构建产物 %s，跳过静态文件挂载", dist)` (`main.py`) |
| `exception` (ERROR + traceback) | unexpected exceptions that are masked from the client | see below |

There is no `debug`/`warning` usage. Expected business failures (`AppError`,
`AIProviderError`, `JSONExtractError`) are **not** logged — they are returned to the
user and are part of normal operation.

---

## Structured Logging

Plain-text lines in the `basicConfig` format
`%(asctime)s %(levelname)s %(name)s: %(message)s`. Context is passed with
`%`-style lazy arguments, never f-strings:

```python
logger.exception("章节 %s 生成失败", job.chapter_id)
logger.exception("向导生成失败 project=%s kind=%s", job.project_id, job.kind)
logger.exception("回退章节 %s 状态失败", chapter_id)
```

Identify the object by numeric id (`chapter_id`, `project_id`) and the operation
(`kind`); do not dump request bodies or model objects.

---

## What to Log

Every place where an exception is swallowed and replaced by a generic message
must call `logger.exception(...)` so the traceback is kept server-side:

- Global 500 handler (`backend/app/core/errors.py`): `logger.exception("未处理的异常", exc_info=exc)`
  before returning `{"detail": "服务器内部错误"}`.
- SSE run phase generic `except Exception` in `generation_service.run_chapter_job` and
  `wizard_service.run_wizard_job`, before yielding `error {"detail": "生成失败：服务器内部错误"}`.
- Best-effort cleanup that must not raise: `generation_service._restore_status`
  wraps the status rollback in `try/except Exception` and logs.

---

## What NOT to Log

- **Never log API keys** — plaintext or encrypted (`api_key_enc`). The key exists in
  plaintext only inside `ai_config_service.build_provider` / the provider instance;
  do not log `cfg`, the provider object, or SDK client kwargs.
- Never log passwords, password hashes, JWTs or the `access_token` cookie, `JWT_SECRET`,
  `ENCRYPTION_KEY`.
- Do not log full prompts or generated chapter text (user content, potentially large).
- Do not log `RequestValidationError.errors()` with `input` — it contains the raw request
  (the 422 handler strips `input` for the same reason, see `error-handling.md`).
- Do not add `logger.info` per SSE chunk.

When something sensitive must be shown (UI only, not logs) use
`app.core.security.mask_secret` (`sk-***1234`).
