# 场景任务规范摘录：数据库事务、归属、字数、迁移与常见错误

来源：.trellis/spec/backend/database-guidelines.md

来源 SHA-256：0866f54a43e9485f5fd7221948529b7a173888987cc8897b4b979f84ac9bfde3

摘录日期：2026-11-07。用于避免 native context 单文件 32768 字节截断，不替代原规范。开工先核对来源是否变化；超出摘录职责时读取原文。原文相对链接以来源文件所在目录为基准。新场景行为以本任务已审阅的 PRD／design 为准，未改动的原合同仍适用。

## 原文 23–174 行

## 概览

- ORM：SQLAlchemy 2.x async（`sqlalchemy[asyncio]`），使用带类型的 `Mapped[...]` / `mapped_column(...)`。
- 驱动：生产环境在 PostgreSQL 上使用 `asyncpg`
  （默认 `DATABASE_URL=postgresql+asyncpg://ainovel:ainovel@localhost:5432/ainovel`）；
  测试中使用 `aiosqlite`（开发依赖）。
- Migration：Alembic，异步 `env.py`；版本 `0001`（初始）→ `0002`（ai_configs 模型参数）→ `0003`（users `is_admin`/`is_active`/`token_version`，见 [auth-admin.md](./auth-admin.md)）→ `0004`（ai_configs `model_type`）→ `0005`（`chapter_versions` + `generation_logs`）→ `0006`（`tasks` + `generation_logs.task_id`）→ `0007`（结构化设定：角色新列、世界观分类、组织 / 成员、关系类型 / 关系 / 关系历史、章节出场角色，见 [story-entities.md](./story-entities.md)）→ `0008`（职业等级体系：`careers` / `career_stages` / `character_careers` / `career_history`，见 story-entities.md「职业等级体系」）→ `0009`（章节分析：`chapter_analyses` / `analysis_changes`、`projects.auto_analyze`、`tasks.serial_key`，见 story-entities.md「章节分析与回写」）→ `0010`（伏笔：`foreshadows` / `foreshadow_characters` / `foreshadow_history`，见 story-entities.md「伏笔管理」）→ `0011`（大纲进阶：`chapter_plans` / `outline_snapshots`、`chapters.plan_id`、`projects.wizard_progress`，见 [outline.md](./outline.md)）→ `0012`（批量生成：`tasks.checkpoint`，任务新增 `paused` 状态与 `chapter.batch` 类型，见 [batch-generate.md](./batch-generate.md)）→ `0013`（提示词模板与写作风格：`prompt_overrides` / `prompt_override_versions` / `writing_styles`、`projects.default_style_id`、`chapters.style_id`，`projects.style` 迁移为「原风格」，见 [prompts-styles.md](./prompts-styles.md)）→ `0014`（灵感模式：`projects` 结构化字段 `synopsis` / `theme` / `tags` / `selling_points` / `audience` / `target_words`、`inspiration_sessions`，见 [inspiration.md](./inspiration.md)）→ `0015`（知识库与剧情记忆：`kb_settings` / `kb_documents` / `kb_chunks` / `story_memories` / `retrieval_chunks`，见 [retrieval.md](./retrieval.md)）→ `0016`（阅读进度：`projects.reading_progress`，见 [reader.md](./reader.md)）→ `0017`（拆书导入：`book_imports`（`user_id` CASCADE、`expires_at` 索引，与项目无关、不进备份注册表）、`generation_logs.kind` 放宽到 String(32)，任务新增 `book.import` 类型、版本来源新增 `import`，见 [book-import.md](./book-import.md)）→ `0018`（项目智能体：`agent_conversations`（`user_id` / `project_id` CASCADE）/ `agent_messages`（`conversation_id` CASCADE、`turn_task_id` → tasks SET NULL）/ `agent_changes`（`conversation_id` / `project_id` CASCADE、`turn_task_id` SET NULL），后两张表 `(conversation_id, turn_task_id, tool_call_id)` 唯一；任务新增 `agent.turn` 类型、生成记录新增 `agent.turn` kind、版本来源新增 `agent`；三张表登记为备份 `NOT_EXPORTED`；downgrade 删表并把 `agent` 版本改记为 `manual`、删除 `agent.turn` 生成记录与任务，见 [agent.md](./agent.md)）→ `0019`（Skill 与 MCP 插件：`skills`（`owner_user_id` 为空 = 全局，CASCADE；`source_skill_id` 自引用 SET NULL；`builtin_key` 唯一）/ `skill_references`（`skill_id` CASCADE）/ `mcp_plugins`（`user_id` CASCADE）、`agent_conversations.mcp_plugin_ids`（JSON，可空）、`agent_messages.skill_id`（SET NULL）/ `skill_snapshot`（JSON）；写入 4 个内置全局 Skill；三张新表是用户级数据、与项目无关，不登记备份注册表，见 [skills-mcp.md](./skills-mcp.md)）。新版本使用 `batch_alter_table`，以便 SQLite 测试能够执行。
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
    return async_sessionmaker(engine, expire_on_commit=False, info={"authorization_runtime": AuthorizationRuntime()})
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

