# Research: 任务引擎单进程状态与假设清单

- **Query**: TODOS 第 25 项规划前调研——列出任务引擎内所有依赖单进程的状态与假设，用于「多 worker / 多实例共享同一数据库时不重复调度、不误标中断」的技术设计
- **Scope**: internal（只读代码与规范，未改任何产品代码，未运行多进程实验）
- **Date**: 2026-11-20

路径均相对仓库根目录。行号以 2026-11-20 的 `main`（`515f3af`）为准。
凡标注「按代码推演」的结论来自阅读代码路径，没有用多进程实际运行验证；标注「未核实」的条目说明了原因。

## 调研涉及的文件

| 文件 | 说明 |
|---|---|
| `backend/app/services/task_engine/engine.py`（3554 行） | 调度、领取、执行、收尾、取消、订阅 |
| `backend/app/services/task_engine/hub.py` | 单任务内存 hub：实时文本、进度、订阅者队列 |
| `backend/app/services/task_engine/cancellation.py` | 管理员持久取消意图的 drain |
| `backend/app/services/task_engine/admission.py` | 受理证明（进程内弱引用登记） |
| `backend/app/services/task_engine/stream_lease.py` | 受控 SSE 的单帧发送租约 |
| `backend/app/services/task_engine/stream_evidence.py` | 受控 SSE 的帧来源证明（进程内弱引用登记） |
| `backend/app/services/task_engine/execution_identity.py` | 领取时冻结的执行身份 |
| `backend/app/services/task_engine/registry.py` | handler 注册表、`TaskContext` |
| `backend/app/services/task_engine/handlers/{agent,batch,consistency,book_import,...}.py` | 各任务类型 |
| `backend/app/services/task_service.py` | 创建、重试、继续、取消、输出、作品排空 |
| `backend/app/services/admin_task_service.py` | 管理员监控投影与取消受理 |
| `backend/app/services/task_statistics_service.py` | 任务统计事务原语 |
| `backend/app/services/agent/service.py` | 智能体提案确认、外部调用恢复、会话锁 |
| `backend/app/services/system_settings_service.py`、`backend/app/core/authorization_runtime.py`、`backend/app/services/authorization_service.py`、`backend/app/services/quota_service.py` | 准入 runtime、设置发布、配额 |
| `backend/app/services/recycle_bin_service.py` | 作品删除排空（读引擎内存） |
| `backend/app/api/v1/tasks.py`、`backend/app/api/v1/admin_tasks.py`、`backend/app/api/sse.py` | 任务路由与 SSE |
| `backend/app/main.py`、`Dockerfile`、`docker-compose.yml` | 进程装配与启动命令 |
| `backend/app/models/entities.py:796-857`、`backend/alembic/versions/0006/0025/0026/0045` | `tasks` 表结构与迁移 |

## 相关规范

- `.trellis/spec/backend/database-guidelines.md:520-576`「后台任务表」——第 552-554 行写明单进程前提，并写「改为多实例前须把领取改成 `SELECT ... FOR UPDATE SKIP LOCKED` 并引入心跳 / 租约」。
- `.trellis/spec/backend/error-handling.md:320-344`（第 6 节调度/运行/收尾/取消/重启）——第 342-344 行「重启（D2）……前提：单进程部署」。
- `.trellis/spec/backend/admin-task-monitoring.md:7`「仍为单进程数据库队列」；第 86-98 行 drain/CAS/winners/completed 契约。
- `.trellis/spec/backend/rbac-quotas.md:73-77`——准入 runtime 与领取共用，「继续沿用单进程 TaskEngine，不宣称多实例租约」。
- `.trellis/spec/backend/system-settings.md:5`「继续沿用任务引擎已有的单进程部署前提，不提供多 worker 广播」；第 37、45 行发布与消费契约。
- `README.md:96`、`README.md:1071`「单进程任务调度」限制；`TODOS.md:310-315` 第 25 项原文。

---

## 1. 生命周期：`start()` / `recover()` / `stop()`

### 1.1 进程装配

- `create_app` 每个进程构造一个 `TaskEngine`、一个 `SystemSettingsManager`、一个 sessionmaker（`backend/app/main.py:148-173`）。sessionmaker 的 `info` 里放一个进程内 `AuthorizationRuntime`（`backend/app/core/database.py:55-57`）。
- lifespan 顺序：`authorization_service.initialize` → `system_settings.load()` → `bootstrap_admin` → `sync_presets` → `purge_expired_all` → `recover_pending_deletions` → `initialize_tracking` → `task_engine.start()` → 回收站清理器 / 仪表盘采样 / 备份 runtime（`backend/app/main.py:112-129`）。
- 启动命令：`alembic upgrade head && uvicorn app.main:app ...`，不带 `--workers`（`Dockerfile:37`）。`docker-compose.yml` 只有一个 `app` 服务。若用 `--workers N`，每个 worker 进程各自跑一遍 lifespan；若起多个容器，每个容器启动时各跑一遍 `alembic upgrade head`。

### 1.2 `start()`（`engine.py:292-302`）

1. 新建 `self._wake = asyncio.Event()`，`_stopping = False`，`_paused_scan_at = None`。
2. `task_statistics_service.initialize_tracking`（第 298 行；该函数用 `ON CONFLICT DO NOTHING` + 单例行 `FOR UPDATE` 串行初始化，`task_statistics_service.py:24-41`）。
3. `await self.recover()`。
4. `await self.scan_admin_cancellations()`——为库里每个带 `admin_cancel_operation_id` 的任务在本进程起一个 drain 协程。
5. 起调度协程 `_scheduler`，`notify()` 一次。

### 1.3 `recover()`（`engine.py:436-583`）

先读出全库 `status == 'running'` 的任务（第 440 行，没有任何「属于谁」的过滤条件），只跳过 `task.id in self._running` 的（第 442 行，即本进程自己登记的）。逐条处理：

| 步骤 | 行号 | 做了什么 |
|---|---|---|
| 登记「启动遗留」 | 444-447 | `admin_cancellations.recovering[task_key] = (入队轮次, execution_key)`，之后 drain 据此把结果记为 interrupted 而不是 cancelled |
| 分析收据收尾 | 448-456 | `chapter.analyze` 且断点含 `_analysis_completion`、无管理员意图：`_complete_from_analysis_receipt` 记 succeeded，注释「旧进程已排空」 |
| 向导收据收尾 | 458-467 | `wizard.*` 含 `_wizard_completion`：`_complete_from_wizard_receipt` 记 succeeded，注释「旧进程已排空」 |
| 向导未决凭据撤销 | 468-471 | 只有 `_wizard_pending` 没有完成收据：`_release_wizard_pending`（推定「内容保存从未提交」） |
| 待核对 | 472-478 | 其余 `chapter.analyze`、未释放的 `wizard.*`：只在内存登记 `remember_abort_failure` + `remember_uncertain_execution`，任务**保持 running** |
| 富文档提案 | 479-504 | `_durable_rich_review` 找到已落库提案：`complete_state(running → paused)`；有管理员意图则跳过 |
| 单章/批量完成凭据 | 505-540 | 单章含 `_chapter_claim` 断点：`_complete_from_chapter_receipt`；批量含 `_batch_completion`：`complete_state(... interrupted, batch_receipt_proof=True)`；否则登记待核对并保持 running |
| 让给管理员 drain | 541-543 | 单章且有管理员取消意图：不处理 |
| 通用中断 | 544-577 | `handler.on_abort`（单章用 `capture_running_claim` 重建 claim，注释「仅启动恢复在旧进程排空的前提下，从持久身份重建清理证据」第 552-554 行；批量 `on_abort(recovery=True)`）→ `complete_state(running → interrupted, error="服务重启，任务已中断")` → `close_hub("error")` |
| 外部调用 | 578-583 | `recover_external(db, stale_only=False)`：把全库所有仍为 `executing` 的外部（MCP）提案判为失败，不看是否过期 |

`on_abort` 做的收尾因任务类型而异：单章回退 `chapters.status='generating'` 与占用、批量 `restore_current` 回退当前章（`handlers/batch.py:323-359`）、协作智能体轮次作废待确认提案（`handlers/agent.py:747-759`）。生成记录的 cancelled 行由 runner 自己的 `finally` 写（`engine.py:10-11` 文件头），`recover()` 不补写。

**假设「此刻没有任何其他执行体」的收尾**：上表除「登记启动遗留」外全部如此。依据：

- 选取条件是全库 running，没有 owner/心跳列可区分（见第 7 节）。
- 代码注释三处直接写「旧进程已排空」（第 451、462、513、552 行）。
- `recover_external` 的文档串写「stale_only=False 用于进程启动：单进程部署下启动时不可能有正在进行的确认请求」（`agent/service.py:153-154`）。

收据类收尾（分析/向导/单章）都是按原 UUID + 入队轮次 + `execution_key` + `status='running'` 的条件 UPDATE；另一进程里活着的执行体随后做自己的 `complete_state` 时会发现行已不是 running。两者并发的最终状态**未核实**（需要专门推演或多进程测试，本次只读代码）。

> 若要支持多进程，这里最少需要：`recover()` 只处理能证明「执行者已不在」的 running 行（需要一个可持久判断的执行者/存活依据），`recover_external(stale_only=False)` 同理不能在有其他进程存活时无条件执行。

### 1.4 `stop()`（`engine.py:304-335`）

- 置 `_stopping`、唤醒并等调度协程退出（最多 5 秒）。
- 取消本进程的协作复查协程（第 317-321 行）。
- 只对 `self._running` 里的任务加 `_shutdown` 标记并 `cancel()`（第 322-329 行），它们在 `_run` 里被记为 interrupted（第 1424-1425、1463-1464 行）。
- 等本进程的管理员 drain 与 `_cleanup_work` 结束（第 331-334 行）。

