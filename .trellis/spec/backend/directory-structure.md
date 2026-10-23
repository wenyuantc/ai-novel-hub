# Directory Structure

> How backend code is organized in this project.

---

## Overview

The backend is a single FastAPI application under `backend/app/`, managed with `uv`
(`backend/pyproject.toml`, `[tool.uv] package = false`, Python `>=3.12,<3.13`).
It follows a strict layering:

```
HTTP route (app/api/v1)  ->  service (app/services)  ->  ORM model (app/models)
        |                         |
   Pydantic schema           AI layer (app/ai: providers + prompts)
   (app/schemas)
```

- Routes are thin: parse input via Pydantic schemas, resolve dependencies from
  `app/core/deps.py`, call **one** service function, return the result.
- All business queries and all `user_id` isolation live in `app/services/`.
- `app/ai/` knows nothing about HTTP or the DB; it exposes an `LLMProvider`
  abstraction, prompt builders and the SSRF guard.

The app is created by a factory, `create_app(settings, provider_factory)` in
`backend/app/main.py`, which stores shared objects on `app.state` (engine,
sessionmaker, cipher, provider factory) so tests can inject a fake LLM.

---

## Directory Layout

```
backend/
├── pyproject.toml            # deps, pytest + ruff config
├── alembic.ini
├── alembic/
│   ├── env.py                # async migrations, render_as_batch=True
│   └── versions/0001_initial_schema.py
├── app/
│   ├── main.py               # create_app(), error handlers, SPA fallback, /healthz
│   ├── api/
│   │   ├── sse.py            # format_sse(), sse_response() — SSE framing
│   │   └── v1/
│   │       ├── __init__.py   # api_router = APIRouter(prefix="/api/v1")
│   │       ├── auth.py       # /auth/register|login|logout|me|config
│   │       ├── ai_configs.py # /ai-configs CRUD, /{id}/default, /{id}/test
│   │       ├── projects.py   # /projects CRUD, /{id}/export
│   │       ├── story.py      # /projects/{id}/world|characters|outlines (router factory)
│   │       ├── chapters.py   # chapters CRUD, /chapters/{id}/generate|polish (SSE)
│   │       └── wizard.py     # /projects/{id}/wizard/{outline|characters|world} (SSE)
│   ├── core/
│   │   ├── config.py         # Settings (pydantic-settings), fail-fast validators
│   │   ├── database.py       # create_engine(), create_sessionmaker()
│   │   ├── deps.py           # DbDep, CurrentUser, CipherDep, FactoryDep, ...
│   │   ├── errors.py         # AppError hierarchy + register_error_handlers()
│   │   └── security.py       # bcrypt, JWT, SecretCipher (Fernet), mask_secret()
│   ├── models/
│   │   ├── base.py           # Base (naming convention), TimestampMixin
│   │   └── entities.py       # User, AIConfig, Project, WorldSetting, Character, Outline, Chapter
│   ├── schemas/              # Pydantic request/response models per domain
│   │   ├── auth.py  ai_config.py  project.py  story.py  chapter.py
│   ├── services/
│   │   ├── auth_service.py        ai_config_service.py   project_service.py
│   │   ├── story_service.py       chapter_service.py     export_service.py
│   │   ├── context_builder.py     # chapter prompt context with char budget
│   │   ├── generation_service.py  # chapter generate/polish jobs (prepare + run)
│   │   └── wizard_service.py      # outline/characters/world jobs + JSON normalize
│   └── ai/
│       ├── endpoint_guard.py # SSRF guard for user-supplied Base URLs
│       ├── json_extract.py   # tolerant JSON extraction from model output
│       ├── providers/
│       │   ├── __init__.py   # create_provider(), ProviderFactory type
│       │   ├── base.py       # LLMProvider ABC, AIProviderError, describe_error()
│       │   ├── openai_compat.py  anthropic.py  gemini.py
│       └── prompts/
│           ├── __init__.py   common.py   chapter.py   wizard.py
└── tests/
    ├── conftest.py           # app/client fixtures, autouse fake_dns
    ├── helpers.py            # FakeLLM, make_settings, register, parse_sse
    └── test_*.py
```

