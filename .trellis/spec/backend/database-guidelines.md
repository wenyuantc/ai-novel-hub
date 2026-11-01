# 数据库规范

新增迁移0029（父0028）创建roles/user_roles/authorization_state/user_quotas，不重建users/projects；唯一超管仍是User.is_admin，角色AUTOINCREMENT与FK RESTRICT、两项mode/value约束、同快照投影、共同授权锁序、四创建入口与真实占用见[RBAC与配额](./rbac-quotas.md)。新实例/用户级表不登记项目备份；有新语义数据时拒绝有损downgrade。

新增迁移 `0028_user_last_login`（父0027）：User增加可空UTC `last_login_at`，存量不回填，原位ADD/DROP COLUMN不重建users父表；只在密码登录/注册自动登录的条件认证事务记录，保留核验版本快照与账号updated_at。三种生命周期的用户资源单SQL聚合、Cookie与持久化故障边界见[管理员用户详情与登录契约](./auth-admin.md)。

实例级运行配置新增 `0027_system_settings`：单行revision/稀疏覆盖、无业务外键、不进项目备份；设置/审计同事务、确认后共享对象发布及提交不确定协调见[系统设置](./system-settings.md)。不要因新增实例表向仅容纳项目相关表的NOT_EXPORTED登记无关项。

新增迁移 `0026_admin_task_monitoring`（父0025）：Task原位增加可空queued_at、admin_cancel_operation_id/admin_cancel_requested_at及标记索引，旧时钟不回填猜测值，不重建SQLite tasks父表；有pending时拒绝降级。状态/统计/清标记共享条件事务、单SQL管理筛选计数及资源屏障见[任务监控](./admin-task-monitoring.md)。

新增迁移 `0025_admin_dashboard`（父0024）：实例级跟踪起点、每日规模快照和不随任务裁剪/业务级联删除的独立任务统计；Task增加唯一UUID统计身份。升级不重建SQLite tasks父表，已有统计时拒绝破坏性降级。统计表不进入项目备份，完整事务、身份与时间边界见[管理员首页与全站统计](./admin-dashboard.md)。

> 本项目的数据库模式与约定。

新增迁移 `0020_prompt_workshop`：工坊四张用户级表、审核更新部分唯一索引、作者删号 SET NULL 与行为记录 CASCADE；详细约束与导入事务见 [workshop.md](./workshop.md)。这些表与项目无关，不登记项目备份表规则。

---

新增迁移 `0023_admin_audit_logs`（父0022）创建实例级 `admin_audit_logs`，无业务外键、快照ID保留、operation_id唯一、SQLite AUTOINCREMENT；不进项目备份。事务与受保护日期清理见[管理员操作审计](./admin-audit.md)。

