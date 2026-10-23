# Database Guidelines

> Database patterns and conventions for this project.

---

## Overview

- ORM: SQLAlchemy 2.x async (`sqlalchemy[asyncio]`), typed `Mapped[...]` / `mapped_column(...)`.
- Driver: `asyncpg` on PostgreSQL in production
  (default `DATABASE_URL=postgresql+asyncpg://ainovel:ainovel@localhost:5432/ainovel`);
  `aiosqlite` in tests (dev dependency).
- Migrations: Alembic, async `env.py`, single initial revision `0001`.
- No ORM `relationship()` declarations. Cascades are done by database foreign keys;
  all access goes through explicit `select()` in services
  (docstring of `backend/app/models/entities.py`).

### Engine and session setup

`backend/app/core/database.py`:

```python
def create_engine(database_url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    engine = create_async_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        # SQLite 默认不启用外键约束，开启后 ON DELETE CASCADE / SET NULL 才生效
        @event.listens_for(engine.sync_engine, "connect")
        def _fk_pragma(dbapi_conn, _record):
            ...
            cursor.execute("PRAGMA foreign_keys=ON")
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False：提交后仍可读取属性，避免流式响应期间触发隐式查询
    return async_sessionmaker(engine, expire_on_commit=False)
```

`create_app()` stores `engine` and `sessionmaker` on `app.state`; the engine is
disposed in the lifespan. Per-request sessions come from `get_db` in
`backend/app/core/deps.py`:

```python
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session
```

Streaming jobs must **not** reuse the request session: routes call
`await db.close()` after the prepare step, and the SSE generator opens its own
session with `async with sessionmaker() as db:` (`SessionmakerDep`).

### Transactions

There is no unit-of-work wrapper. Each mutating service function ends with an
explicit `await db.commit()` followed by `await db.refresh(obj)` when the object
is returned. Use `await db.flush()` when a later query in the same function
must see the change (`ai_config_service.delete_config`).

---

## Query Patterns

### user_id isolation (mandatory)

Every service entry point first proves ownership. Foreign and non-existent rows
both return **404** so existence is not leaked.

`backend/app/services/project_service.py`:

```python
async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """按 user_id 校验归属；他人项目与不存在统一返回 404，避免泄露存在性。"""
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    if project is None:
        raise NotFoundError("项目不存在")
    return project
```

Tables without a `user_id` column are scoped by joining to the owner
(`backend/app/services/chapter_service.py`):

```python
async def get_owned_chapter(db: AsyncSession, user_id: int, chapter_id: int) -> Chapter:
    chapter = await db.scalar(
        select(Chapter)
        .join(Project, Project.id == Chapter.project_id)
        .where(Chapter.id == chapter_id, Project.user_id == user_id)
    )
    if chapter is None:
        raise NotFoundError("章节不存在")
    return chapter
```

Nested resources check the project first and then filter by **both** ids
(`get_project_chapter`, `story_service.get_owned`):

```python
await get_owned_project(db, user_id, project_id)
chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
```

Cross-references in request bodies must be validated against the same project
(`chapter_service._get_outline` raises `BadRequestError("大纲条目不存在或不属于该项目")`).
AI configs are filtered by `AIConfig.user_id == user_id` in every query
(`ai_config_service.get_config`, `list_configs`, `_clear_default`, `resolve_config`).

`backend/tests/test_projects_and_story.py::test_cross_user_isolation` asserts 404
for every route of another user's project, including generate/polish/wizard/export.

### Atomic state transitions

Concurrency-sensitive transitions are a conditional `UPDATE ... WHERE` and the
row count is checked (`backend/app/services/generation_service.py`):

```python
result = await db.execute(
    update(Chapter).where(Chapter.id == chapter.id, Chapter.status != "generating").values(status="generating")
)
await db.commit()
if result.rowcount != 1:
    raise ConflictError("该章节正在生成中，请稍候再试")
```

The rollback uses the same guard: `... where(Chapter.id == chapter_id, Chapter.status == "generating")`.

### Aggregates

Counts/sums are done in SQL, not Python: `project_service._stats_subquery()` uses
`func.count(Chapter.id)` and `func.coalesce(func.sum(Chapter.word_count), 0)` joined with `outerjoin`.

### Partial updates

Update schemas use optional fields. Distinguish "omitted" from "explicit null"
with `data.model_fields_set` for nullable columns
(`"base_url" in fields`, `"outline_id" in fields`), and `model_dump(exclude_unset=True)`
for generic updates (`project_service.update_project`).

### Word count rule

`word_count` is always derived server-side via `chapter_service.count_words`:

```python
def count_words(text: str) -> int:
    """字数统计：按非空白字符计（中文惯例）。"""
    return len(_WS_RE.sub("", text or ""))
```