`stop()` 只动本进程内存里登记的任务，不读库里的其他 running 行。

> 若要支持多进程，这里最少需要：无（只影响本进程任务）；但滚动重启时被 stop 的 worker 上的任务仍按现有语义记 interrupted，不会转给其他进程。

### 1.5 lifespan 中与引擎并列的其他启动恢复（引擎目录之外，一并记录）

- `recycle_bin_service.recover_pending_deletions`：文档串「仅在单进程启动、引擎尚未运行时调用」，把所有未进回收站作品的 `deletion_started_at` / `deletion_token` 清空（`recycle_bin_service.py:507-515`，调用点 `main.py:121`）。多进程下新 worker 启动会清掉其他进程正在进行的删除排空标记。
- `RecycleBinCleaner`、`DashboardSampler`、`BackupRuntime`、`usage_maintenance`、`sync_presets`、`purge_expired_all`、`bootstrap_admin`：每个进程各起一份。它们在多进程下是否已有数据库级单飞**未核实**（不在本次调研范围，未读实现）。

---

## 2. 领取与调度

### 2.1 dispatch 循环（`engine.py:654-706`）

- `settings_confirmed` 为假时直接返回（第 655-656 行；该标志是进程内的，见第 8 节）。
- 扫描：`SELECT id, statistics_key, user_id, serial_key, project_id, queued_at FROM tasks ... WHERE status='queued' AND execution_key IS NULL AND admin_cancel_operation_id IS NULL AND <作品可见> ORDER BY id`（第 660-667 行）。没有 `LIMIT`，没有行锁，没有 `SKIP LOCKED`。
- 每用户上限的**限额值**查库：`quota_service.task_limits`（第 668 行；`quota_service.py:54-57` 读 `UserQuota`，`inherit` 时取进程内 `settings.task_max_per_user`）。
- 每用户**已用数**数内存：`Counter(self._running_users.values())`（第 669、690 行）。
- 全局**已用数**数内存：`len(self._running) >= settings.task_max_concurrency`（第 678 行）。
- `serial_key` 判定只看内存：`busy_serial = set(self._running_serial.values())`（第 671、682 行）。全文件没有任何按 `serial_key` 查库的语句（`grep serial engine.py` 只命中第 7、261、661-702、743 行）。
- 通过检查后先在内存登记（`_running_projects/_running_users/_running_rounds/_running_statistics_keys/_running_serial/_running`），再 `asyncio.create_task(self._claim_and_run(...))`（第 695-705 行）。

### 2.2 领取事务 `_claim_and_run`（`engine.py:1176-1370`）

锁序：进程内 `self.runtime.hold()`（`asyncio.Lock`，第 1182 行）→ 数据库全局门 `authz.gate(db)`（对 `authorization_state` 单行做 `UPDATE`，`authorization_service.py:105-127`）→ `lock_users`（actor/作者 User 行 no-op UPDATE，第 1199 行）→ 单章类取发送写门禁 → `project_lifecycle.lock_active` → `SELECT ... FOR UPDATE` 原 queued 行（第 1214-1220 行）。

锁内再做一次额度检查，仍然数内存：`self.admitted_count() >= task_max_concurrency`、`self.admitted_count(user_id) >= limit`（第 1261-1265 行；`admitted_count` 遍历 `self._admitted`，第 752-753 行）。

领取本身是条件 UPDATE（第 1285-1297 行）：

```
UPDATE tasks SET status='running', execution_key=<本次 uuid4>, started_at=now(), stage=...
WHERE id=? AND statistics_key=? AND queued_at=? AND user_id=? AND project_id=? AND kind=?
  AND status='queued' AND execution_key IS NULL AND <作品可见> AND admin_cancel_operation_id IS NULL
```

`rowcount != 1` 即放弃。提交确认丢失时不重放，循环重读原 UUID + `execution_key` 核实（第 1319-1352 行）。

### 2.3 多进程下的结论

| 项 | 依赖 | 多进程下 |
|---|---|---|
| 同一任务被两个进程领取 | 数据库：全局门行锁 + 行锁 + 条件 UPDATE 的 `rowcount` | **已经原子**。两个进程都会把同一批 queued 当候选并各起 `_claim_and_run`，但只有一个 CAS 命中；另一个在第 1189 或 1221 行读不到 queued 行后返回（不记 `_stalled`）。SQLite 下多进程争用文件写锁时的 busy/超时行为**未核实**（未运行） |
| `resource_key` 互斥 | 数据库：部分唯一索引 `uq_tasks_active_resource`（`entities.py:804-810`，条件 `status IN ('queued','running') AND resource_key IS NOT NULL`，第 145 行），在插入（`task_service.py:638-643`）与继续（`task_service.py:745-750`）时撞索引 → 409 | 无影响 |
| 章节占用、知识库文档占用 | 数据库列（`chapters.generation_claim_*`、`kb_documents.index_*`），领取同事务激活（`engine.py:1301-1315`） | 无影响 |
| `serial_key` 串行 | **仅内存** `_running_serial` | **破坏正确性**：两个进程可同时运行同键任务。用到串行键的类型：`chapter.analyze` = `analysis:{project_id}`（`handlers/analysis.py:51`，规范写「保证回写顺序与入队顺序一致」`database-guidelines.md:560`）、`kb.index` = `index:{project_id}`（`handlers/kb.py:40`） |
| 全局上限 `TASK_MAX_CONCURRENCY` | 内存 `len(self._running)` / `admitted_count()` | **突破上限**：N 个进程各算一份，实际上限 ≈ N × 配置值 |
| 每用户上限 | 限额值查库，已用数数内存 | **突破上限**：同上，≈ N × 限额；`limited 0`（禁止准入）仍有效，因为 0×N=0 |
| 同键 FIFO（被跳过的任务占住队首） | 内存 `busy_serial` + `_stalled` | 破坏：另一进程不知道本进程的退避记录 |
| 排队位次 | 查库：`queue_position` 数 `status='queued' AND id < ?`（`task_service.py:347-351`）；广播时再减去本进程 `_running`（`engine.py:716`） | 体验降级（位次广播只到本进程 hub） |

> 若要支持多进程，这里最少需要：把全局/每用户已用数与 `serial_key` 占用的判定改为基于数据库事实（在领取事务的锁内判定）；领取 CAS 本身不必改。

---

## 3. 唤醒

### 3.1 `notify()` 的实现

`notify()` 清空本进程退避表 `_stalled` 并 `self._wake.set()`（`engine.py:337-341`）。`_wake` 是进程内 `asyncio.Event`，没有任何跨进程通道：在 `backend/app` 与 `pyproject.toml` 中检索 PostgreSQL `LISTEN`/`pg_notify`、redis、celery、kafka、`multiprocessing`、文件锁，均无相关实现（命中的只有 `.notify()` 方法名与 SQLAlchemy `event.listen`）。

### 3.2 调用方清单

| 调用方 | 位置 |
|---|---|
| 引擎自身：`start()` / `stop()` | `engine.py:302`、`engine.py:310` |
| 任务执行体结束回调 `_on_done`（直接 `_wake.set()`，不清退避） | `engine.py:730-748` |
| 管理员 drain 收尾 | `cancellation.py:479` |
| 协作成员变更 `notify_collaboration_change`（邀请/改角色/撤权 5 个路由传入） | `engine.py:2893-2917`；`api/v1/collaboration.py:49,58,69,116,125` |
| 创建任务（所有类型的统一入口） | `task_service.py:662` |
| 继续任务 `resume_task` | `task_service.py:760` |
| 排队取消（含旧文档任务清理） | `task_service.py:1114`、`task_service.py:1258` |
| 系统设置发布（`_publish` → 构造时注入的 `lambda: app.state.task_engine.notify()`） | `system_settings_service.py:68`；`main.py:161-163` |
| 用户配额修改 | `quota_service.py:114` |
| 人工记忆写入后排向量任务 | `api/v1/memories.py:80` |
| 富文档提案采纳 | `rich_document_review.py:1238` |
| 分析派生记忆索引任务 | `analysis_service.py:1911` |
| 智能体：提案确认提交后、协作者记忆向量排队、撤销后重排 | `agent/service.py:1404`、`1525`、`1612` |
| 智能体工具预览用的窄接口 `DeferredEngine.notify`（不调度） | `agent/tools/tasks.py:166-176` |

成功回调里直接插 queued 行的路径（如 `analysis_service.enqueue_auto`，`analysis_service.py:943-`）不调 `notify()`，靠当前任务结束时的 `_on_done` 唤醒（`database-guidelines.md:561-562`）。

### 3.3 另一进程插入的 queued 任务本进程怎么知道

只有周期轮询兜底：`_scheduler` 用 `asyncio.timeout(self.poll_interval)` 等 `_wake`，超时即清退避并照常执行 `scan_admin_cancellations()` → `_maybe_scan_collaboration_paused_soon()` → `_dispatch()`（`engine.py:633-652`）；`poll_interval = 5.0`（第 233 行）。所以其他进程插入的 queued 任务、继续的任务、写入的管理员取消意图，本进程最迟约 5 秒后看到。

待审核协作任务的全量复查另有 `collaboration_scan_interval = 15.0`（第 237 行），文档串写明目的包含「覆盖进程重启与跨实例未收到通知」（`engine.py:1024-1025`）。

> 若要支持多进程，这里最少需要：无（5 秒轮询已保证最终被领取）；若要求亚秒级领取延迟才需要跨进程唤醒通道。

---

## 4. 实时流（hub）

### 4.1 `TaskHub` 保存的内容（`hub.py:38-69`）

