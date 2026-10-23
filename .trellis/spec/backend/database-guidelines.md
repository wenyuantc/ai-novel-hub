# 数据库规范

> 本项目的数据库模式与约定。

---

## 概览

- ORM：SQLAlchemy 2.x async（`sqlalchemy[asyncio]`），使用带类型的 `Mapped[...]` / `mapped_column(...)`。
- 驱动：生产环境在 PostgreSQL 上使用 `asyncpg`
  （默认 `DATABASE_URL=postgresql+asyncpg://ainovel:ainovel@localhost:5432/ainovel`）；
  测试中使用 `aiosqlite`（开发依赖）。
- Migration：Alembic，异步 `env.py`；版本 `0001`（初始）→ `0002`（ai_configs 模型参数）→ `0003`（users `is_admin`/`is_active`/`token_version`，见 [auth-admin.md](./auth-admin.md)）→ `0004`（ai_configs `model_type`）→ `0005`（`chapter_versions` + `generation_logs`）→ `0006`（`tasks` + `generation_logs.task_id`）。新版本使用 `batch_alter_table`，以便 SQLite 测试能够执行。
- 不声明 ORM `relationship()`。级联由数据库外键完成；
  所有访问都通过 service 中显式的 `select()` 进行
  （见 `backend/app/models/entities.py` 的 docstring）。

### Engine 与 session 配置

`backend/app/core/database.py`：

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

`create_app()` 将 `engine` 和 `sessionmaker` 存放在 `app.state` 上；engine 在
lifespan 中释放。每个请求的 session 来自 `backend/app/core/deps.py` 中的 `get_db`：

```python
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session
```

流式任务**不得**复用请求 session：路由在 prepare 步骤之后调用
`await db.close()`，SSE 生成器通过 `async with sessionmaker() as db:`
（`SessionmakerDep`）打开自己的 session。

### 事务

没有 unit-of-work 包装。每个有写操作的 service 函数都以显式的
`await db.commit()` 结束，若需返回对象，则紧接着 `await db.refresh(obj)`。
当同一函数中后续查询需要看到变更时，使用 `await db.flush()`
（`ai_config_service.delete_config`）。

---

## 查询模式

### user_id 隔离（强制）

每个 service 入口首先校验归属。他人的记录与不存在的记录
都返回 **404**，以免泄露存在性。

`backend/app/services/project_service.py`：

```python
async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """按 user_id 校验归属；他人项目与不存在统一返回 404，避免泄露存在性。"""
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    if project is None:
        raise NotFoundError("项目不存在")
    return project
```

没有 `user_id` 列的表通过 join 到归属方来限定范围
（`backend/app/services/chapter_service.py`）：

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

嵌套资源先校验项目，再按**两个** id 同时过滤
（`get_project_chapter`、`story_service.get_owned`）：

```python
await get_owned_project(db, user_id, project_id)
chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
```

请求 body 中的交叉引用必须针对同一项目进行校验
（`chapter_service._get_outline` 抛出 `BadRequestError("大纲条目不存在或不属于该项目")`）。
AI 配置在每个查询中都按 `AIConfig.user_id == user_id` 过滤
（`ai_config_service.get_config`、`list_configs`、`_clear_default`、`resolve_config`）。

`backend/tests/test_projects_and_story.py::test_cross_user_isolation` 断言访问他人项目的
每个路由都返回 404，包括 generate/polish/wizard/export。

### 原子状态迁移

对并发敏感的状态迁移使用条件 `UPDATE ... WHERE`，并检查
受影响行数（`backend/app/services/generation_service.py`）：

```python
result = await db.execute(
    update(Chapter).where(Chapter.id == chapter.id, Chapter.status != "generating").values(status="generating")
)
await db.commit()
if result.rowcount != 1:
    raise ConflictError("该章节正在生成中，请稍候再试")
```

回退使用同样的守卫条件：`... where(Chapter.id == chapter_id, Chapter.status == "generating")`。

### 聚合

计数/求和在 SQL 中完成，而不是在 Python 中：`project_service._stats_subquery()` 使用
`func.count(Chapter.id)` 和 `func.coalesce(func.sum(Chapter.word_count), 0)`，并通过 `outerjoin` 关联。