---

## Module Organization

**Rule: routes never run ORM queries; all business queries go through services.**
Route modules import from `app.core.deps`, `app.schemas.*` and `app.services.*`.
The only `app.models` imports in `app/api/v1/` are for return-type annotations
(`User` in `auth.py`, `Chapter` in `chapters.py`) — no `select()` / `db.get()` in routes.

Typical route (`backend/app/api/v1/chapters.py`):

```python
@router.patch("/projects/{project_id}/chapters/{chapter_id}", response_model=ChapterOut)
async def update_chapter(
    project_id: int, chapter_id: int, body: ChapterUpdate, user: CurrentUser, db: DbDep
) -> Chapter:
    return await svc.update_chapter(db, user.id, project_id, chapter_id, body)
```

Service functions take `db: AsyncSession` and `user_id: int` as the first
arguments and start with an ownership check (see `database-guidelines.md`).

Adding a new feature:

1. Model in `app/models/entities.py` (no ORM `relationship()`), then an Alembic revision.
2. Schemas in `app/schemas/<domain>.py` (`XxxCreate`, `XxxUpdate`, `XxxOut` with
   `ConfigDict(from_attributes=True)`). Exception: when an Out schema must not mirror
   the row (secrets), build it explicitly in the service — `AIConfigOut` is built by
   `ai_config_service.to_out()` so only `api_key_masked` leaves the server.
3. Service in `app/services/<domain>_service.py`, every entry point scoped by `user_id`.
4. Router in `app/api/v1/<domain>.py`, registered in `app/api/v1/__init__.py`.
5. Any LLM call goes through `ai_config_service.resolve_config()` +
   `ai_config_service.build_provider()`; prompt text lives in `app/ai/prompts/`, never inline in services.

AI streaming endpoints are split into a **prepare** step (request session, may
raise `AppError`) and a **run** async generator (own session from the
sessionmaker) — see `generation_service.py` / `wizard_service.py` and the SSE
contract in `error-handling.md`.

Repetitive CRUD is generated by a factory instead of copy-paste:
`backend/app/api/v1/story.py` `_make_router(segment, kind, create_schema, update_schema, out_schema, tag)`
backed by the generic `StoryKind` dataclass in `backend/app/services/story_service.py`.

---

## Naming Conventions

- Files: `snake_case.py`. Services are `<domain>_service.py`; helpers that are not
  domain services keep a descriptive name (`context_builder.py`, `json_extract.py`, `endpoint_guard.py`).
- Routes import a service module as `svc`: `from app.services import chapter_service as svc`.
- Router objects are named `router` (or `world_router` etc. when one module builds several).
- Schemas: `<Entity>Create`, `<Entity>Update`, `<Entity>Out`, plus request DTOs like
  `GenerateChapterRequest`, `WizardRequest`.
- Ownership helpers: `get_owned_<entity>(db, user_id, id)`.
- Dependency aliases in `deps.py` end with `Dep` (`DbDep`, `SettingsDep`, `CipherDep`,
  `FactoryDep`, `SessionmakerDep`), except `CurrentUser`.
- Code comments and docstrings are written in Chinese; user-facing error strings are Chinese.

---

## Examples

- Thin router + service split: `backend/app/api/v1/ai_configs.py` with `backend/app/services/ai_config_service.py`.
- Generic CRUD factory: `backend/app/api/v1/story.py` + `backend/app/services/story_service.py`.
- Two-phase streaming job: `backend/app/services/generation_service.py`
  (`prepare_chapter_job` / `run_chapter_job`).
- Provider abstraction: `backend/app/ai/providers/base.py` and the three implementations.
