# 设计：统一持久化后台任务

## 1. 总体思路

- **数据库就是队列**：`tasks` 表是唯一事实来源。服务进程内只有一个调度协程（`TaskEngine`），它从表里领取排队任务，用 `asyncio.Task` 执行。前提是单进程部署（`Dockerfile:26`）；以后要支持多实例，再把领取改成 `FOR UPDATE SKIP LOCKED`，接口不变。
- **复用现有的 runner**：`run_chapter_job` 和 `run_wizard_job` 已经是 `(event, data)` 异步生成器，引擎直接消费它们产出的事件（delta、continue、done、error），转成任务状态和广播。业务逻辑（写正文、版本、生成记录、自动续写、向导解析覆盖）基本不动。
- **prepare 拆成两段**：
  - `validate_and_claim`：在创建任务的请求里同步执行，负责鉴权、AI 配置解析、SSRF 校验、参数校验；章节类任务还要原子抢占 `generating`。输出可序列化的 `input`。
  - `build_job`：任务开始运行时执行，从 `input` 重新组装 provider、上下文和 messages。上下文按运行时的最新数据构建。
- **实时输出**：引擎在内存里为每个排队或运行中的任务维护一个 `TaskHub`，包括已输出的文本缓冲和订阅者队列。已输出的文本每 2 秒刷入数据库 `output_text` 一次，结束时再刷一次，供重启后查看和已结束任务的回看。

## 2. 数据模型（迁移 `0006_tasks`）

> 开工时先检查 `backend/alembic/versions`。封面任务如果已经占了 `0006`，本任务顺延到下一个编号。

### `tasks`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| user_id | FK users CASCADE，index | |
| project_id | FK projects CASCADE，可空，index | |
| chapter_id | FK chapters SET NULL，可空，index | |
| kind | String(32) | `chapter.generate`、`chapter.polish`、`wizard.world`、`wizard.characters`、`wizard.outline` |
| resource_key | String(100)，可空 | `chapter:{id}` 或 `wizard:{project_id}:{kind}`，用于 D3 互斥 |
| status | String(16) | `queued`、`running`、`succeeded`、`failed`、`cancelled`、`interrupted` |
| input | JSON | 可序列化的任务输入，包括 `ai_config_id`、请求参数和章节的 `prev_status` |
| result | JSON，可空 | 章节任务为 `{chapter, warning?}`，向导任务为 `{kind, items}` |
| output_text | Text，默认 "" | 已输出文本的快照 |
| progress | int 0–100 | |
| stage | String(100)，默认 "" | 阶段文案，例如"排队中""生成中""字数不足，自动续写第 1 轮""保存中" |
| error | Text，默认 "" | 中文错误 |
| step_cursor | int，默认 0 | 多步任务的断点，目前的单步类型不用，留给 outline-pro 等 |
| retry_of_id | FK tasks SET NULL，可空 | |
| created_at / started_at / finished_at | | `started_at` 和 `finished_at` 可空 |

索引设计：

- `(status, id)`：调度器扫描用。
- `(user_id, id)`：列表查询用。
- **部分唯一索引** `uq_tasks_active_resource`，建在 `resource_key` 上，条件为 `WHERE status IN ('queued','running') AND resource_key IS NOT NULL`。PostgreSQL 和 SQLite 都支持部分索引，SQLAlchemy 里同时写 `postgresql_where` 和 `sqlite_where`。D3 的资源互斥靠它保证原子性：插入撞到唯一约束时返回 409「已有任务在进行中」。

### `generation_logs` 增加 `task_id`

`task_id` 为 FK tasks，SET NULL，可空，用来关联生成记录和任务（R7）。

## 3. 任务生命周期

```
create ──> queued ──claim──> running ──> succeeded
             │                  ├──> failed        (runner 产出 error 事件或抛异常)
             │                  ├──> cancelled     (用户取消)
             │                  └──> interrupted   (引擎停止或进程崩溃后重启时恢复)
             └──cancel──> cancelled
failed / cancelled / interrupted ──retry──> 新任务(queued, retry_of_id=原任务)
```

- **创建**：路由调用 `validate_and_claim`，在同一事务里插入 `queued` 任务（如果撞到部分唯一索引，回滚并返回 409），然后 `engine.notify()` 唤醒调度器，返回 202 和 `TaskOut`。
  - 章节类任务在创建时就把章节置为 `generating`（与现在的原子抢占一致），排队期间编辑器即只读。
  - 抢占失败（章节已在生成中）返回 409，与现有行为一致。
- **领取**：调度器按 `id` 升序扫描 `queued` 任务，跳过所属用户运行中任务已达 2 个的；全局运行中任务达到 `TASK_MAX_CONCURRENCY` 时停止扫描。领取用条件更新 `UPDATE ... SET status='running', started_at=now WHERE id=? AND status='queued'`，影响行数为 1 才算领到。领到后执行 `asyncio.create_task(self._run(task_id))`。
- **执行**：`_run` 先执行 `build_job`；如果配置被删、章节被删等，任务失败。然后用 `aclosing` 消费 runner 的事件：
  - `delta`：追加到 hub 的缓冲区，广播给订阅者；节流写库，每 2 秒刷一次 `output_text`。
  - `continue`：广播，同时更新 `stage`。
  - `done`：`result` 写入 payload，状态置为 `succeeded`，`progress` 置为 100。
  - `error`：状态置为 `failed`，写入 `error`。
  - 结束时刷一次 `output_text` 和 `finished_at`，广播终止事件，然后释放 hub。