- 身份：`task_id`、`statistics_key`、`queued_at`、`execution_key`。
- 实时值：`status`、`text`（累计输出全文）、`progress`、`stage`、`queue_position`。
- 终止载荷：`result`（done）、`paused`；`closed`、`dirty`（自上次刷库后是否有新内容）。
- 订阅者：`_subscribers: set[Subscriber]`，每个订阅者一个 `asyncio.Queue(maxsize=1000)`（`hub.py:13,20`）。
- 受控流来源：`_source`、`text_source`、`_mixed_text_source`、`terminal_source`，以及两个 `asyncio.Event`：`source_changed`、`terminal_ready`。

引擎持有 `self.hubs: dict[int, TaskHub]`（`engine.py:246`）。hub 的建立时机：创建任务（`task_service.py:656-661`）、继续任务（`task_service.py:754-759`）、领取（`engine.py:1359`）、订阅时本进程没有 hub（`engine.py:3225-3229`）。hub 的关闭/移除只发生在本进程的收尾路径（`_finalize` 第 2068-2079 行、`recover`、取消路径、drain `_after_completion`、`forget_project_hubs`）。

落库节奏：`output_text`/`progress`/`stage` 只在 runner 产出事件且 `hub.dirty` 且距上次 ≥ `flush_interval`（2 秒）时刷库（`engine.py:1655-1658`、`_flush` 第 1663-1714 行），终态时再写一次。

### 4.2 在活动任务上读 hub 而不读库的路径

| 读路径 | 位置 | 读 hub 的条件 |
|---|---|---|
| `GET /tasks/{id}/output` | `task_service.py:901-926`（hub 分支第 909-916 行） | 本进程有 hub、`statistics_key` 相同、任务为 queued/running。**不比对 `execution_key`** |
| `TaskOut` 的 progress/stage 覆盖（`GET /tasks`、`GET /tasks/{id}`、创建/继续/取消的返回值、SSE 快照） | `task_service.py:324-332` | 本进程有 hub、UUID 相同、活动状态、`queued_at` 相同、`task.execution_key is None or hub.execution_key == task.execution_key` |
| `TaskOut` 的「结果待核对」覆盖 | `task_service.py:333-334` | 本进程 `_uncertain_executions` 集合命中 |
| 管理员监控的 progress/stage | `admin_task_service.py:101-105` | 本进程有 hub、UUID 相同、未关闭、活动状态。**不比对 `execution_key`** |
| 配额页「已用并发」 | `quota_service.py:83` | `engine.admitted_count(user_id)`，纯内存 |
| SSE 订阅 | `engine.py:2969-3490` | 见 4.3 |

### 4.3 订阅请求落在没有运行该任务的进程时（按代码推演）

`subscribe()` 先读库（`read_visible`，第 3205 行）。之后分支：

1. **任务已结束或待审核**：走持久回放——用库里的 `output_text` 发 `snapshot`，再发带持久 `result`/`error` 的终止帧，然后结束（第 3221-3251 行，`_finished_events` 第 3543-3547 行）。**任何进程都能正确服务**，不依赖 hub。
2. **任务活动、本进程没有 hub**：`ensure_hub` 用库里当前的 `output_text/progress/stage/execution_key` 建一个本地 hub（第 3225-3229 行）并注册订阅。
   - 作者（非受控）订阅：收到一帧 `snapshot`（文本是建 hub 那一刻的库值，最多滞后约 2 秒），此后本地 hub 永远收不到 delta/progress/done——这些只发布到执行进程的 hub。订阅循环每 `heartbeat_seconds`（15 秒）超时一次，只检查「任务行是否还可见」，可见就发心跳并继续（第 3430-3442 行），**不检查任务是否已结束**。结果是：不 404、不报错，流在快照后只有 `: ping`，任务在别的进程结束后也不会收到终止帧，直到客户端自己断开重连（重连后命中分支 1）。
   - 协作者（受控）订阅：本地 hub 没有可证明来源。运行中且库里已有文本时 `snapshot_source()` 返回 `None`（`hub.py:66,129-132`：`_mixed_text_source = bool(text)`），`checked_frame` 因 `source is None` 返回 `None`（第 3183-3185 行）→ 发 `error {detail: "任务访问权限已失效", code: "permission_denied"}` 后结束。运行中且文本为空时进入 `awaiting_claim`，等 `claim_source_wait_seconds`（5 秒）拿不到来源后同样以 `permission_denied` 结束（第 3372-3392、3492-3515 行）。排队中则能发出一帧持久快照，之后只有心跳。
3. **任务活动、本进程有残留 hub**（本进程创建/继续过该任务，或先前有人在本进程订阅过）：`ensure_hub` 复用它（第 607-612 行只在身份不一致时替换），其 `text/progress/stage` 停在建立时的值；`/output`、`TaskOut`、管理员监控也会读到这些旧值（4.2 表）。

残留 hub 不会被本进程清理：`subscribe` 的已结束分支只把局部变量置 `None`（第 3237-3238 行），不从 `self.hubs` 移除。

### 4.4 `stream_lease.py` / `stream_evidence.py` / `SendGuard` 的依赖

| 机制 | 位置 | 依赖 |
|---|---|---|
| 帧来源登记 `_PUBLICATIONS` / `_CLAIMS` / `_QUEUED` / `_TERMINALS` / `_FRAMES` | `stream_evidence.py:128-130,161-162` | 模块级 `WeakKeyDictionary`，键是引擎在**本进程**签发的证据对象，值里含对 hub 的 `weakref`。文件头第 1 行写明「受控任务流的进程内帧来源；不序列化到响应或日志」。校验用对象身份比较（如 `state[1] is not source.guard`，第 151 行；`_publication_hub(latest) is hub`，第 827 行） |
| 运行中帧的校验 `verify_stream_frame` | `stream_evidence.py:718-860` | 需要：本进程的 `PublicationSource`/`ClaimSource`/`QueuedSource` 登记 + 本进程 hub + 执行体 job 上的 `SendGuard` 对象。只有执行该任务的进程具备 |
| 持久回放帧的校验 `_verify_persisted_replay` | `stream_evidence.py:399-429` | 只认同一持锁行重算投影；来源是 `TaskViewEvidence`（值比较）。`_FRAMES` 登记发生在订阅所在进程自己（`frame_evidence`，第 279-285 行）。**任何进程可用** |
| 等待终态/下一来源 `TerminalEvidencePending` / `PublicationSourcePending` | `stream_evidence.py:86-121` | 等本进程 hub 的 `terminal_ready` / `source_changed` 事件 |
| 单帧发送租约登记 `_LIVE_STREAM_LEASES` | `stream_lease.py:67,365-399` | `WeakKeyDictionary[AsyncSession, _LeaseState]`，绑定当前协程、根事务、物理连接、驱动连接对象。是「同一响应内」的证明，不跨请求，多进程本身不影响 |
| SQLite 共享读租约 `_SQLITE_SHARED_FRAME_LEASES` | `stream_lease.py:70,204-224` | 仅 SQLite 非 WAL：显式 `BEGIN` + 读一页，让该连接真实持有 SQLite SHARED 锁，用来阻塞撤权等写方的 COMMIT。**依赖 SQLite 连接级（文件级）锁** |
| 帧租约里的数据库锁 `_task_frame_lease` | `stream_lease.py:195-291` | `require_authenticated_for_send`（PG `FOR SHARE` User 行；SQLite 为 User no-op UPDATE 或上面的共享读事务，`authorization_service.py:260-275`）、`ai_dispatch._lock(write=False)`（PG 共享 advisory 锁；SQLite 见第 9 节）、`Project`/`Task` 行 `FOR SHARE`（第 230、256 行）、`ai_config_service.lock_config_owner`。都是数据库锁 |
| `SendGuard.require_task_read` / `require_terminal_read` | `collaboration_ai_guard.py:738-856` | guard 对象在执行体的 job 上（进程内）；校验时要求 `require_task_stream_physical_lease`（即本进程 `_LIVE_STREAM_LEASES` 登记）+ 一系列数据库共享锁内重验 |
| 出站发送租约 `_ACTIVE_SEND_LEASES` | `ai_dispatch.py:95,525-570` | `dict[AsyncSession, _SendLease]`，同一次发送内的证明；发送只发生在执行进程，多进程本身不影响 |

> 若要支持多进程，这里最少需要：对最小目标（不重复调度、不误标中断）无需改动；但订阅/输出/监控要么保证路由到执行进程，要么让非执行进程在活动任务上退回读库并能感知终态，否则作者流挂起、协作者流 `permission_denied`。

---

## 5. 取消

### 5.1 本人取消 `POST /tasks/{id}/cancel`

路由 `api/v1/tasks.py:92-99` → `task_service.cancel_task`（`task_service.py:1010-1034`）→ `_cancel_loaded_impl`（第 1118-1331 行）。按状态：

- **queued**（第 1150-1262 行）：纯数据库。文档/章节/批量类把终态与占用释放放在同一事务的条件 UPDATE 里；其他类型先把 `execution_key` 写成一个清理标记（第 1186-1193 行），`on_abort` 后再 `complete_state(queued → cancelled)`。然后 `close_hub` + `notify()`。唯一的内存依赖是 `engine._failed_cleanups`（第 1179 行）与 `remember/forget_abort_failure`。
- **paused**（第 1263-1299 行）：纯数据库条件 UPDATE + `discard_pending` + 可选 `on_abort`。
- **running**（第 1300-1330 行）：先提交请求侧事务，再看 `engine._running.get(task.id)`：
  - 在本进程：`engine.cancel_running(...)`（`engine.py:2836-2857`）——核对内存里的 UUID/轮次/执行键后对 `asyncio.Task` 调一次 `cancel()`，`asyncio.wait({t}, timeout=10)`。
  - **不在本进程**（第 1304-1326 行）：注释「结束状态落库曾失败的孤立 running；单进程中已无执行体，可重试幂等收尾」。直接 `_abort_loaded_task`（`handler.on_abort`，第 1705-1723 行）→ 条件 UPDATE `running → cancelled, execution_key=NULL` → 关本地 hub。