### 部分更新

更新 schema 使用可选字段。对于可空列，使用 `data.model_fields_set`
区分「未传」与「显式传 null」
（`"base_url" in fields`、`"outline_id" in fields`）；通用更新则使用 `model_dump(exclude_unset=True)`
（`project_service.update_project`）。

### 字数规则

`word_count` 始终由服务端通过 `chapter_service.count_words` 计算：

```python
def count_words(text: str) -> int:
    """字数统计：按非空白字符计（中文惯例）。"""
    return len(_WS_RE.sub("", text or ""))
```

每当 `content` 变化（创建、更新、生成 `done`）时都要重新计算。
客户端从不发送 `word_count`。

### 字符串长度限制与 AI 输出

列长度（`backend/app/models/entities.py`）与 Pydantic 的
`max_length`（`backend/app/schemas/*.py`）保持一致：

| 列 | 限制 |
|--------|-------|
| `users.username` | String(64)；schema 3..32 |
| `ai_configs.name` / `characters.name` | String(100) |
| `ai_configs.base_url` | String(500) |
| `ai_configs.model` | String(200) |
| `projects.title`、`world_settings.title`、`outlines.title`、`outlines.volume_title`、`chapters.title` | String(200) |
| `projects.genre` | String(100) |
| `characters.role_type` | String(50) |
| `chapters.status` | String(16)；`projects.status` / `ai_configs.provider` String(32) |
| 长文本（`content`、`summary`、`profile`、`premise`、`style`、`api_key_enc`） | Text |

AI 输出绕过了 Pydantic，因此插入前**必须**截断到列长度
（`backend/app/services/wizard_service.py`）：

```python
"title": _text(ch.get("title") or ch.get("name"))[:200],
name = _text(c.get("name"))[:100]
"role_type": _text(c.get("role_type") or c.get("role"))[:50],
```

PostgreSQL 会拒绝超长的 `VARCHAR` 值（SQLite 不会），因此漏掉截断
只会在生产环境中失败。

### 枚举值

状态/provider 值是普通字符串，允许的取值以元组形式定义在
`entities.py` 中（`CHAPTER_STATUSES = ("draft", "generating", "done")`、
`AI_PROVIDERS`、`PROJECT_STATUSES`），schema 中使用 `Literal[...]` 类型。不使用数据库枚举。

---

## Migration

- 位置：`backend/alembic/versions/`：`0001_initial_schema.py`（`down_revision = None`）、
  `0002_ai_config_model_params.py`（`down_revision = '0001'`，新增 `thinking_level`，
  `server_default='off'`，以及可空的 `context_window_k` / `max_output_k`）、
  `0003_user_admin_fields.py`（`down_revision = '0002'`，新增 `users.is_admin` / `is_active` /
  `token_version`，通过 `server_default` 回填；`test_0003_backfills_existing_users`）、
  `0004_ai_config_model_type.py`（`down_revision = '0003'`，`ai_configs.model_type` 回填 `text`；
  `test_0004_backfills_model_type`）、`0005_chapter_versions.py`（`down_revision = '0004'`，只新建
  `generation_logs` 与 `chapter_versions` 两张表，不回填；`test_0005_chapter_versions` 断言外键规则与
  `(chapter_id, seq)` 唯一约束）、`0006_tasks.py`（`down_revision = '0005'`，新建 `tasks`，给 `generation_logs`
  加可空 `task_id`；`test_0006_tasks` 断言外键规则、部分唯一索引确实只约束活动任务、存量日志 `task_id` 为空，
  `downgrade -1` 后存量日志保留）。
  `tests/test_migrations.py` 在 `upgrade head` 之后断言新列存在。
- 针对某个版本的回填测试必须 `upgrade` 到**该版本号**再 `downgrade -1`，不要写 `upgrade head`——
  否则后续新增版本后 `-1` 回退的是最新版本，测试会失效（`test_0004_*` 曾因此在 0005 合入时调整）。
- 应用：`cd backend && uv run alembic upgrade head`。Docker 镜像运行
  `alembic upgrade head && uvicorn app.main:app ...`（`Dockerfile`）。
