# 任务上下文摘录：database-migration-and-fk

来源：`.trellis/spec/backend/database-guidelines.md`，原文第234–404行。
源文件SHA256：`0866f54a43e9485f5fd7221948529b7a173888987cc8897b4b979f84ac9bfde3`。
这是2026-11-03为避免自动注入截断而保留的连续原文，不修改或降低原规范；涉及摘录外主题时按原索引读取相应章节。若原规范更新，以原文件为准。

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