**取消请求落在其他进程时（按代码推演）**：

- `chapter.analyze` 运行中：第 1132-1135 行，本进程没有执行体 → 409「分析执行结果尚未核对，不能取消」。
- `wizard.*` 运行中：第 1139-1147 行 → 409「向导执行结果尚未核对，不能取消」。
- 其余类型运行中：被当作孤立 running，`on_abort` + 库里改成 cancelled。真正的执行体在另一个进程里没有被 `cancel()`，继续跑到它下一次事件核验或刷库：所有 handler 都是 `execution_identity_aware`（`grep execution_identity_aware handlers/`），每个事件都经 `_event_task` → `require_write_execution` → `matches_running`（`engine.py:1512-1552`、`execution_identity.py:210-269`）核对库里仍是同一 `execution_key` 的 running，不符即失败关闭。期间已发出的模型请求不会被打断。`on_abort` 与活执行体并发的资源后果**未核实**。

### 5.2 管理员取消 `POST /admin/tasks/{task_key}/cancel`

路由 `api/v1/admin_tasks.py:34-37` → `admin_task_service.cancel_task`（`admin_task_service.py:188-257`）。

**持久化了什么**：与成功审计同一事务的条件 UPDATE 写入 `tasks.admin_cancel_operation_id`、`tasks.admin_cancel_requested_at`（第 229-246 行）；单章类另在 `input` 里写 `_admin_chapter_cancel` 意图，冻结原 Task/轮次/执行键/章节 claim（第 232-237 行；`cancellation.py:37-53`）。提交后调用本进程的 `engine.request_admin_cancel`（第 256 行）。

**由谁消费**：`AdminCancellationDrain`（`cancellation.py:81-481`）。

- `request()` 按 `(task_key, operation_id)` 在本进程单飞起 `_drain` 协程（第 92-98 行）。文件头第 1 行写「单进程 single-flight」。
- `scan()`（第 100-133 行）读出全库 `admin_cancel_operation_id IS NOT NULL` 的任务并逐个 `request()`；调度循环每轮都调（`engine.py:646`），启动时也调（`engine.py:300`）。

**能否被另一进程消费**：能——意图是持久的，每个进程的 `scan()` 最迟约 5 秒都会看到并各起一个 drain。但 `_drain` 判断「原执行体是否已停止」全靠本进程内存（按代码推演）：

- `engine._running.get(task.id)`（第 289 行）、`engine._claiming/_finalizing/_admitted`（第 305-306 行）、`self.winners`（第 309 行）、`engine._cleanup_work`（第 387、416 行）、`self.recovering`（第 392、419、443 行）。
- 非执行进程里这些都是空的，于是对运行中任务走「孤立 running」分支：单章 `_complete_chapter`（第 411-427 行，注释「孤立 running 仅在单进程中确认原执行体及其他项目清理都已停止后接管」）；批量按持久断点 `release_uncertain_attempt`（第 365-403 行，注释「旧执行及其独立清理均已停止后」）；分析/向导登记待核对并返回（第 329-364 行）；其他类型 `on_abort` + `complete_state`（第 439-453 行）。
- 执行进程里的 drain 则走 `cancel_running`（第 290-303 行）。两个进程的 drain 会并发处理同一意图。

本人取消遇到已有管理员意图时转 `_wait_admin_cancel`（`task_service.py:1334-1348`），等的是**本进程**的 drain 协程。

> 若要支持多进程，这里最少需要：运行中取消要能送达执行进程（执行进程自己消费持久取消意图即可，非执行进程不得把他人的 running 当孤立）；「孤立 running」的判定需要跨进程可证明的依据。

---

## 6. 暂停 / 继续 / 重试 / agent 确认

任务到达审核点时 runner 产出 `paused`，执行体结束，任务行写成 `paused`（`engine.py:12-14` 文件头、`_run` 第 1449-1450 行、`complete_state`）。暂停期间**没有任何内存中的 future/事件在等待**——执行体已经退出，断点在 `tasks.checkpoint` / `step_cursor`。

| 操作 | 位置 | 只改库 + notify？ | 进程内依赖 |
|---|---|---|---|
| 继续 `POST /tasks/{id}/resume` | `task_service.py:699-761` | 是。条件 UPDATE `paused → queued`，换新入队轮次 `queued_at`（第 735-741 行），撞资源索引 → 409；然后 `ensure_hub` + `notify()` | 只有本地 hub 与唤醒。任何进程都能在下一轮领取并由 `build_job` 从库重建（批量 `handlers/batch.py:202-208`） |
| 提前结束 `POST /tasks/{id}/finish` | `task_service.py:764-819` | 是，纯条件 UPDATE，连 notify 都没有 | 无 |
| 重试 `POST /tasks/{id}/retry` | `api/v1/tasks.py:102-146`、`task_service.py:666-696` | 是。插入一条新 queued 任务（新 UUID，`retry_of_id` 指回原任务）+ `notify()` | 受理证明 `_RETRY_SOURCES` / `_ADMISSIONS` / `_CONSUMED`（`admission.py:38,91,92`）与 `project_ai_config._PREFLIGHTS`（`project_ai_config.py:132`）是进程内弱引用登记，但签发与消费都在**同一个 HTTP 请求**内，不跨请求 |
| 富文档提案采纳 | `rich_document_review.py:1238`（`api/v1/tasks.py:171-175`） | 是，提交后 `notify()` | 未发现其他引擎内存依赖（只检索了对 engine 的引用） |
| 批量暂停/继续 | `handlers/batch.py` | 是 | `BatchHandler._jobs`（第 145、210-219、305-309 行）只在执行进程里供 `progress()` 读；`task._batch_job`（第 207、328 行）是执行体持有的游离对象 |
| agent 提案确认/拒绝 | `agent/service.py:1320-1414` `decide_change` / `_decide_change`；整轮 `decide_turn` 第 1417- | 基本是：锁内改 `AgentChange` 状态、执行工具效果、提交 → `engine.notify()`（第 1404 行）→ `maybe_resume`（第 1277-1317 行）在没有剩余 `proposed/executing` 时调 `resume_task` | ① `conversation_lock(cid)`：进程内 `asyncio.Lock`（第 68、331-337 行），注释「SQLite 无 FOR UPDATE；单进程任务引擎再以会话锁串行化 HTTP 操作」；另有 `AgentConversation` 行 `FOR UPDATE`（第 1385 行）。② `recover_external(stale_only=True)` 用 `engine.project_work()` 与 `engine._cleanup_work` 判断「本进程是否有请求正在执行这个外部调用」（第 207-212 行），另有 2 分钟过期阈值 `EXTERNAL_STALE_AFTER`（第 73、183、205 行）。③ `_execute_external` 用 `engine.cleanup_scope(task)` 登记（第 1012-1020 行） |
| agent 删除会话 / 发送消息 / 撤销 | `agent/service.py:706,743,1628,1648` | 同样套 `conversation_lock` | 同上 ① |

同会话互斥本身在数据库：`resource_key = agent:{conversation_id}` 的部分唯一索引 + `active_turn` 查 `OPEN_TASK_STATUSES`（`agent/service.py:392-401`、`handlers/agent.py:355,395`）。

依赖内存 future / 事件 / 锁的操作汇总：运行中取消（`asyncio.Task.cancel` + `asyncio.wait`）、`_wait_admin_cancel`（等本进程 drain）、作品删除排空（`engine.project_work` + `asyncio.wait`，`task_service.py:1634-1702`、`recycle_bin_service.py:131,180-203,222`）、`conversation_lock`、受控流的 `_await_claim_source` / `terminal_ready`、`_paused_scan_lock`、准入 `runtime.hold()`。

> 若要支持多进程，这里最少需要：继续/重试/提前结束无需改动（只差唤醒，已有 5 秒轮询）；agent 确认在 PostgreSQL 上还有行锁兜底，在 SQLite 上 `conversation_lock` 是唯一串行化手段。

---

## 7. 执行身份与 `tasks` 表现有列

### 7.1 `TaskExecutionIdentity`（`execution_identity.py:54-79`）

冻结 dataclass，**只存在于领取进程的内存**。字段：`task_id`、`task_key`（即 `statistics_key`）、`actor_id`、`project_id`、`chapter_id`、`kind`、`resource_key`、`serial_key`、`retry_of_id`、`queued_at`、`execution_key`、`grant_identity`、`step_cursor`、`actor_token_version`、`input_json`、`checkpoint_json`。

由 `from_queued_task(task, execution_key)` 在原 queued 行锁内构造（第 82-123 行，调用点 `engine.py:1270`）。其中落库的只有三样：

- `tasks.statistics_key`——任务 UUID，创建时生成（`entities.py:815-817`）。
- `tasks.queued_at`——当前入队轮次；继续时换新值（`_next_queued_at`，`task_service.py:87-93`，调用点第 740 行）。
- `tasks.execution_key`——领取者生成的 `uuid4`，与 `queued → running` 同一条 UPDATE 写入（`engine.py:1268,1294`；`entities.py:818-819`，`String(36)`，CHECK 约束长度 36）。

其余字段是对当时行内容的快照，之后每次写入/发布都用 `matches_running` / `require_write_execution` 对库里当前行逐字段重验（第 126-152、210-269 行）。

### 7.2 `execution_key` 目前承担的含义（不止「正在运行」）

| 状态组合 | 含义 | 位置 |
|---|---|---|
| `running` + 非空 | 本次领取的执行键 | `engine.py:1294` |
| `succeeded` + 非空 | 成功回调（如自动分析派生）尚未完成；完成后由 `clear_finished_execution` 清空 | `engine.py:2707,2769-2779` |
| 失败终态 + 非空 | 收尾失败（`not abort_ok`）故意保留的屏障 | `engine.py:2707` |
| `queued` + 非空 | 本人取消正在清理的标记（`cleanup_key`） | `task_service.py:1186-1193`；调度扫描只取 `execution_key IS NULL`（`engine.py:663`） |
| 管理员取消的单章终态 + 非空 | 可恢复收据的 `completion_key` | `cancellation.py:206-241` |