- 回滚：`uv run alembic downgrade -1` / `base`。
- 新建版本：先修改模型，然后执行
  `uv run alembic revision --autogenerate -m "<message>"`，审查生成的文件，并保持
  `ondelete=` 和命名约定名称（`op.f('fk_...')`）不变。
- `alembic/env.py` 使用 `render_as_batch=True`（兼容 SQLite 的 ALTER），并
  按以下顺序解析 URL：调用方设置的 `sqlalchemy.url`（测试）→
  `DATABASE_URL` 环境变量 → `get_settings().database_url`。
- `alembic/versions` 被排除在 ruff 之外（`pyproject.toml` 中的 `extend-exclude`）。
- `backend/tests/test_migrations.py::test_upgrade_and_downgrade` 在 `tmp_path` 下的 SQLite 文件上
  执行 `upgrade head` + `downgrade base`，并断言 `chapters` 外键的 `ondelete` 值。
  每个新版本都必须保持该测试通过。

---

## 命名约定

- 表：复数 snake_case（`users`、`ai_configs`、`world_settings`、`chapters`）。
- 主键：整数 `id`。外键：`<singular>_id`，并设置 `index=True`。
- 约束名称来自 `backend/app/models/base.py` 中的 `NAMING_CONVENTION`：

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

- 时间戳：每个模型都混入 `TimestampMixin`：

```python
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
updated_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
)
```

  始终带时区；永远不要在 Python 中设置它们。
- 排序列命名为 `sort`（Integer，默认 0）。

### 外键规则

所有外键都是 `ON DELETE CASCADE`（user → ai_configs/projects；project →
world_settings/characters/outlines/chapters），**例外**：

```python
# 大纲被重新生成/删除时章节正文必须保留，因此用 SET NULL 而非 CASCADE
outline_id: Mapped[int | None] = mapped_column(
    ForeignKey("outlines.id", ondelete="SET NULL"), index=True, nullable=True
)
```

原因：大纲向导通过
`delete(Outline).where(Outline.project_id == project_id)` 替换全部大纲。若使用 CASCADE，
会静默删除已写好的章节正文。由
`backend/tests/test_wizard.py::test_wizard_overwrites_and_keeps_chapters` 覆盖
（`assert ch["outline_id"] is None`，正文被保留）。

其他 `SET NULL` 例外（`0005`）：`generation_logs.chapter_id` / `generation_logs.ai_config_id`（章节或配置删除后
记录仍留在项目下，`provider`/`model` 冗余存储）、`chapter_versions.generation_log_id`（日志被 200 条上限裁剪后
版本保留）。`chapter_versions.chapter_id`、`generation_logs.project_id` 为 CASCADE。

`0006`：`tasks.user_id` / `tasks.project_id` 为 CASCADE（删用户 / 项目时任务随之删除）；`tasks.chapter_id`
（章节删除后任务仍可查看，`title` 冗余存储）、`tasks.retry_of_id`（原任务被清理后重试链断开）、
`generation_logs.task_id`（任务被清理 / 裁剪后日志保留）为 SET NULL。

---

## 场景：章节版本写入规则

### 1. 范围 / 触发条件

任何改写 `chapters.content` 的代码路径：手动保存（`chapter_service.create_chapter` / `update_chapter`）、
整章生成/润色（`generation_service.run_chapter_job`）、局部改写采纳（`rewrite_service.apply_rewrite`）、
回滚（`chapter_version_service.restore_version`）。后续的批量生成、按建议重写也必须复用这里的原语。

### 2. 签名

