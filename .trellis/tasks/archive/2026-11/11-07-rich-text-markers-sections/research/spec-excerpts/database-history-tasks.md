<!-- 自动摘录自 .trellis/spec/backend/database-guidelines.md；逐行保留原文，使用时按行号回查完整规范。 -->

<!-- .trellis/spec/backend/database-guidelines.md:405-526 -->
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