重试、继续、删除、裁剪都把「`execution_key` 非空」当作「仍在安全收尾」而拒绝（`task_service.py:679,706,1151,1264,1465`）。

### 7.3 有没有可复用为「租约持有者」的字段

`tasks` 模型全部列（`entities.py:813-857`）：`id`、`statistics_key`、`execution_key`、`user_id`、`project_id`、`chapter_id`、`kind`、`title`、`resource_key`、`serial_key`、`status`、`input`、`result`、`output_text`、`progress`、`stage`、`error`、`step_cursor`、`checkpoint`、`retry_of_id`、`started_at`、`finished_at`、`queued_at`、`admin_cancel_operation_id`、`admin_cancel_requested_at`，加 `TimestampMixin` 的 `created_at` / `updated_at`（`models/base.py:19-23`）。

- **没有** heartbeat / owner / worker / lease / 过期时间之类的列。全后端 `grep -E "heartbeat_at|lease_expires|worker_id|claimed_by|locked_by|locked_until|SKIP LOCKED"` 只命中 `engine.py:6` 的文件头注释。
- 给 `tasks` 加过列的迁移：`0006_tasks`（建表）、`0009`、`0012`（`checkpoint` 等）、`0025_admin_dashboard`（`statistics_key`）、`0026_admin_task_monitoring`（`queued_at`、`admin_cancel_operation_id`、`admin_cancel_requested_at`）、`0045_kb_index_identity`（`execution_key`）。最新迁移是 `0065_scene_moves`，0046 之后没有再动 `tasks` 的列。
- `execution_key` 标识的是「哪一次领取」，不标识「哪个进程」，也没有存活信息。
- `updated_at` 带 `onupdate=func.now()`，会被刷库 UPDATE 顺带更新，但刷库只在 runner 有事件且 `hub.dirty` 时发生（`engine.py:1655-1658`），等待慢模型响应头期间不更新；它不是周期心跳。
- `started_at` 只在领取时写一次。

> 若要支持多进程，这里最少需要：一个能让其他进程判断「这次领取的执行者是否还活着」的持久依据（现有列都不提供）；`execution_key` 可继续作为领取代次的 fencing 值，但它已被多种收尾标记复用，不能直接等同于「正在被某进程持有」。

---

## 8. 引擎内其他进程内状态（逐项）

后果分级：**破坏正确性** / **突破上限** / **体验降级** / **无影响**。

### 8.1 `TaskEngine` 实例属性（`engine.py:243-284`）

| 名称 | 行 | 用途 | 多进程后果 |
|---|---|---|---|
| `hubs: dict[int, TaskHub]` | 246 | 活动任务的实时文本/进度/订阅者 | 体验降级：非执行进程读到旧值、订阅挂起或被拒（第 4 节） |
| `_running: dict[int, asyncio.Task]` | 247 | 执行体登记；全局并发计数；「库里 running 时内存一定有可取消执行体」的不变量（第 695 行注释） | 突破上限（全局）；破坏正确性（取消/drain/作品排空把他人 running 当孤立，第 5 节） |
| `_running_statistics_keys` / `_running_execution_keys` / `_running_rounds` | 248、249、257 | 取消与撤权复查时核对 UUID/执行键/轮次 | 随 `_running`：非执行进程无法定位执行体 |
| `_running_projects` | 250 | `project_work()`：作品删除等待执行体停止 | 破坏正确性：其他进程的执行体不在等待集合内，删除排空可能在执行体仍运行时继续（`task_service.py:1666`、`recycle_bin_service.py:186-203,222`） |
| `_running_users` | 256 | 每用户并发计数；撤权通知定位执行体 | 突破上限；撤权通知不达其他进程（见下 `notify_collaboration_change`） |
| `_running_serial` | 261 | `serial_key` 占用 | **破坏正确性**（同键并行，第 2 节） |
| `_admitted: dict` | 258 | 领取提交后的准入计数（锁内二次额度检查、配额页已用数、drain 判断执行体是否仍在） | 突破上限；配额页显示偏低 |
| `_claiming: set` | 259 | 领取 CAS/提交确认期间不 `cancel()` 游标 | 无影响（只约束本进程的取消时机）；但 drain 用它判断「登记与退出窗口」（`cancellation.py:305`），非执行进程读不到 |
| `_cancel_requested` / `_shutdown` / `_finalizing` | 262-264 | 本进程执行体的取消/关停/收尾标记 | 无影响（纯本进程执行控制） |
| `_cleanup_work: dict[int, dict[asyncio.Task, int]]` | 251 | `cleanup_scope` 登记的请求侧清理协程；`project_work()`、drain、`recover_external` 读它 | 破坏正确性：其他进程在途的清理/外部调用不可见 |
| `_abort_failures` / `_failed_cleanups` / `_unproven_abort_failures` | 252-254 | 收尾失败的作品级屏障：阻止作品删除、供按原身份重试（`has_cleanup_failure` 第 398-401 行；`stop_project_tasks`） | 破坏正确性：屏障只在登记它的进程里；删除请求落到别的进程时看不到。另：`_unproven_abort_failures` 全文件没有移除语句（`grep` 只见 add 与读取），只随进程重启清空 |
| `_uncertain_executions` | 255 | 「结果待核对」展示覆盖 | 体验降级：其他进程不显示 |
| `_stalled: dict` | 270 | 领取在锁内确认无法前进的任务的退避表，避免热循环；`notify()` 与轮询超时清空 | 体验降级/额外负载：每个进程各退避各的，各自每 5 秒重试一次并各取一次写锁 |
| `_wake: asyncio.Event` | 265 | 调度唤醒 | 体验降级：跨进程最多延迟约 5 秒（第 3 节） |
| `_loop_task` / `_stopping` | 271-272 | 调度协程句柄与停止标志 | 无影响 |
| `runtime`（`AuthorizationRuntime`） | 260 | 见 8.3 | 见 8.3 |
| `admin_cancellations` | 273 | 见 8.2 | 见 8.2 |
| `_collaboration_subscribers: dict[(project, actor), set[Subscriber]]` | 274 | 成员变更时唤醒本进程订阅立即复验（第 2893-2903 行） | 体验降级：其他进程的订阅不被立即唤醒，但每帧发送前与每 15 秒心跳都会按库复验 |
| `_collaboration_revoked: dict` | 276 | 库里确认撤权后登记本进程执行体，取消后记 failed 而非 cancelled | 无影响（本进程执行体的收尾标记） |
| `_collaboration_checks: set[asyncio.Task]` | 277 | 在途的撤权复查协程 | 无影响 |
| `_paused_scan_at` / `_paused_scans` / `_paused_scan_lock` / `_paused_unproven_logged` | 279-282 | 待审核协作任务全量复查的节流、单飞锁、去重日志 | 无影响：各进程各扫各的，最终判定在数据库锁内条件 UPDATE（第 1055-1107 行），重复只是多做一遍 |
| `_legacy_unproven_logged` | 266 | 旧文档任务日志去重 | 无影响（日志重复） |
| `_done_candidates: dict` | 284 | 本执行体产出的单章 done 载荷，供收据收尾比对 `result_hash` | 无影响（只在执行进程使用；缺失时结果置空，第 2154-2155 行） |

`notify_collaboration_change`（第 2893-2917 行）只唤醒本进程的订阅、调度器、待审核复查，并只复查本进程 `_running_users` 里的执行体。其他进程的运行中协作任务收不到通知；`_check_collaboration_runs` 的文档串写明「未收到通知（跨实例、进程重启）时，执行体仍在下一次受控发送/写入检查点失败关闭」（第 2920-2923 行）。

### 8.2 `AdminCancellationDrain`（`cancellation.py:81-90`）

| 名称 | 行 | 用途 | 多进程后果 |
|---|---|---|---|
| `work: dict[(task_key, op_id), asyncio.Task]` | 84 | drain 单飞 | 破坏单飞：每个进程各起一个 drain 处理同一意图（第 5.2 节） |
| `recovering: dict` | 86 | 「启动遗留 running」标记，决定记 interrupted 还是 cancelled | 破坏正确性前提：其含义是「本进程启动时看到的 running」，多进程下可能是别的进程正在跑的任务 |
| `completed: dict` | 88 | 已按本轮完成的代次，防止旧失败标记重放 abort | 仅本进程可见；其他进程的旧失败快照看不到「已完成」 |
| `winners: dict` | 90 | 已产出 done/paused 但终态事务暂时失败的本进程已知赢家；注释「本进程知识；重启仍按既有 interrupted 规则」 | 仅执行进程持有；其他进程的 drain 不知道有赢家，会按孤立 running 处理 |

### 8.3 引擎外、但被领取/准入直接使用的进程内状态