- **进度**：章节任务按 `已输出字数 / 目标字数` 估算，封顶 95%；向导任务按阶段分为排队 0、生成中 10–90、保存 95。
- **取消**：`POST /tasks/{id}/cancel`。
  - `queued`：条件更新为 `cancelled`，章节类任务把章节状态还原为 `input.prev_status`（只在 `status='generating'` 时还原）。
  - `running`：对 `asyncio.Task` 调用 `cancel()`。`CancelledError` 会传进 runner 的 `finally`，它在 shield 里回退章节状态，并把生成记录写成 cancelled，这部分已有实现。引擎捕获取消后，把任务状态置为 `cancelled`。
  - 已经结束的任务返回 409。
- **停止与重启**（D2）：
  - `engine.stop()` 时，把运行中的任务标记为"关停中断"再 cancel。`_run` 根据这个标记，把状态写为 `interrupted` 而不是 `cancelled`。
  - `engine.start()` 时先做恢复：把所有 `running` 任务置为 `interrupted`，`error` 写「服务重启，任务已中断」；章节类任务把仍处于 `generating` 的章节还原为 `prev_status`。`queued` 任务保持不变，正常调度。
  - 进程崩溃时 `stop` 没有机会执行，由下次 `start` 的恢复逻辑兜底。
- **重试**：`POST /tasks/{id}/retry`。只允许对 `failed`、`cancelled`、`interrupted` 状态的任务重试。用原任务的 `input` 重新执行 `validate_and_claim`，期间会重新校验 AI 配置、重新抢占章节；然后插入一个新任务，`retry_of_id` 指向原任务，`step_cursor` 继承原任务的值。

## 4. 实时订阅（`GET /api/v1/tasks/{id}/stream`）

SSE，GET 请求，同源 cookie 鉴权。帧格式和响应头沿用 `app/api/sse.py`。

| 事件 | payload | 时机 |
|---|---|---|
| `snapshot` | `{task: TaskOut, text: <已输出全文>}` | 订阅后第一帧 |
| `delta` | `{text}` | 新增输出 |
| `continue` | `{round, words, target}` | 自动续写，原样转发 |
| `progress` | `{progress, stage, queue_position?}` | 状态或进度变化时，以及排队位置变化时 |
| `done` | `{task: TaskOut, result}` | 成功；之后关闭流 |
| `error` | `{task: TaskOut, detail}` | 失败、取消或中断；之后关闭流 |

- **不重复也不遗漏**：hub 用 `subscribe()` 在同一个同步临界区里复制当前缓冲区，并注册订阅队列，中间没有 `await`。所以快照之后的增量恰好从快照末尾开始。
- **已经结束的任务**：快照取自数据库的 `output_text`，紧接着发出 `done` 或 `error`，然后关闭。
- **排队中的任务**：hub 在创建任务时就已建立，订阅者可以提前挂上。
- **心跳**：每 15 秒发一次注释帧 `: ping`，防止代理断开空闲连接。
- **客户端断开**只会移除这个订阅者，不影响任务本身执行，D1 的关闭页面不中断就靠这一点。
- 订阅者的队列有上限，比如 1000 帧；溢出时断开该订阅者，前端会自动重连，用快照补齐。

## 5. API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chapters/{cid}/generate` | **返回值改为** 202 `TaskOut`，不再返回 SSE；请求体不变 |
| POST | `/chapters/{cid}/polish` | 同上 |
| POST | `/projects/{pid}/wizard/{kind}` | 同上 |
| GET | `/tasks?project_id=&chapter_id=&kind=&active=&limit=` | 当前用户的任务列表，按 id 倒序。`active=true` 只返回排队和运行中的任务。排队中的任务附带 `queue_position` |
| GET | `/tasks/{id}` | `TaskOut`，不含 `output_text` |
| GET | `/tasks/{id}/output` | `{text}`，查看已结束或中断任务的输出 |
| GET | `/tasks/{id}/stream` | SSE，见 §4 |
| POST | `/tasks/{id}/cancel` | 返回 `TaskOut`；已结束的任务返回 409 |
| POST | `/tasks/{id}/retry` | 202，返回新任务的 `TaskOut`；状态不允许重试时返回 409 |
| DELETE | `/tasks/{id}` | 只能删除已结束的任务，否则返回 409 |
| DELETE | `/tasks?finished=true[&project_id=]` | 清理已结束的任务 |