范围内管理员写入使用 `AuditOperation.commit` 把业务与成功审计同事务提交；它在审计 flush 前先做业务 flush。章节必须传 `flush_fn=version_service.flush_versioned` 保持原版本409，Skill传 `_flush_unique`；审计错误不能误译为业务唯一键冲突。失败先整体回滚再写安全失败，通常提交后再refresh；工坊approve为保留原锁契约，先flush/refresh并生成output，再audit.commit，不得移动其既有投影时机。不得以HTTP响应成功后补写替代同事务契约，详见[审计规范](./admin-audit.md)。

没有 unit-of-work 包装。每个有写操作的 service 函数都以显式的
`await db.commit()` 结束，若需返回对象，则紧接着 `await db.refresh(obj)`。
当同一函数中后续查询需要看到变更时，使用 `await db.flush()`
（`ai_config_service.delete_config`）。

---

## 查询模式

### user_id 隔离（强制）

每个 service 入口首先校验归属。他人的记录与不存在的记录
都返回 **404**，以免泄露存在性。

人工项目内容管理允许当前具有所需projects.read/edit/recycle能力的启用账号使用get_manageable_project；写前共同gate/User屏障必须先于资源锁；AI、任务和账户资源仍用严格
`get_owned_project`，不能全局改义。例外范围与子资源规则见[管理员作品管理](./admin-projects.md)。

`backend/app/services/project_service.py`：

```python
async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """按 user_id 校验归属；他人项目与不存在统一返回 404，避免泄露存在性。"""
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    if project is None:
        raise NotFoundError("项目不存在")
    return project_lifecycle.ensure_active(project)
```

上例为只读简化形式；实际helper还刷新identity map，并支持 `lock=True` 的写事务屏障。所有修改入口必须在同一事务内获取该父项目锁；不能只在读时过滤deleted_at，然后允许旧请求晚提交。重复锁刷新数据库生命周期时必须保留已持锁事务内尚未flush的非生命周期标量/JSON修改，回归见 `test_project_lifecycle.py`。

没有 `user_id` 列的表通过 join 到归属方来限定范围
（`backend/app/services/chapter_service.py`）：