| 名称 | 位置 | 用途 | 多进程后果 |
|---|---|---|---|
| `AuthorizationRuntime._lock`（`asyncio.Lock`）、`_owner`、`_depth` | `core/authorization_runtime.py:20-52` | 文件头「单应用共享、可重入的短发布临界段；数据库锁仍负责持久授权串行」。领取（`engine.py:1182`）与设置发布（`system_settings_service.py:94,148`）共用，保证「设置发布」与「领取读设置」在本进程不交错 | 跨进程不互斥；持久串行仍由数据库门 `authorization_state` 行锁保证（`authorization_service.py:105-127`） |
| `AuthorizationRuntime.settings_confirmed` | `core/authorization_runtime.py:26`；写入 `system_settings_service.py:43,67,198,207`；读取 `engine.py:655,1183` | 设置提交事实未知时暂停本进程新准入 | 各进程各一份；A 进程提交未知时 B 进程照常准入 |
| `ctx.settings`（进程内 `Settings` 对象）+ `SystemSettingsManager._revision/_overrides` | `system_settings_service.py:60-69` | `_publish` 把持久覆盖写进本进程 `Settings`；`task_max_concurrency`、`task_max_per_user`、`auto_analyze_enabled`、`mcp_enabled` 等运行时读它 | **突破上限 / 配置不一致**：`_reload()` 只在启动 `load()`、本进程自己的 `_update()` 前后、提交未知后的 `_recover()` 里调用（第 105、133、200、216 行），没有周期重载。A 进程改了设置，B 进程一直用旧值直到重启或自己也收到一次保存请求。规范明写「不提供多 worker 广播」（`system-settings.md:5`） |
| `SystemSettingsManager._lock`（`asyncio.Lock`）、`_writes`、`_recovery` | `system_settings_service.py:48-50` | 本进程内串行设置写入 | 跨进程靠 `revision` 条件 UPDATE（第 183-191 行）分出唯一赢家，输家 409 |
| `registry._HANDLERS` | `registry.py:90` | kind → handler，导入即注册 | 无影响（各进程相同） |
| `BatchHandler._jobs` | `handlers/batch.py:145` | `progress()` 读当前章下标/目标字数 | 无影响（只在执行进程调用；取不到时返回 1，第 306-309 行） |
| `ConsistencyHandler._jobs` | `handlers/consistency.py:47,93-100,147` | 同上 | 无影响 |
| `BookImportHandler._jobs` | `handlers/book_import.py:28,69-75,108` | 同上，键只是 `task_id` | 无影响 |
| `task._batch_job`（游离 Task 对象上的属性） | `handlers/batch.py:207,328`；`engine.py:2523` | 执行体持有的批量 job，`on_abort` / `complete_state` 取其内存断点 | 无影响（只在执行进程） |
| job 上的 guard 缓存 `publication_guards` / `execution_state.publication_guard` | `handlers/batch.py:42-131`、`handlers/agent.py:688-695` | 受控流帧来源须是同一 `SendGuard` 对象 | 随第 4.4 节：仅执行进程可证明 |
| `admission._RETRY_SOURCES` / `_ADMISSIONS` / `_CONSUMED` | `admission.py:38,91,92` | 受理证明，同一请求内签发并消费 | 无影响 |
| `project_ai_config._PREFLIGHTS` | `project_ai_config.py:132` | 模型预检证明，同一请求内 | 无影响 |
| `stream_evidence` 五个登记表 | `stream_evidence.py:128-130,161-162` | 受控流帧来源 | 体验降级：非执行进程的协作者运行中订阅被拒（第 4 节） |
| `stream_lease._LIVE_STREAM_LEASES` / `_SQLITE_SHARED_FRAME_LEASES` | `stream_lease.py:67,70` | 单帧发送租约证明 | 无影响（同一响应内） |
| `ai_dispatch._ACTIVE_SEND_LEASES` / `_CHAPTER_TRANSPORTS` / `_CHAPTER_GUARDS` / `_REQUEST_HOOK` | `ai_dispatch.py:75,95,105,448` | 出站发送租约与 provider 绑定 | 无影响（发送只在执行进程内） |
| `agent/service._locks`（`WeakValueDictionary[int, asyncio.Lock]`） | `agent/service.py:68,331-337` | 按会话串行化确认/拒绝/发送/撤销的 HTTP 请求 | PostgreSQL：还有 `AgentConversation`/`Task` 行锁；SQLite：注释说明这是弥补无 `FOR UPDATE` 的手段，跨进程失效（破坏正确性，仅 SQLite） |

不属于任务引擎、但同样是进程级缓存的（未深入，仅列出位置）：`retrieval_service._CACHE`（`retrieval_service.py:1715`，注释「正常由签名自动失效」）、`inspiration_service._generating`（`inspiration_service.py:95`）、`project_transfer/importer._CACHE`（`importer.py:1401`）、`mcp/client._test_connectors`（`mcp/client.py:83`）。它们在多进程下的后果**未核实**。

---

## 9. SQLite 与 PostgreSQL 的差异

检索范围：`services/task_engine/`、`task_service.py`、`ai_dispatch.py`、`chapter_claim.py`、`authorization_service.py`、`ai_config_service.py`、`collaboration_ai_guard.py`、`mcp/settings_gate.py`、`agent/service.py`、`core/database.py`。全后端**没有** `BEGIN IMMEDIATE` / `BEGIN EXCLUSIVE` / `SKIP LOCKED`（`grep` 无命中，除 `engine.py:6` 注释）。

| 位置 | 分支内容 | 是否靠单进程弥补 SQLite 无行锁 |
|---|---|---|
| `agent/service.py:331-337` `conversation_lock` | 进程内 `asyncio.Lock`，注释「SQLite 无 FOR UPDATE；单进程任务引擎再以会话锁串行化 HTTP 操作」 | **是**——唯一一处注释明说依赖单进程 |
| `handlers/agent.py:560-568` | 注释「SQLite 的 FOR UPDATE 不持有行锁；条件 no-op UPDATE 同时证明并锁定真实执行体」 | 否——用真实写锁（SQLite 文件级写锁）替代行锁 |
| `ai_dispatch.py:338-398` `_lock` | PG：`pg_try_advisory_xact_lock[_shared]` 轮询到 `_LOCK_WAIT_SECONDS`；SQLite：对 `Project` 行 no-op UPDATE 取写锁并临时调 `busy_timeout`；SQLite 读锁在帧租约内退化为普通 SELECT | 否——数据库锁。SQLite 没有共享行锁，所以读方也取写锁，同库所有受控发送/帧互斥 |
| `stream_lease.py:204-224` `_task_frame_lease` | 仅 SQLite 非 WAL：显式 `BEGIN` + 读 `sqlite_master`，真实持 SHARED 锁；WAL 模式回退写锁路径 | 否——SQLite 文件锁；但要求回滚日志模式（非 WAL） |
| `stream_lease.py:230,256` | `Project` / `Task` 行 `with_for_update(read=True)` | 否——PG 上是 `FOR SHARE`；SQLite 上该子句无效，靠上一行的 SHARED/写锁 |
| `authorization_service.py:105-127` `gate` | 两种方言都对 `authorization_state` 单行 UPDATE；PG 的 REPEATABLE READ 会话先 `LOCK TABLE ... SHARE ROW EXCLUSIVE` | 否——数据库锁；这是领取的全局串行点 |
| `authorization_service.py:130-138` `lock_users` | 两种方言都对 User 行 no-op UPDATE | 否 |
| `authorization_service.py:260-275` `require_authenticated_for_send` | SQLite：`lock_users`（或已持共享读租约则只读）；PG：`FOR SHARE` | 否 |
| `ai_config_service.py:40-62` `lock_config_owner` | PG：advisory 锁；SQLite：User no-op UPDATE（或共享读租约内只读） | 否 |
| `mcp/settings_gate.py:44-54` | PG：advisory 锁并要求 `read committed`；SQLite：注释「使用调用方已取得的物理写锁」 | 否 |
| `chapter_claim.py:141,149,417` | 只在 PG 加 `FOR UPDATE` / `FOR SHARE` | 否——SQLite 靠调用方先前的写语句已持写锁（未逐条核对每个调用点） |
| `collaboration_ai_guard.py:647` | 只在 PG 加 `FOR SHARE` | 同上 |
| `stream_evidence.py:252-253` | 只在 PG 加 `FOR SHARE`（`AgentChange`） | 同上 |
| `engine.py:977-983`、`task_service.py:113-116,142-148`、`agent/service.py:218-294` | JSON 函数方言（`json_type` vs `json_typeof` / JSONB） | 与锁无关 |
| `task_statistics_service.py:27-35` | upsert 方言；注释「PostgreSQL 用单例行串行初始化，SQLite 的 INSERT 已取得写事务」 | 否 |
| `core/database.py:13-52` | 仅 SQLite：外键 PRAGMA；`handle_error` 钩子在取消打断 aiosqlite 语句时先 finalize 游标并回滚，避免「僵尸连接」留锁 | 否——驱动行为补丁 |
| `engine.py:211-224` `_finish_before_cancel`、`engine.py:330` | 取消不打断在途数据库步骤；`stop()` 不 cancel「SQLite 活游标」 | 否——aiosqlite 取消语义 |

说明：

- 上表「否」的条目依赖的是 SQLite 的文件级锁。SQLite 文件锁本身对同一文件的多个进程有效（SQLite 机制），但本仓库的测试都是单进程、单引擎（`tests/conftest.py:101-124`），多进程 + SQLite 下的 busy 超时、`database is locked` 行为**未核实**。
- 生产 Docker 部署是 PostgreSQL 16（`docker-compose.yml:2-15`、`README.md:77`）；SQLite 是本地开发与测试用。`README.md:942` 写「自动化测试不再验证 PostgreSQL」，PG 用例需要环境变量 `AINOVEL_POSTGRES_TEST_URL`，否则跳过（如 `tests/test_collaboration_stream_agent_postgresql.py:35-45`）。

> 若要支持多进程，这里最少需要：PostgreSQL 路径的锁都是数据库级，不需为多进程改锁；SQLite 路径唯一明确依赖单进程的是 `conversation_lock`，其余需要实测 busy 行为后才能下结论。

---

## 10. 现有测试

测试夹具：`tests/conftest.py:101-124` 每个测试建一个 `create_app(..., start_task_engine=False)`，手动 `task_engine.start()` / `stop()`；数据库是每测试独立的 SQLite 文件（`tests/helpers.py:217-231`）。**没有任何测试起两个引擎共享一个库**（`tests/` 下检索 `TaskEngine(` 无命中，引擎只经 `create_app` 构造一个）。「重启」一律用同一个引擎对象 `stop()` 后再 `start()` 模拟；「崩溃」用手工 UPDATE 成 running 再 `start()` / `recover()` 模拟。