`TaskOut` 包含这些字段：`id, kind, status, project_id, chapter_id, progress, stage, error, retry_of_id, queue_position, created_at, started_at, finished_at`，另带 `title`，即用于展示的中文标题，例如"第 3 章 · AI 生成""世界观向导"。

局部改写预览 `POST /chapters/{cid}/rewrite` 保持同步 SSE，不进任务引擎（D1）。

## 6. 引擎结构（`app/services/task_engine/`）

```
task_engine/
  engine.py      TaskEngine：start/stop/notify/cancel、调度循环、_run、恢复
  hub.py         TaskHub：缓冲区、订阅者、节流刷库
  registry.py    kind → TaskHandler(validate_and_claim, build_job, run, on_abort)
  handlers/
    chapter.py   chapter.generate / chapter.polish（包装 generation_service）
    wizard.py    wizard.*（包装 wizard_service）
app/services/task_service.py   列表、详情、取消、重试、清理、保留裁剪（API 入口）
app/api/v1/tasks.py            路由
```

- **`TaskHandler` 协议**：后续子任务注册新 kind 只需实现这个协议。`on_abort` 负责任务中止时清理资源，例如还原章节状态，供取消和恢复路径调用。
- `TaskEngine` 挂在 `app.state.task_engine`，在 `lifespan` 中启动和停止。`create_app(..., start_task_engine=True)` 由测试 fixture 显式调用 `start` 和 `stop`。
- **保留裁剪**：任务结束时顺带检查，每个用户超过 200 个已结束任务时删除最旧的（R11）。
- **配置**：在 `Settings` 增加 `task_max_concurrency: int = 4`（≥1）和 `task_max_per_user: int = 2`（≥1），并补充到 `.env.example`。

## 7. 前端

- **`api/tasks.ts`**：任务的增删改查，以及 `subscribeTask(id, handlers, signal)`。订阅基于 fetch 的 GET 加现有 SSE 解析器，复用 `sse.ts` 的帧解析；断线后带退避自动重连，重连时用 `snapshot` 覆盖本地文本。
- **`hooks/useTaskStream(taskId)`**：返回 `{text, task, running, error, continueInfo}`；组件卸载时只断开订阅，不取消任务。
- **`stores/tasks.ts`**（Zustand）：维护活动任务列表。有活动任务时每 3 秒轮询 `GET /tasks?active=true`，没有时改为每 15 秒；本地创建任务后立即刷新。登出时清空。
- **章节编辑器**：
  - 生成和润色改为调用接口创建任务、拿到任务后用 `useTaskStream` 订阅。原来基于 `useStreamTask` 的 AI 生成流程改造完成，局部改写仍用 `useStreamTask`。
  - 挂载时查询 `GET /tasks?chapter_id=X&active=true`，有活动任务就自动订阅，正文只读。
  - "停止"按钮改为调用取消任务的接口。
  - 排队时显示"排队中，前面还有 N 个"。
  - 完成后执行 `applyServerChapter(result.chapter)`，有 `warning` 时提示。
  - 离开守卫：AI 任务运行中可以离开，任务会继续；只有存在未保存修改或局部改写待采纳时才拦截。这条行为变化要同步更新到 spec。
- **向导页**（`WizardGenerator`）：同样改为"创建任务加订阅"；挂载时按 `project_id + kind` 查询活动任务并自动接上。
- **`components/TaskPanel.tsx`**：
  - 位置在 `AppLayout` 右下角，是一个 `FloatButton` 加 `Badge`，显示活动任务数。
  - 点开是一个 Drawer，分"进行中"和"最近"两个 Tab。每行显示标题、状态、进度条、时间。
  - 每行的操作有：取消、重试、查看输出（Modal，进行中的任务用 `useTaskStream` 实时显示）、跳转、删除；顶部有"清理已结束"。

## 8. 兼容与风险

- **接口语义变化**：`generate`、`polish`、`wizard` 三个端点从 SSE 改成 202 JSON，前后端同一版本发布，不保留旧行为。原有测试改为"创建任务 → 等待完成 → 断言"，测试 helper 提供 `wait_task(client, task_id)`。
- **测试**：
  - `FakeLLM` 需要支持可控阻塞，比如推入一个 `asyncio.Event`，用来测试取消、并发上限和排队。
  - 引擎在测试中真实运行。
- **部分唯一索引**在 SQLite 和 PostgreSQL 上都需要有迁移测试覆盖。
- **恢复逻辑**在每次 `start` 时运行。只要部署是单进程，就不会误伤其他进程的运行中任务。这一前提写进 spec。
- **回滚**：迁移只新增表和一个可空列。代码回滚加 `alembic downgrade -1` 即可。

## 9. 取舍

- **不用 Redis 和 Celery**：单进程部署下引入外部队列收益低、运维成本高。数据库队列加内存 hub 已经能满足 D1、D2、D3。
- **订阅用 GET SSE，不用 WebSocket**：复用现有 SSE 栈和代理配置，并且单向推送已经足够。
- **任务创建时就抢占章节**：排队期间禁止编辑，与"生成中只读"的心智一致，也避免排队期间的编辑在生成完成后被覆盖。