新增迁移 `0024_project_recycle_bin`（父0023）：Project生命周期/删除者快照/协调token、到期查询索引与状态约束，Project→User从CASCADE改为RESTRICT；审计actor ID允许系统空值。普通DELETE改为保留30天的软删除，写入口显式锁父项目，完整契约见[作品回收站](./recycle-bin.md)。SQLite重建父表须验证存量子行、PRAGMA foreign_key_check和审计AUTOINCREMENT高水位；有回收站/删除中数据或新增生命周期审计时禁止无损假象的downgrade。

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
| `characters.role_type` | String(50)（下拉 + 兼容旧自由文本） |
| `characters.aliases` | JSON 列表；单个 ≤50、最多 10 个（`schemas.story.clean_aliases`） |
| `characters.age` / `gender` | String(50) / String(20) |
| `organizations.name` / `org_type` / `location` | String(100) / String(50) / String(200)；`(project_id, name)` 唯一 |
| `organization_members.position` | String(50) |
| `relationship_types.role_a` / `role_b` | String(20) |
| `relationship_history.note` | String(200) |
| `careers.name` / `kind` | String(100) / String(8)；`(project_id, name)` 唯一 |
| `career_stages.name` | String(50)（职业内唯一由服务层保证） |
| `character_careers.role` / `note` | String(8) / String(200)；`(character_id, career_id)` 唯一 |
| `career_history.career_name` / `note` / `source` | String(100) / String(200) / String(16) |
| `foreshadows.title` / `status` / `source` | String(200) / String(16) / String(12)；要点、内容、备注为 Text |
| `foreshadow_history.note` / `source` | String(200) / String(16) |
| `chapter_plans.title` / `emotion` | String(200) / String(100)；梗概、叙事目标为 Text；`key_events` / `character_ids` 为 JSON 列表；`chapter_id` 唯一 |
| `outline_snapshots.reason` / `note` | String(32) / String(200)；`data` 为 JSON |
| `writing_styles.name` / `description` / 维度字段（pov 等 6 个）/ `taboos` / `preset_key` | String(100) / String(200) / String(500) / String(1000) / String(32)；`sample`（≤2000，schema 限制）/ `extra`（≤5000）为 Text |
| `prompt_overrides.key` / `base_fingerprint` | String(64) / String(16)；`system` / `user` / `base_*` 为 Text；`(user_id, key)` 唯一 |
| `prompt_override_versions.note` | String(200) |
| `projects.synopsis` / `theme` / `selling_points` / `audience` | Text（≤2000，schema）/ String(200) / Text（≤500，schema）/ String(100)；`tags` 为 JSON 列表（≤8 个、每个 ≤10 字，`schemas.project.normalize_tags`）；`target_words` 可空 Integer（1000–10,000,000） |
| `inspiration_sessions.title` / `status` | String(200) / String(16)；`state` 为 JSON（`schemas.inspiration.SessionState` 校验） |
| `world_settings.category` / `characters.life_status` / 各 `status` / `category` | String(16) |
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
`AI_PROVIDERS`、`PROJECT_STATUSES`、`LIFE_STATUSES`、`WORLD_CATEGORIES`、`MEMBER_STATUSES`、`RELATION_CATEGORIES`、
`RELATION_STATUSES`、`RELATION_SOURCES`、`CAREER_KINDS`、`CAREER_SOURCES`、`ANALYSIS_CHANGE_KINDS`（含三个伏笔 kind）、`ANALYSIS_CHANGE_STATUSES`、
`FORESHADOW_STATUSES`、`FORESHADOW_SOURCES`、`OUTLINE_SNAPSHOT_REASONS`；`VERSION_SOURCES` / `GENERATION_KINDS` 含 `revise`；`VERSION_SOURCES` 含 `import`、`agent`，`GENERATION_KINDS` 含 `book_import.*`、`agent.turn`），schema 中使用 `Literal[...]` 类型。不使用数据库枚举。

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

`0007`：`organizations.project_id`、`organization_members.*`、`relationship_types.project_id`、`relationships.project_id/a_id/b_id`、
`relationship_history.relationship_id`、`chapter_characters.*` 为 CASCADE（删角色自动清理关系、历史、成员、出场记录）；
`organizations.parent_id` 为 SET NULL（删上级后下级变顶级）；`relationships.type_id` **不设 ondelete**（NO ACTION，语句结束时检查）——
服务层先查引用返回 409，且删除项目时类型与关系在同一语句内级联删除，RESTRICT 会因触发顺序失败。
角色向导整体替换角色（`delete(Character)`）会级联清掉旧关系 / 成员 / 出场记录，这是 R11 的预期语义。

`0008`：`careers.project_id`、`career_stages.career_id`、`character_careers.character_id/career_id`、
`career_history.character_id/career_id` 为 CASCADE（删项目 / 职业 / 角色时阶段、挂接、历史一并清理；AC2 要求删职业时历史也清理，
`career_name` 冗余仅用于展示）；`character_careers.stage_id` **不设 ondelete**，与 `relationships.type_id` 同理（服务层先查引用返回 409）。
挂接只能经 `career_service.apply_assignment` 写入（不 commit，写历史）。职业向导整体替换职业会级联清掉挂接与历史。

`0009`：`chapter_analyses.project_id/chapter_id`、`analysis_changes.analysis_id/project_id` 为 CASCADE（删章节时分析与回写条目一并删除，
已写入设定的变化仍留在各实体的历史里）；`chapter_analyses.task_id` 为 SET NULL（任务被清理 / 裁剪后分析保留）。
`analysis_changes` 有 `(project_id, field_key)` 复合索引，供「更晚章节优先」查询。

`0010`：`foreshadows.project_id`、`foreshadow_characters.foreshadow_id/character_id`、`foreshadow_history.foreshadow_id` 均为 CASCADE
（删角色只删关联行、伏笔保留；删伏笔时关联与历史一并删除）。伏笔的四个章节字段存章节号、**不做外键**（可指向尚未创建的章节）。
伏笔只能经 `foreshadow_service.apply_foreshadow` 写入（不 commit，写历史）。