### 10.1 直接断言「库里 running 且本进程没有执行体 ⇒ 遗留」的用例（多进程改造后语义需重新定义，预计要改写）

| 文件 | 用例 |
|---|---|
| `tests/test_tasks.py` | `test_start_recovers_crashed_running_task`（345，手工写 running + `engine.hubs.clear()` 注释「模拟新进程」后 `start()` 断言 interrupted）、`test_start_does_not_restore_chapter_without_original_claim`（380） |
| `tests/test_task_statistics.py` | `test_startup_recovery_records_once`（187） |
| `tests/test_task_statistics_identity.py` | `test_recovery_snapshot_does_not_interrupt_replacement`（174） |
| `tests/test_task_abort_identity.py` | `test_recovery_never_replays_unproven_running_analysis`（169） |
| `tests/test_batch_generate.py` | `test_crash_recovery_restores_current_chapter`（586）、`test_crash_recovery_keeps_unproven_legacy_chapter_unchanged`（629） |
| `tests/test_batch_chapter_claim.py` | `test_recovery_of_committed_chapter_does_not_repeat_generation`（745）、`test_recovery_with_corrupt_receipt_keeps_running_barrier`（779）、`test_recovery_of_batch_review_keeps_pending_without_model_replay`（794）、`test_recovered_unstarted_batch_analysis_sends_once`（885） |
| `tests/test_rich_document_proposals.py` | `test_pending_review_is_recovered_from_running_checkpoint_without_model_replay`（141） |
| `tests/test_chapter_claim_author_wiring.py` | `test_lost_commit_confirmation_recovers_exact_completed_result`（260）、`test_unverifiable_committed_result_preserves_running_on_recovery`（302） |
| `tests/test_chapter_claim_wiring.py` | `test_recovery_retains_even_malformed_completion_receipt`（320） |
| `tests/test_collaboration_chapter_entry_e4f.py` | `test_receipt_completion_rejects_tampered_receipt`（624）、`test_recover_completes_valid_receipt_left_running`（658） |
| `tests/test_collaboration_analysis_entry_e4g.py` | `test_analysis_receipt_completion_rejects_tampered_receipt`（669）、`test_recover_completes_valid_analysis_receipt_left_running`（702） |
| `tests/test_collaboration_wizard_entry_e4g3c.py` | `test_tampered_receipt_keeps_running_barrier_and_recover_refuses`（925）、`test_recover_finalizes_committed_receipt_when_in_process_finalize_unavailable`（959）、`test_recover_releases_provable_uncommitted_pending`（989）、`test_recover_keeps_damaged_pending_unresolved`（1017） |
| `tests/test_wizard_uncertain_cleanup.py` | `test_recovery_keeps_orphan_wizard_running_and_blocks_retry`（27） |
| `tests/test_admin_task_cancellation.py` | `test_restart_recovers_persisted_intent_without_model`（199）、`test_running_abort_failure_retains_barrier_and_restart_interrupts`（402）、`test_unknown_handler_recovery_preserves_admin_intent`（672） |
| `tests/test_admin_uncertain_execution.py` | `test_admin_orphan_analysis_without_proof_remains_running`（30）、`test_admin_orphan_analysis_can_finish_only_verified_original_receipt`（55）、`test_admin_orphan_batch_unknown_does_not_abort`（74）、`test_admin_orphan_batch_generate_preserves_unknown_receipt_and_releases_claim`（108）——管理员 drain 接管孤立 running |
| `tests/test_agent_mcp_skills.py` | `test_executing_external_failed_on_engine_restart`（595）——`recover_external(stale_only=False)` |
| `tests/test_project_lifecycle.py` | `test_restart_clears_only_pending_intents_without_undeleting_trash`（193）——`recover_pending_deletions` |

这些用例验证的「收据可证则完成、不可证则保留 running、可回退则 interrupted」的判定逻辑本身与进程数无关；需要改的是「如何让恢复认定这条 running 确实无人执行」的前置布置。

### 10.2 断言 `stop()` → interrupted、重启后继续的用例（`stop()` 只动本进程任务，语义预计不变，夹具可能要调整）

`tests/test_tasks.py::test_stop_interrupts_running_and_keeps_queued`（306）、`::test_serial_key_after_restart`（840，直接断言 `engine._running_serial == {}`）、`::test_lifespan_starts_and_stops_engine`（721）；`tests/test_batch_generate.py::test_interrupt_then_retry_resumes_analysis_phase`（509）、`::test_interrupt_restores_current_chapter`（544）；`tests/test_agent_runtime.py::test_interrupted_by_restart_then_retry_continues_same_turn`（345）；`tests/test_book_import.py::test_interrupt_then_retry_does_not_repeat_done_batches`（694）；`tests/test_admin_task_cancellation.py::test_shutdown_waits_independent_drain_without_cancelling_cleanup`（434）。

### 10.3 断言并发上限 / 串行键 / 准入计数的用例（单引擎下仍成立；若计数改为查库，直接读内存映射的断言要改）

- `tests/test_tasks.py::test_per_user_limit_and_queue_position`（437）、`::test_global_limit`（464）、`::test_serial_key_same_key_serial_other_keys_parallel`（769）、`::test_serial_key_keeps_fifo_when_user_limit_blocks`（816）。
- `tests/test_analysis.py::test_analyze_pending_serial_in_order_and_dedup`（336）。
- `tests/test_rbac_concurrency.py::test_cancel_in_claim_gap_defers_signal_until_safe_finalize`、`::test_candidate_rechecks_lowered_quota_and_claim_holds_commit_return_gap`、`::test_database_terminal_state_does_not_release_post_success_execution_slot`、`::test_unknown_settings_commit_pauses_admission_until_confirmed_publish`——直接读写 `_admitted` / `admitted_count` / `settings_confirmed`。
- `tests/test_user_quotas.py::test_explicit_task_limit_replaces_default_and_inherit_uses_latest_default`、`::test_task_zero_queues_then_raise_runs_and_lower_does_not_kill`、`::test_unlimited_task_quota_still_respects_global_cap`。
- `tests/test_system_settings_runtime.py::test_hot_concurrency_lower_preserves_running_and_raise_releases_queue`（52）。

### 10.4 依赖本进程 hub / 通知 / 内存登记的用例（只要仍在单引擎夹具下运行就不受影响；跨进程路由或 hub 语义变化时需要新增而非改写）

- hub 与订阅：`tests/test_tasks.py::test_hub_snapshot_then_delta_no_gap`（54）、`::test_subscribe_mid_stream_two_subscribers`（68）、`::test_subscribe_finished_task_ignores_stale_hub`（107）、`::test_subscribe_task_finishes_before_snapshot`（139）、`::test_create_does_not_recreate_finished_hub`（173）、`::test_stream_heartbeat`（221）；`tests/test_task_stream_snapshot.py`、`tests/test_task_stream_evidence.py`、`tests/test_task_sse_*.py`、`tests/test_collaboration_stream_*.py`（文件级，未逐条列）。
- 运行中取消：`tests/test_tasks.py::test_cancel_running_task`（239）、`tests/test_task_statistics_identity.py::test_old_cancel_running_cannot_cancel_replacement_execution`（210）、`tests/test_collaboration_task_review.py::test_running_cancel_binds_task_uuid_and_queued_round`（316）、`tests/test_admin_chapter_claim_cancellation.py::test_admin_waits_for_original_running_executor_to_stop`（295）。
- 撤权通知唤醒本进程执行体：`tests/test_collaboration_task_revocation_e4.py::test_notify_wakes_running_executor_after_database_recheck`（404）、`::test_notify_after_direct_owner_ai_close_fails_running_with_model_message`（428）、`::test_notify_never_stops_running_author_task`（446）。
- 外部调用恢复读 `cleanup_scope`：`tests/test_agent_recovery_claim_identity.py::test_recovery_sees_the_current_cleanup_scope_and_recovers_after_exit`（167）。
- 会话锁：`tests/test_recycle_bin_tasks.py::test_waiting_for_conversation_lock_cannot_recover_external_after_trash`（415）。
- 作品排空读 `project_work`：`tests/test_recycle_bin_tasks.py::test_running_task_stops_before_trash_and_restore_does_not_restart`（41）、`::test_concurrent_queued_cancel_cleanup_is_waited_and_new_writes_are_blocked`（90）。

访问引擎私有内存状态最多的测试文件（按 `grep` 命中数）：`test_admin_task_cancellation.py`（12）、`test_kb_legacy_document_cleanup.py`（10）、`test_chapter_claim_wiring.py`（10）、`test_entity_ai_execution_identity.py`（8）、`test_admin_task_check_regressions.py`（8）、`test_admin_chapter_claim_cancellation.py`（8）、`test_rbac_concurrency.py`（6）、`test_collaboration_chapter_execution.py`（6）、`test_agent_auto_external_fencing.py`（6）。调用 `engine.start()/stop()` 的测试文件共约 100 个（`test_tasks.py` 15 处、`test_batch_chapter_claim.py` 14、`test_collaboration_task_revocation_e4.py` 14、`test_collaboration_outline_ai.py` 13、`test_task_statistics.py` 12、`test_task_receipt_deletion_guard.py` 12 等）。本节的用例清单来自名称与关键字检索，**不保证穷尽**；未逐个打开每个用例核对其断言。

---

## 改造影响面汇总表

「阻塞最小目标」指是否阻塞「不重复调度、不误标中断」。

