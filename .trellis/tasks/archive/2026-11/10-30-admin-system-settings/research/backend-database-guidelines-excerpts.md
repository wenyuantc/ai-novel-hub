# 规范摘录

权威来源：`.trellis/spec/backend/database-guidelines.md`。仅截取本任务相关段落；完整规范以原文件为准。

## 原文第 228–290 行

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

## 原文第 485–515 行

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