`0011`：`chapter_plans.project_id` / `outline_id` 为 CASCADE（删大纲时细纲一并删除）、`chapter_plans.chapter_id` 为 SET NULL（unique）；
`chapters.plan_id` 为 SET NULL（模型上 `use_alter=True`：两表互相引用，PostgreSQL 建表后再加外键，SQLite 内联）；`outline_snapshots.project_id` 为 CASCADE。
细纲与章节的双向关联只经 `plan_service` 维护；在已有章节之前插入 / 删除章节必须经 `chapter_numbering.shift_chapter_numbers`
平移全部章节号引用（登记表见 [outline.md](./outline.md)）。

`0013`：`writing_styles.user_id`（为空 = 预设）、`prompt_overrides.user_id`、`prompt_override_versions.override_id` 为 CASCADE；
`projects.default_style_id` / `chapters.style_id` 为 SET NULL（删除风格后引用处回落到上一层：章节 → 项目默认 → 无）。
预设风格与 `RelationshipType` 一样：迁移写入 + `after_create` 事件（测试的 `create_all`），另外应用启动时 `style_service.sync_presets` 按 `preset_key` upsert。

`0014`：`inspiration_sessions.user_id` 为 CASCADE；`project_id`（从已有项目发起）/ `converted_project_id`（转为的新项目）为 SET NULL
（删项目后会话保留，可以再次转为项目）。

`0015`：`kb_settings.project_id`（主键）、`kb_documents.project_id`、`kb_chunks.document_id`、`story_memories.project_id`、
`retrieval_chunks.project_id` 为 CASCADE；`story_memories.chapter_id` / `analysis_id` 为 SET NULL（删章节前服务层先删该章自动记忆，
手动记忆保留、章号不变）。`retrieval_chunks.ref_id` 指向 `kb_chunks.id` 或 `story_memories.id`，**不做外键**（按 source 区分），
由业务写入点经 `retrieval_service` 同步删除；`(source, ref_id)` 唯一、`(project_id, source, enabled)` 索引；
`kb_documents (project_id, sha256)` 唯一（重复上传 409）。列长度：`kb_documents.filename` String(255) / `status` String(24) / `mime` String(100) /
`warnings` JSON；`kb_chunks.heading_path` String(500)；`story_memories.kind` String(16) / `origin` String(8) / `content_key` String(16)、
`content` ≤1000（schema）；`retrieval_chunks.source` String(8) / `embedding` LargeBinary（归一化 float32）/ `embedding_key` String(200) / `version` Integer。

`0019`：`skills.owner_user_id`、`skill_references.skill_id`、`mcp_plugins.user_id` 为 CASCADE（删用户时其私有 Skill 与插件随之删除，全局 Skill 不受影响）；
`skills.source_skill_id`（复制来源）、`agent_messages.skill_id`（强制启用的 Skill，正文另有 `skill_snapshot`）为 SET NULL。
**唯一性**：`(owner_user_id, slug)` 唯一约束不约束 NULL，全局 Skill 的 slug 另用部分唯一索引
`uq_skills_global_slug`（`Index(..., unique=True, postgresql_where=text("owner_user_id IS NULL"), sqlite_where=text("owner_user_id IS NULL"))`，
模型与迁移都要写两种方言）；`mcp_plugins (user_id, slug)` 唯一。`mcp_plugins.headers_encrypted` 是 Fernet 密文（Text，可空），`tools` / `tool_prefs` 为 JSON。
内置 Skill 与预置风格同理：迁移写入 + `after_create` 事件（测试的 `create_all`）。

关系、组织、卡片与章节上下文的写入契约见 [story-entities.md](./story-entities.md)；要点：关系只能经
`relationship_service.apply_change` 写入（不 commit，写历史），一对角色一条（`pair_low/pair_high` 唯一）。

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
  这些来源（`baseline`/`generate`/`polish`/`rewrite`/`restore`/`revise`）永不参与合并。
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
TASK_KINDS = ("chapter.generate", "chapter.polish", "wizard.outline", "wizard.characters", "wizard.world",
              "character.generate", "organization.generate", "relations.parse", "career.generate", "wizard.careers",
              "chapter.analyze", "chapter.revise", "outline.continue", "outline.expand", "chapter.batch", "kb.index",
              "book.import", "agent.turn")
TASK_STATUSES = ("queued", "running", "paused", "succeeded", "failed", "cancelled", "interrupted")
ACTIVE_TASK_STATUSES = ("queued", "running")            # 调度、占名额、占 resource_key 部分唯一索引
OPEN_TASK_STATUSES = (*ACTIVE_TASK_STATUSES, "paused")  # 未结束：「进行中」列表、批量唯一性
FINISHED_TASK_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")  # 可删除、参与裁剪；paused 不在其中