| # | 位置 | 当前行为 | 多进程后果 | 阻塞最小目标 |
|---|---|---|---|---|
| 1 | `engine.py:436-577` `recover()` | 启动时把全库 running（除本进程登记的）全部收尾：`on_abort` + interrupted，或按收据完成 | **破坏正确性**：其他进程正在跑的任务被回退资源并标 interrupted | **是**（核心） |
| 2 | `engine.py:578-583` + `agent/service.py:147-328` `recover_external(stale_only=False)` | 启动时把全库 executing 的外部调用判为失败 | **破坏正确性**：其他进程在途的 MCP 确认被标「结果未知」 | **是**（同属启动误伤） |
| 3 | `engine.py:444-447`、`cancellation.py:86` `recovering` | 把启动时看到的 running 记为「启动遗留」，drain 据此记 interrupted | 破坏正确性前提 | **是**（随 #1） |
| 4 | `recycle_bin_service.py:507-515` `recover_pending_deletions` | 启动时清空所有在途删除标记 | 破坏正确性：其他进程的删除排空标记被清 | 否（不属调度/中断，但同为启动假设） |
| 5 | `engine.py:1285-1297` 领取条件 UPDATE + `authz.gate` + 行锁 | `rowcount==1` 才算领到 | **无影响**：已原子，不会重复领取（PG 已读代码确认；SQLite 多进程 busy 行为未核实） | 否（已满足） |
| 6 | `engine.py:660-667` 扫描 queued | 无 `LIMIT`、无行锁，全量读 | 无正确性影响；每个进程都把同一批当候选，多做无效领取尝试 | 否 |
| 7 | `engine.py:671,682,702` `_running_serial` | `serial_key` 占用只看内存 | **破坏正确性**：`analysis:{project}`、`index:{project}` 同键可并行，回写顺序不再保证 | **是**（属「调度约束被绕过」；是否算「重复调度」取决于 PRD 口径） |
| 8 | `engine.py:678,1262` `len(_running)` / `admitted_count()` | 全局并发数内存 | **突破上限**：≈ N × `TASK_MAX_CONCURRENCY` | 否（不重复、不误标；是否纳入由 PRD 定） |
| 9 | `engine.py:669,690,1263` `_running_users` / `_admitted` | 每用户并发数内存 | **突破上限**：≈ N × 限额（限额 0 仍有效） | 否 |
| 10 | `system_settings_service.py:60-69,93-109` + `ctx.settings` | 设置只在本进程发布，无周期重载 | 突破上限 / 配置不一致：其他进程用旧的并发上限、自动分析开关、MCP 开关 | 否 |
| 11 | `core/authorization_runtime.py:26` `settings_confirmed` | 设置提交未知时暂停本进程准入 | 其他进程不暂停 | 否 |
| 12 | `engine.py:337-341,633-652` `notify()` / `_wake` + 5 秒轮询 | 进程内事件唤醒；轮询兜底 | **体验降级**：跨进程领取延迟最多约 5 秒 | 否 |
| 13 | `task_service.py:1300-1326` 本人取消运行中任务 | 本进程没有执行体即视为孤立：`on_abort` + 改 cancelled | **破坏正确性**：未 `cancel()` 真正的执行体就回退资源并改终态；执行体到下一检查点才失败关闭 | 否（是「误标取消」而非误标中断；但与 #1 同源：「不在本进程 ⇒ 孤立」） |
| 14 | `task_service.py:1132-1149` 分析/向导运行中取消 | 本进程没有执行体 → 409 | 体验降级：请求落到非执行进程时无法取消 | 否 |
| 15 | `engine.py:2836-2857` `cancel_running` | `asyncio.Task.cancel()` + 进程内等待 | 只对本进程执行体有效 | 否 |
| 16 | `cancellation.py:92-133,269-456` 管理员 drain | 进程内单飞；用内存判断执行体是否已停 | **破坏正确性**：各进程并发 drain 同一意图；非执行进程按孤立 running 接管 | 否（仅发生在已有取消意图的任务上） |
| 17 | `cancellation.py:88,90` `completed` / `winners` | 本进程已知的完成代次与赢家 | 破坏正确性：其他进程不知道有赢家，可能把已产出结果的任务按 abort 处理 | 否 |
| 18 | `engine.py:251-254,398-401` 收尾失败屏障 | 内存集合阻止作品删除 | 破坏正确性：屏障只在登记进程可见 | 否 |
| 19 | `engine.py:2878-2882` `project_work` + `task_service.py:1634-1702`、`recycle_bin_service.py:131,180-222` | 作品删除等本进程执行体与清理协程停止 | 破坏正确性：其他进程的执行体不在等待集合内 | 否 |
| 20 | `agent/service.py:207-212` `recover_external(stale_only=True)` | 用本进程 `project_work`/`_cleanup_work` 判断外部调用是否仍在途；另有 2 分钟过期阈值 | 其他进程在途调用只受 2 分钟阈值保护 | 否 |
| 21 | `agent/service.py:68,331-337` `conversation_lock` | 进程内按会话加锁 | PG：有行锁兜底；SQLite：**破坏正确性**（注释明说依赖单进程） | 否 |
| 22 | `engine.py:246` `hubs` + `hub.py` | 实时文本/进度/订阅者在内存 | **体验降级**：非执行进程的作者订阅在快照后只有心跳、收不到终止帧；残留 hub 提供旧值 | 否 |
| 23 | `task_service.py:909-916` `/output`、`task_service.py:324-332` `TaskOut`、`admin_task_service.py:101-105` | 活动任务读本进程 hub | 体验降级：无 hub 时读库（滞后 ≤2 秒）；有残留 hub 时读到建立时的旧值 | 否 |
| 24 | `stream_evidence.py:128-130,161-162` + `engine.py:3183-3185,3372-3392` | 受控流帧来源是进程内对象 | 体验降级（失败关闭）：协作者在非执行进程订阅运行中任务得到 `permission_denied` | 否 |
| 25 | `engine.py:2893-2917` `notify_collaboration_change` | 只唤醒本进程订阅与执行体 | 体验降级：其他进程的执行体到下一检查点才停；订阅每帧/每 15 秒按库复验 | 否 |
| 26 | `quota_service.py:83` | 配额页已用并发 = 本进程 `admitted_count` | 体验降级：显示偏低 | 否 |
| 27 | `engine.py:255` `_uncertain_executions` | 「结果待核对」展示 | 体验降级：其他进程不显示 | 否 |
| 28 | `engine.py:270` `_stalled` | 无法前进任务的本进程退避 | 体验降级：各进程各自每 5 秒重试 | 否 |
| 29 | `engine.py:1021-1053,1109-1133` 待审核协作任务复查 | 各进程每 15 秒全量复查，锁内条件 UPDATE 收尾 | **无影响**（重复工作） | 否 |
| 30 | `engine.py:304-335` `stop()` | 只中断本进程执行体 | **无影响**；滚动重启时该进程任务按现有语义记 interrupted | 否 |
| 31 | `entities.py:804-810` `uq_tasks_active_resource`、章节/文档占用列 | 数据库级资源互斥 | **无影响** | 否（已满足） |
| 32 | `task_service.py:699-761` 继续、`666-696` 重试、`764-819` 提前结束 | 条件 UPDATE / 插入 + notify | **无影响**（只差唤醒，见 #12） | 否 |
| 33 | `admission.py:38,91,92`、`project_ai_config.py:132`、`stream_lease.py:67,70`、`ai_dispatch.py:95` | 同一请求/同一发送内的进程内证明 | **无影响** | 否 |
| 34 | `handlers/batch.py:145`、`consistency.py:47`、`book_import.py:28` `_jobs` | 执行进程内的进度辅助 | **无影响** | 否 |
| 35 | `entities.py:813-857` `tasks` 列 | 无 heartbeat / owner / lease 列；`execution_key` 是领取代次且被多种收尾标记复用 | 无法区分「别的进程正在跑」与「执行者已死」 | **是**（#1 的前提缺口） |
| 36 | `Dockerfile:37` | 每个容器启动先 `alembic upgrade head` | 多实例同时启动时并发迁移（行为未核实） | 否（属部署演练半项） |

---

## Caveats / Not Found

- **未运行任何多进程实验。** 第 4.3、5.1、5.2 节关于「请求落到非执行进程」的结论是按代码路径推演得出的，已在各处标注。
- **收据类恢复与活执行体并发的最终状态未核实**（第 1.3 节）：条件 UPDATE 能保证只有一方命中，但另一方随后走哪条回退分支、资源是否一致，需要专门推演或测试。
- **`on_abort` 与另一进程活执行体并发的资源后果未核实**（第 5.1 节）：只确认了执行体会在下一次事件核验/刷库时失败关闭，未逐个 handler 核对回退与在途保存事务的交错。
- **SQLite 多进程行为未核实**（第 2.3、9 节）：SQLite 文件锁跨进程有效是其自身机制，但本仓库没有多进程 SQLite 测试，busy 超时与 `database is locked` 的表现未知。
- **lifespan 中其他后台组件未读实现**（第 1.5 节）：`RecycleBinCleaner`、`DashboardSampler`、`BackupRuntime`、`usage_maintenance`、`sync_presets`、`purge_expired_all`、`bootstrap_admin` 在多进程下是否安全未核实。
- **引擎外的进程级缓存未深入**（第 8.3 节末）：`retrieval_service._CACHE` 等只列了位置。
- **`chapter_claim.py` / `collaboration_ai_guard.py` 的 PG-only `FOR UPDATE` 分支**：未逐个调用点确认 SQLite 下是否都已先持写锁。
- **测试清单不保证穷尽**（第 10 节）：来自名称与关键字检索，未逐个打开用例核对断言。
- **未找到**：任何跨进程通知机制（LISTEN/NOTIFY、Redis、消息队列）、任何心跳/租约/owner 列或代码、任何双引擎共享数据库的测试。
- 本文件按任务要求只描述现状；每节末尾的「若要支持多进程，这里最少需要」是对缺口的一句话标注，不是设计方案。