```python
async def get_owned_chapter(db: AsyncSession, user_id: int, chapter_id: int) -> Chapter:
    chapter = await db.scalar(
        select(Chapter)
        .join(Project, Project.id == Chapter.project_id)
        .where(Chapter.id == chapter_id, Project.user_id == user_id, *project_lifecycle.active_conditions())
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

计数/求和在 SQL 中完成，而不是在 Python 中：`project_service.chapter_stats_subquery()` 使用
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


## 原文 234–297 行

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
  `downgrade -1` 后存量日志保留）、`0007_story_entities.py`（`down_revision = '0006'`，只改结构不调 AI：
  `world_settings.category` 回填 `other`，`characters.always_in_context` 对 `role_type` 为「主角」/`protagonist`（trim + lower）回填 true，
  插入 20 个全局预置关系类型；`test_0007_story_entities` 断言简介 / 旧关系备注原样保留、回填结果、预置类型与
  `PRESET_RELATIONSHIP_TYPES` 一致、外键规则、`(pair_low, pair_high)` 唯一，`downgrade -1` 后存量数据保留）、
  `0008_careers.py`（`down_revision = '0007'`，只新建四张表、不回填；`test_0008_careers` 断言外键规则、两个唯一约束、
  删除职业时阶段 / 挂接 / 历史在同一语句内级联删除，`downgrade -1` 删除四张表且存量角色保留）、
  `0009_chapter_analysis.py`（`down_revision = '0008'`，新建 `chapter_analyses` / `analysis_changes`，`projects.auto_analyze`
  `server_default=true` 回填，`tasks.serial_key` 可空 + 索引；`test_0009_chapter_analysis` 断言外键规则、回填、删章节级联、
  删任务置空，`downgrade -1` 删除两表两列且存量项目保留）、
  `0010_foreshadows.py`（`down_revision = '0009'`，只新建三张表、不回填；`test_0010_foreshadows` 断言外键规则、复合主键、
  删角色只解除关联、删伏笔级联历史，`downgrade -1` 删除三张表且存量角色保留）、
  `0011_outline_pro.py`（`down_revision = '0010'`，新建 `chapter_plans` / `outline_snapshots`，`chapters` 加 `plan_id`（batch 建外键），
  `projects.wizard_progress` `server_default='{}'` 回填；`test_0011_outline_pro` 断言外键规则、`chapter_plans.chapter_id` 唯一、回填、
  删大纲级联细纲且章节保留并解除关联，`downgrade -1` 删除两表两列且存量章节保留）、
  `0013_prompt_styles.py`（`down_revision = '0012'`，新建三张表与两列，插入 6 个预设风格（迁移内冻结副本），非空 `projects.style` →
  所有者的「《书名》原风格」（description = 前 100 字、extra = 全文 ≤5000）并设为项目默认，`projects.style` 列保留不再读写；
  downgrade 把默认风格的 extra（空则 description）写回 `projects.style`；`test_0013_prompt_styles` 断言外键规则、预设与代码一致、
  迁移结果、`(user_id, key)` 唯一、往返后 style 原文无损）、
  `0014_inspiration.py`（`down_revision = '0013'`，`projects` 加 6 列（带 server_default，`target_words` 可空，不回填）、新建
  `inspiration_sessions`；`test_0014_inspiration` 断言存量项目取默认值、外键规则、删项目后会话保留且引用置空，`downgrade -1` 删除新表新列且存量项目保留）、
  `0015_knowledge_base.py`（`down_revision = '0014'`，只新建五张表、不回填；`ai_configs.model_type` 新增取值 `embedding` 不改表；
  `retrieval_chunks` 建表带 `sqlite_autoincrement`；`test_0015_knowledge_base` 断言外键规则、两个唯一约束、设置默认值、删章节后手动记忆保留且
  `chapter_id` 置空、删文档级联分块，`downgrade -1` 删除五张表且存量项目保留）、
  `0016_reading_progress.py`（`down_revision = '0015'`，`projects` 加 `reading_progress` JSON `server_default='{}'`，不回填；
  `test_0016_reading_progress` 断言存量项目取 `{}`，`downgrade -1` 删除该列且存量项目保留）、
  `0019_skills_mcp.py`（`down_revision = '0018'`，新建三张表与三列；`agent_messages` 的外键与索引在 `batch_alter_table` 内创建（显式命名
  `fk_agent_messages_skill_id_skills`，downgrade 先删约束再删列）；内置 Skill 用 `op.bulk_insert` 写入，正文读自 `app/skills/builtin/*.md`；
  `test_0019_skills_mcp` 断言三张表、内置 Skill 的 `builtin_key` 顺序、全局 slug 部分唯一索引在 SQLite 上生效且私有 Skill 可与全局同名、
  `(owner_user_id, slug)` 唯一、`skill_id` 为 SET NULL、删 Skill 级联参考资料、删用户级联插件，`downgrade -1` 删除三表三列）。
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
- **新增与项目相关的表（直接或经外键间接引用 `projects`）、或给已登记表新增外键列 / JSON 列时，必须登记到项目备份的
  表规则注册表** `app/services/project_transfer/registry.py`（或写进 `NOT_EXPORTED` 并说明理由；JSON 里存 id 的要写重映射函数），
  并同步更新 `exporter.APP_SCHEMA`；格式有不兼容变化时提升 `exporter.VERSION` 并在 `importer.migrate` 补升级步骤。
  反射测试 `tests/test_project_transfer.py::test_registry_*` 会在漏登记时失败。见 [project-transfer.md](./project-transfer.md)。

---


## 原文 527–549 行

## 常见错误

- 在面向用户的 service 中仅按主键查询（`db.get(Chapter, id)`）：
  这会跳过归属校验。`db.get` 只在归属已被证明的地方使用
  （任务运行阶段使用创建时已校验归属的 `chapter_id`，且 `build_job` 仍按 `task.user_id` 走 `get_owned_*`；`auth_service.get_user` 按 token subject 查询）。
- 手动构建 SQLite engine 时忘记 `PRAGMA foreign_keys=ON` —— CASCADE/SET NULL 测试会以错误的方式静默通过。
- 将 AI 生成的字符串插入 `String(n)` 列时没有截断。
- 在 `StreamingResponse` 生成器或后台任务中复用请求的 `AsyncSession`。
- 新增任务类型时在 `validate_and_claim` 里 commit：章节抢占与任务插入必须在同一事务，否则撞唯一索引时抢占无法回滚。
- 部分唯一索引只写 `postgresql_where`：SQLite 测试会建成全表唯一索引，已结束任务也会阻止新任务（`uq_skills_global_slug` 同理：只写 PG 条件时，SQLite 上所有 Skill 的 slug 都变成全表唯一，私有 Skill 不能再与全局或他人的 Skill 同名）。
- 以为普通唯一约束能约束可空列为 NULL 的行：`(owner_user_id, slug)` 对全局 Skill（owner 为 NULL）不生效，需要额外的部分唯一索引。
- 将某个会被向导批量替换的表的外键改为 CASCADE。
- 重新读写 `projects.style`：它自 0013 起只为 downgrade 保留；写作风格走 `writing_styles` + `default_style_id`（见 prompts-styles.md）。
- 直接改 `Relationship` 字段而不经 `apply_change`：没有历史、绕过角色对唯一与同项目校验。
- 直接改 `CharacterCareer.stage_id` / `note` 而不经 `career_service.apply_assignment`：没有挂接历史，绕过主 1 副 3 与类别校验。
- 新增全局预置数据只写进迁移：测试create_all不跑迁移。既有RelationshipType用after_create；RBAC四角色与单行授权状态由authorization_service.initialize显式初始化，测试fixture和lifespan都必须接入。
- 更新 `content` 时没有重新计算 `word_count`。
- 直接改 `Foreshadow` 字段而不经 `foreshadow_service.apply_foreshadow`：没有伏笔历史，绕过必填与关联角色校验，分析回写也无法撤销。
- 章节分析回写绕过 `writeback_service` 直接改角色 / 关系 / 挂接 / 成员：没有回写条目，无法撤销，也不受「更晚章节优先」约束。
- 新增存章节号的列却没有登记到 `chapter_numbering.CHAPTER_NO_REFS`：建章插入章节后该列不随之平移，先后判断错位（反射测试会失败）。
- 直接改 `ChapterPlan.chapter_id` / `Chapter.plan_id` 的一侧：双向冗余不一致；只经 `plan_service`。
- 用 `resource_key` 实现「同类排队串行」：它是冲突即拒绝（409）；需要排队等待用 `serial_key`。
- 新增与项目相关的表却没有登记到 `project_transfer/registry.py`：项目备份会静默丢掉这张表的数据（反射测试会失败）。

<!-- excerpt-end -->