Recompute it whenever `content` changes (create, update, generation `done`).
Clients never send `word_count`.

### String length limits and AI output

Column lengths (`backend/app/models/entities.py`) are mirrored in Pydantic
`max_length` (`backend/app/schemas/*.py`):

| Column | Limit |
|--------|-------|
| `users.username` | String(64); schema 3..32 |
| `ai_configs.name` / `characters.name` | String(100) |
| `ai_configs.base_url` | String(500) |
| `ai_configs.model` | String(200) |
| `projects.title`, `world_settings.title`, `outlines.title`, `outlines.volume_title`, `chapters.title` | String(200) |
| `projects.genre` | String(100) |
| `characters.role_type` | String(50) |
| `chapters.status` | String(16); `projects.status` / `ai_configs.provider` String(32) |
| long text (`content`, `summary`, `profile`, `premise`, `style`, `api_key_enc`) | Text |

AI output bypasses Pydantic, so it **must** be truncated to the column length
before insert (`backend/app/services/wizard_service.py`):

```python
"title": _text(ch.get("title") or ch.get("name"))[:200],
name = _text(c.get("name"))[:100]
"role_type": _text(c.get("role_type") or c.get("role"))[:50],
```

PostgreSQL rejects over-long `VARCHAR` values (SQLite does not), so a missing
slice only fails in production.

### Enumerated values

Status/provider values are plain strings, with the allowed set as tuples in
`entities.py` (`CHAPTER_STATUSES = ("draft", "generating", "done")`,
`AI_PROVIDERS`, `PROJECT_STATUSES`) and `Literal[...]` types in schemas. There are no DB enums.

---

## Migrations

- Location: `backend/alembic/versions/`; there is one revision,
  `0001_initial_schema.py` (`revision = '0001'`, `down_revision = None`).
- Apply: `cd backend && uv run alembic upgrade head`. The Docker image runs
  `alembic upgrade head && uvicorn app.main:app ...` (`Dockerfile`).
- Roll back: `uv run alembic downgrade -1` / `base`.
- New revision: edit models, then
  `uv run alembic revision --autogenerate -m "<message>"`, review the file, and keep
  `ondelete=` and naming-convention names (`op.f('fk_...')`) intact.
- `alembic/env.py` uses `render_as_batch=True` (SQLite-compatible ALTERs) and
  resolves the URL in order: `sqlalchemy.url` set by the caller (tests) →
  `DATABASE_URL` env → `get_settings().database_url`.
- `alembic/versions` is excluded from ruff (`extend-exclude` in `pyproject.toml`).
- `backend/tests/test_migrations.py::test_upgrade_and_downgrade` runs
  `upgrade head` + `downgrade base` on a `tmp_path` SQLite file and asserts the
  `chapters` FK `ondelete` values. Every new revision must keep this test passing.

---

## Naming Conventions

- Tables: plural snake_case (`users`, `ai_configs`, `world_settings`, `chapters`).
- PK: `id` integer. FK: `<singular>_id` with `index=True`.
- Constraint names come from `NAMING_CONVENTION` in `backend/app/models/base.py`:

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

- Timestamps: every model mixes in `TimestampMixin`:

```python
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
updated_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
)
```

  Always timezone-aware; never set them from Python.
- Ordering columns are named `sort` (Integer, default 0).

### Foreign key rules

All FKs are `ON DELETE CASCADE` (user → ai_configs/projects; project →
world_settings/characters/outlines/chapters), **except**:

```python
# 大纲被重新生成/删除时章节正文必须保留，因此用 SET NULL 而非 CASCADE
outline_id: Mapped[int | None] = mapped_column(
    ForeignKey("outlines.id", ondelete="SET NULL"), index=True, nullable=True
)
```

Why: the outline wizard replaces all outlines with
`delete(Outline).where(Outline.project_id == project_id)`. With CASCADE that
would silently delete written chapter text. Covered by
`backend/tests/test_wizard.py::test_wizard_overwrites_and_keeps_chapters`
(`assert ch["outline_id"] is None`, content preserved).

---

## Common Mistakes

- Querying by primary key only (`db.get(Chapter, id)`) inside a user-facing service:
  this skips ownership. `db.get` is only used where ownership was already proven
  (the SSE run phase uses `chapter_id` from a prepared job; `auth_service.get_user` by token subject).
- Forgetting `PRAGMA foreign_keys=ON` for a hand-built SQLite engine — CASCADE/SET NULL tests silently pass wrong.
- Inserting AI-produced strings into `String(n)` columns without slicing.
- Reusing the request `AsyncSession` inside a `StreamingResponse` generator.
- Changing an FK to CASCADE on a table that is bulk-replaced by the wizard.
- Updating `content` without recomputing `word_count`.