class Task(TimestampMixin, Base):   # tasks
    user_id, project_id?, chapter_id?, kind String(32), title String(200), resource_key String(100)?,
    serial_key String(100)?（索引 ix_tasks_serial_key）,
    status String(16), input JSON, result JSON?, output_text Text, progress int, stage String(100),
    error Text, step_cursor int, checkpoint JSON?, retry_of_id?, started_at?, finished_at?
    # 索引：ix_tasks_status_id (status, id)、ix_tasks_user_id_id (user_id, id)，
    # uq_tasks_active_resource：resource_key 上的部分唯一索引
    #   WHERE status IN ('queued', 'running') AND resource_key IS NOT NULL（postgresql_where + sqlite_where 同时写）
```

### 3. 契约

- **单进程前提**：调度协程与 hub 都在进程内；`start()` 时把库里所有 `running` 任务视为上次进程遗留并置为
  `interrupted`。部署必须是单进程（`Dockerfile` 中 `uvicorn` 不带 `--workers`）。改为多实例前须把领取改成
  `SELECT ... FOR UPDATE SKIP LOCKED` 并引入心跳 / 租约，否则一个实例重启会把其他实例的运行中任务误判为中断。
- **资源互斥**由部分唯一索引保证原子性：`resource_key` = `chapter:{id}`、`wizard:{project_id}:{kind}` 或 `relparse:{character_id}`（`character.generate` / `organization.generate` / `career.generate` 为空，允许并发；`wizard.careers` 为 `wizard:{project_id}:careers`；`outline.continue` 为 `outline:{project_id}`、`outline.expand` 为 `expand:{outline_id}`；`kb.index` 文档模式为 `kbdoc:{document_id}`、重建为 `kbrebuild:{project_id}`、记忆模式为空，`serial_key = index:{project_id}`；`agent.turn` 为 `agent:{conversation_id}`，同会话待审核（paused）的轮次由服务层 `active_turn` 检查 → 409）。插入撞索引 →
  回滚 → 409。章节类还会先原子抢占 `chapters.status='generating'`（与插入同一事务，失败时一起回滚）。
  新任务类型若不需要互斥，`resource_key` 置空即可（`NULL` 不参与唯一约束）。`chapter.revise` 与生成 / 润色共用 `chapter:{id}`（互斥）。
- **串行键** `serial_key`（`TaskSpec.serial_key`）：冲突时**排队等待**而不是拒绝。调度器扫描 queued 时，同键已有运行中（或本轮已登记）的任务
  就跳过该条，留到下一轮；某个同键任务因每用户 / 全局上限被跳过时，本轮后续同键任务也不越过它（同键内按 id 先进先出）。
  与全局 / 每用户上限同时生效，不同键之间并行。`chapter.analyze` 使用 `analysis:{project_id}`（同项目串行，保证回写顺序与入队顺序一致）。
- **成功回调**（可选 `handler.on_success(ctx, task)`，引擎 `getattr` 探测）：在 succeeded 写库之后、广播 `done` 之前调用，异常只记日志、
  不影响任务状态；失败 / 取消 / 中断不调用。它若新建任务（如自动分析）直接插入 queued 行即可，当前任务结束时引擎的完成回调会唤醒调度器。
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
- `step_cursor` / `checkpoint`：多步任务的断点（下一步下标 + 结构化状态），重试时由 `create_task` 从原任务复制、继续（paused → queued）时原样保留；
  单步类型恒为 0 / NULL。目前只有 `chapter.batch` 使用（结构见 batch-generate.md）。`status` / `kind` 列没有 CHECK 约束，新增取值只改常量。
- `0012` downgrade 先把 `paused` 任务改为 `cancelled`（旧代码不认识 paused）再删 `checkpoint` 列；`test_0012_batch_generate`。

### 4. 必需的测试

`tests/test_migrations.py::test_0006_tasks`；`tests/test_tasks.py`（见 `error-handling.md` 第 6 节）中的
`test_finished_tasks_pruned_to_limit`、`test_resource_conflicts`、`test_cancel_queued_task`、`test_start_recovers_crashed_running_task`、
`test_serial_key_same_key_serial_other_keys_parallel`、`test_serial_key_keeps_fifo_when_user_limit_blocks`、`test_on_success_failure_does_not_affect_task`。

---

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