```python
# backend/app/services/version_service.py（只含写入原语，不依赖其他 service，避免与 chapter_service 循环导入）
MANUAL_MERGE_WINDOW = timedelta(minutes=10); MAX_VERSIONS = 50
async def record_manual(db, chapter) -> ChapterVersion | None
async def ensure_baseline(db, chapter) -> None
async def record(db, chapter, source, *, log_id=None, note="") -> ChapterVersion   # 内部会 prune
async def prune(db, chapter_id) -> None
async def get_latest(db, chapter_id) -> ChapterVersion | None

# backend/app/services/chapter_version_service.py —— HTTP 入口（list/get/update/delete/restore）
# backend/app/services/generation_log_service.py
MAX_LOGS_PER_PROJECT = 200
async def write_log(db, *, project_id, chapter_id, kind, ai_config_id, provider, model, params, messages,
                    status, error="", output_chars=0, duration_ms=0) -> GenerationLog   # flush + 裁剪，不 commit
async def save_log(sessionmaker, **kwargs) -> int | None   # 独立 session + commit，吞异常（失败/取消路径）
# chapter_service
async def commit_versioned(db) -> None   # (chapter_id, seq) 唯一约束冲突 → 409
```

### 3. 契约

- 版本是**事件发生后**的正文快照；写版本时取 `chapter.content` / `chapter.word_count`，调用方须先更新这两个字段。
- 原语**都不 commit**：改正文 + 写版本 + 写日志在同一事务里由调用方提交。
- 手动保存（仅当 PATCH 的 `content` 与原正文不同；创建章节时正文非空）：新正文为空白 → 不写；
  与最新版本相同 → 不写；最新版本是未标星的 `manual` 且 `_now() - created_at < 10 分钟` → 原地更新；
  否则新建 `manual`。**固定窗口**：按版本 `created_at` 计时，合并刷新的 `updated_at` 不参与判断，
  因此长时间连续写作每 10 分钟至少留一个版本（用户决策；`test_manual_merge_window_is_fixed_from_creation`
  通过直接改写 `created_at`/`updated_at` 区分两种窗口，`_now` monkeypatch 无法区分，因为 `onupdate` 用的是数据库时间）。时间用服务端 UTC（SQLite 读出的
  naive 时间按 UTC 处理）。
- AI 覆盖 / 改写采纳 / 回滚之前 `ensure_baseline`：当前正文非空且 ≠ 最新版本内容 → 写 `baseline`。
  这些来源（`baseline`/`generate`/`polish`/`rewrite`/`restore`）永不参与合并。
- `seq = max(seq)+1`（章节内）；最新版本禁止删除（400 `不能删除最新版本`），因此序号不会复用。
- `prune`：保留最新 50 个未标星 + 全部标星；最新版本若未标星必在前 50，故永不被删。
- 回滚：`ensure_baseline` → 正文改为目标版本 → `record("restore", note="恢复自 v{seq}")`；不改标题、不改状态；
  章节 `generating` 时 409。回滚前的快照仍在列表中，再恢复它即可撤销回滚。
- 生成记录每个项目保留最近 200 条（按 id）；`write_log` 会把生成期间已被删除的章节/配置外键置空，避免外键失败。
  `prompt_tokens`/`completion_tokens` 目前恒为 NULL（provider 流式接口拿不到用量）。

### 4. 必需的测试

`backend/tests/test_chapter_versions.py`：合并窗口（monkeypatch `version_service._now`）、标星不合并、
上限裁剪（第 51 个未标星删最旧，标星保留）、删除最新版本 400、AI 覆盖补 baseline、回滚与撤销、
日志 200 条裁剪、跨用户 404。

---

## 场景：后台任务表（`tasks`，数据库即队列）

### 1. 范围 / 触发条件

`app/services/task_engine/`、`task_service.py` 以及任何新增任务类型（后续批量生成、章节分析、拆书等）。
SSE / 订阅契约见 `error-handling.md`「后台任务与 SSE 契约」。

### 2. 结构

```python
# backend/app/models/entities.py
TASK_KINDS = ("chapter.generate", "chapter.polish", "wizard.outline", "wizard.characters", "wizard.world")
TASK_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
ACTIVE_TASK_STATUSES = ("queued", "running"); FINISHED_TASK_STATUSES = (其余四个)

class Task(TimestampMixin, Base):   # tasks
    user_id, project_id?, chapter_id?, kind String(32), title String(200), resource_key String(100)?,
    status String(16), input JSON, result JSON?, output_text Text, progress int, stage String(100),
    error Text, step_cursor int, retry_of_id?, started_at?, finished_at?
    # 索引：ix_tasks_status_id (status, id)、ix_tasks_user_id_id (user_id, id)，
    # uq_tasks_active_resource：resource_key 上的部分唯一索引
    #   WHERE status IN ('queued', 'running') AND resource_key IS NOT NULL（postgresql_where + sqlite_where 同时写）
```

