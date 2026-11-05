# 任务上下文摘录：database-core

来源：`.trellis/spec/backend/database-guidelines.md`，原文第1–233行。
源文件SHA256：`0866f54a43e9485f5fd7221948529b7a173888987cc8897b4b979f84ac9bfde3`。
这是2026-11-03为避免自动注入截断而保留的连续原文，不修改或降低原规范；涉及摘录外主题时按原索引读取相应章节。若原规范更新，以原文件为准。

---

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

