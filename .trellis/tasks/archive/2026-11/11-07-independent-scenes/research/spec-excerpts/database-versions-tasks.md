# 场景任务规范摘录：外键、正文历史及后台任务事务

来源：.trellis/spec/backend/database-guidelines.md

来源 SHA-256：0866f54a43e9485f5fd7221948529b7a173888987cc8897b4b979f84ac9bfde3

摘录日期：2026-11-07。用于避免 native context 单文件 32768 字节截断，不替代原规范。开工先核对来源是否变化；超出摘录职责时读取原文。原文相对链接以来源文件所在目录为基准。新场景行为以本任务已审阅的 PRD／design 为准，未改动的原合同仍适用。

## 原文 326–404 行

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


## 原文 405–460 行

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


## 原文 461–526 行

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


<!-- excerpt-end -->