### 3. 契约

- **单进程前提**：调度协程与 hub 都在进程内；`start()` 时把库里所有 `running` 任务视为上次进程遗留并置为
  `interrupted`。部署必须是单进程（`Dockerfile` 中 `uvicorn` 不带 `--workers`）。改为多实例前须把领取改成
  `SELECT ... FOR UPDATE SKIP LOCKED` 并引入心跳 / 租约，否则一个实例重启会把其他实例的运行中任务误判为中断。
- **资源互斥**由部分唯一索引保证原子性：`resource_key` = `chapter:{id}` 或 `wizard:{project_id}:{kind}`。插入撞索引 →
  回滚 → 409。章节类还会先原子抢占 `chapters.status='generating'`（与插入同一事务，失败时一起回滚）。
  新任务类型若不需要互斥，`resource_key` 置空即可（`NULL` 不参与唯一约束）。
- **领取**：条件 `UPDATE ... SET status='running' WHERE id=? AND status='queued'`，影响行数为 1 才执行；
  排队取消同样用 `WHERE status='queued'` 守卫，二者竞争时只有一方成功。
- `input` 必须可 JSON 序列化，且含原始请求 `request`（重试时原样重新走 `validate_and_claim`）；章节类另存
  `chapter_id`、`prev_status`、`ai_config_id`、`source_words`。运行时按 `request.ai_config_id` 重新解析配置
  （未指定 = 运行时的默认配置）。
- `title` 在创建时生成（`第 3 章 标题 · AI 生成` / `第 3 章 · AI 润色` / `大纲向导` 等），章节标题为默认的「第N章」时不再重复拼接；章节改名或删除后保持不变。
- `output_text` 由引擎每 2 秒节流刷新、结束时最终写入；运行中的实时文本以内存 hub 为准（`/tasks/{id}/output`
  在活动任务上读 hub）。`progress` / `stage` 同理，`TaskOut` 对活动任务用 hub 的实时值覆盖库值。
- `started_at` / `finished_at` 用 `func.now()`（数据库时间）写入，不在 Python 中取时间。
- **保留**：每个用户最多保留 `MAX_FINISHED_PER_USER = 200` 个已结束任务（按 id），每次任务结束、排队取消后裁剪；
  活动任务不参与裁剪。`DELETE /tasks?finished=true[&project_id=]` 手动清理已结束任务。
- `step_cursor`：多步任务的断点，重试时继承；当前单步类型恒为 0。

### 4. 必需的测试

`tests/test_migrations.py::test_0006_tasks`；`tests/test_tasks.py`（见 `error-handling.md` 第 6 节）中的
`test_finished_tasks_pruned_to_limit`、`test_resource_conflicts`、`test_cancel_queued_task`、`test_start_recovers_crashed_running_task`。

---

## 常见错误

- 在面向用户的 service 中仅按主键查询（`db.get(Chapter, id)`）：
  这会跳过归属校验。`db.get` 只在归属已被证明的地方使用
  （任务运行阶段使用创建时已校验归属的 `chapter_id`，且 `build_job` 仍按 `task.user_id` 走 `get_owned_*`；`auth_service.get_user` 按 token subject 查询）。
- 手动构建 SQLite engine 时忘记 `PRAGMA foreign_keys=ON` —— CASCADE/SET NULL 测试会以错误的方式静默通过。
- 将 AI 生成的字符串插入 `String(n)` 列时没有截断。
- 在 `StreamingResponse` 生成器或后台任务中复用请求的 `AsyncSession`。
- 新增任务类型时在 `validate_and_claim` 里 commit：章节抢占与任务插入必须在同一事务，否则撞唯一索引时抢占无法回滚。
- 部分唯一索引只写 `postgresql_where`：SQLite 测试会建成全表唯一索引，已结束任务也会阻止新任务。
- 将某个会被向导批量替换的表的外键改为 CASCADE。
- 更新 `content` 时没有重新计算 `word_count`。
