# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

Backend: FastAPI + async SQLAlchemy 2 + Alembic + PostgreSQL (asyncpg), managed with `uv`,
tested with pytest (SQLite via aiosqlite). Source lives in `backend/`; intent is in
`.trellis/tasks/10-22-ai-novel-platform/design.md`. These files document the conventions
the code actually follows, with excerpts from real files.

Quality gate: `cd backend && uv run ruff check . && uv run pytest`.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | `app/` layering (api/v1 → services → models, ai/providers, ai/prompts); routes never touch the ORM | Filled |
| [Database Guidelines](./database-guidelines.md) | Async engine/session, Alembic `0001`, FK CASCADE vs `outline_id` SET NULL, user_id ownership → 404, length limits, word count | Filled |
| [Error Handling](./error-handling.md) | `AppError` hierarchy, `{"detail": ...}` envelope, safe 422/500 handlers, SSE streaming contract scenario | Filled |
| [Quality Guidelines](./quality-guidelines.md) | ruff + pytest, test isolation, FakeLLM/fake_dns, forbidden patterns, SSRF guard and auth/secrets scenarios | Filled |
| [Admin & Sessions](./auth-admin.md) | Admin bootstrap env, `/admin/users`, guardrails, `token_version` session invalidation, change-password | Filled |
| [Logging Guidelines](./logging-guidelines.md) | stdlib `logging`, `logger.exception` where errors are masked, never log secrets | Filled |

### Code-spec scenarios (7-section contracts)

| Contract | Location |
|----------|----------|
| SSE streaming (frames, events, status lifecycle, rollback) | [error-handling.md](./error-handling.md#scenario-sse-streaming-contract-generate--polish--wizard) |
| SSRF endpoint guard (`ALLOW_PRIVATE_AI_ENDPOINTS`, `build_provider`) | [quality-guidelines.md](./quality-guidelines.md#scenario-ssrf-guard-for-user-supplied-ai-base-url) |
| Auth and secrets env (cookie, JWT, Fernet, key masking) | [quality-guidelines.md](./quality-guidelines.md#scenario-auth-and-secrets-environment-contract) |
| Admin role, user management, token versioning (`ver`, guardrails) | [auth-admin.md](./auth-admin.md#scenario-admin-role-user-management-and-token-versioning) |

---

## How to Maintain These Guidelines

1. Document the project's **actual conventions** (not ideals); verify against code before editing.
2. Include **code examples** copied from the codebase with file paths.
3. List **forbidden patterns** and why.
4. Add **common mistakes** the team has made (e.g. the 422 handler that returned 500 and echoed passwords).

---

**Language**: All documentation should be written in **English** (user-facing strings quoted in Chinese as they appear in code).
