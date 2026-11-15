# Research: E5 任务面现状盘点与切片方案

- **Query**: E5（implement.md:62）任务列表/详情/输出/取消/重试/继续/审核（含富文档提案）与 SSE 对「本人 + 当前作者或 accepted 协作身份」开放；收拢 E4 各片留给 E5 的事项（收据收尾帧、协作者 SSE 404、SSE strict xfail）；给出可独立实现、复核的切片。
- **Scope**: internal（只读代码、测试、规范与任务记录；未改代码、未运行测试）
- **Date**: 2026-11-12
- **验收依据**: design.md §6.3 第二段；§5.3 第 3~5 条（撤权通知、HTTP 流头后不能改 404、已发送不撤回）；§8 前端；§9 第 4、8 条。

---

## 0. 结论速览

1. **HTTP 读/写面大部分已就位**。`task_service` 的列表、详情、输出、取消、重试、继续、提前结束、删除、清理都已经用 `task_visible_to_actor`（SQL 谓词）+ `_lock_user_task_project`（锁后重验）实现「本人 + 作者/accepted 成员 + 活动任务原轮次 editor」，管理员人工权限不放行。富文档提案读取/采纳也已按原 grant 处理。E5 在这一层主要是**补测试矩阵和少数缺口**（`/finish` 无协作者用例、paused 任务降级后的收尾），不是从零实现。
2. **真正未完成的是 SSE**。
   - 生产路由 `GET /tasks/{id}/stream` 仍用**非受控**的 `sse_response(engine.subscribe(...guarded=False))`，并对 `binding.member_grant is not None`（即协作者）直接 404（`app/api/v1/tasks.py:61-80`）。
   - 受控帧管线（`engine.subscribe(guarded=True)` + `stream_lease.task_sse_response` + `stream_evidence.verify_stream_frame`）**已经存在，但只在测试里挂载**（`tests/test_task_sse_engine_http.py` 等通过 `app.add_api_route("/_test/...")`）。
   - 该管线目前只能证明两类帧：一是**大纲/实体**（有 `stream_guard`）的运行中帧和终态帧；二是**排队快照**。下面四类会被判失败，订阅者收到 `permission_denied`：
     - 章节、分析、批量、向导、知识库、拆书、Agent 的运行帧。原因：`hub.source` 是裸的 `TaskExecutionIdentity`，不是 `PublicationSource`；而且 `SendGuard.require_task_read` 明确拒绝章节 claim、文档 claim 和向量输入。
     - **已结束任务的回放**：`verify_stream_frame` 对非 queued 状态的 `TaskViewEvidence` 只放行「作者删除作品时的取消」。
     - 无作品的个人任务回放。
     - 收据收尾帧。
3. **收据收尾帧**：章节、分析、批量（断点）、向导四条路径都以 `status="receipt_completed"` 提前 `return`。这样会跳过终态证明签发（`hub.terminal_source`），并在 `finally` 中以 `{"task": None, "detail": *_RECEIPT_FINALIZED_DETAIL}` 关闭 hub（engine.py:1483-1511、1570-1672、1740-1748）。结果是：
   - 非受控路径（作者）收到不带快照的 error，前端 `subscribeTask` 将其判为 `failed`；
   - 受控路径中该帧必定校验失败，订阅者收到 `permission_denied`。
4. **前端**仍按 `canUseAI = role==='owner' && can_use_ai`（`frontend/src/utils/projectAccess.ts:46`）把编辑者的全部 AI 和任务入口关闭；`AppLayout` 对没有 `canUseAI` 的作品不挂载 TaskPanel。`TaskOutputModal` 读取**已结束**任务输出也要求 `canUseAI`，这与「viewer 可读本人已结束记录」不一致。

---

## 1. 入口逐一盘点（后端）

记号：**O** = 作品作者，**E** = accepted editor（原轮次），**V** = accepted viewer，**R** = 已撤销、pending 或非成员，**A** = 仅管理员人工权限，**A+M** = 管理员兼协作者（按成员身份处理）。

公共谓词为 `task_visible_to_actor(db, actor_id, active_grant_required=False, token_version=None)`（`backend/app/services/collaboration_task_access.py:107-140`），它同时要求：

- `Task.user_id == actor`，且 `statistics_key` 非空；
- 用户启用，且 token_version 与请求认证快照一致；
- 作品活跃，且满足以下之一：
  - actor 是作者；
  - accepted 成员，且任务已结束（任何角色）；
  - 任务处于 OPEN 状态（queued/running/paused），成员为 editor，并且 `_collaboration_grant` 与成员的 `invitation_id`/`revision` 完全一致（SQLite/PG 分别做规范化校验，未知方言用 `false()`）。

当 `active_grant_required=True` 时，**不论任务状态**都必须是 editor 且轮次一致。

| 入口 | 路由 / 服务（file:line） | 现在的鉴权 | 协作者实际得到 | 目标（design §6.3） | 缺口 |
|---|---|---|---|---|---|
| 列表 `GET /tasks` | `api/v1/tasks.py:24-37` → `task_service.list_tasks` `task_service.py:899-977` | LIMIT 之前用 SQL 谓词过滤；投影后对活动行要求原轮次，对结束行要求可见，最后复验；空结果再区分 401 | E 能看到本人全部任务；V 只能看到本人已结束的任务；R/A 为空 | 一致 | 无功能缺口；缺少「A+M、降级、paused」组合的完整矩阵用例 |
| 详情 `GET /tasks/{id}` | `tasks.py:49-51` → `get_task:865` → `get_owned_task:795` + `task_out:342` → `project_task_out:374` / `_require_visible_task_result:354` | 同上，投影后按原 grant 和 queued_at 复验 | 同上；不可见时 404，token 失效时 401 | 一致 | 同上 |
| 输出 `GET /tasks/{id}/output` | `tasks.py:54-56` → `get_output:871-896` | 同上；运行中读 hub.text，否则读 `output_text`，经 `project_payload` 投影后复验 | 同上 | 一致 | 同上 |
| 订阅 `GET /tasks/{id}/stream` | `tasks.py:59-80`；`stream_lease._capture_stream_binding:115-148`；`engine.subscribe:2602` | 作者走**非受控**管线，每帧用独立会话执行 `_visible_task` + `project_payload` 后复验（`checked_frame`）；**协作者只要 member_grant 非空，一律 404**（`tasks.py:70-72`） | E/V：**404**（包括已结束任务）。A：404。A+M：404 | E 订阅本人活动任务需要原轮次；V 可读本人已结束任务；撤权或降级后关闭；响应头发出后以 `permission_denied` 结束 | **E5 主缺口**，见 §2 |
| 取消 `POST /tasks/{id}/cancel` | `tasks.py:83-90` → `cancel_task:980-1004` | `get_owned_task`，再 `_lock_user_task_project(active_grant_required=True)`（`task_service.py:815-849`：先 gate/User 锁，再 `resolve_access(for_update)` 锁项目，锁后重读成员） | E：可以取消本人原轮次的任务；V/R/A：404 | 一致 | 只需补矩阵用例 |
| 重试 `POST /tasks/{id}/retry` | `tasks.py:92-133` → `retry_task:640-666` | 读本人任务；`admission_preflight_aware` 类型先在锁外按原模型预检，再走 `require_project(content.read)`；非作者且 handler 未声明 `collaboration_execution_ready` 时返回 409；锁内要求原 grant | E：章节、分析、批量、大纲、实体、向导可以重试（E4 已覆盖）；Agent、知识库、拆书返回 409 或 404 | 一致；Agent 留给 E5a | 已在 E4 各片验证，无新增缺口 |
| 继续 `POST /tasks/{id}/resume` | `tasks.py:136-138` → `resume_task:669-731` | 同取消，使用 `active_grant_required=True`；有 pending 富文档提案时返回 409 `rich_review_pending`；`revalidate_resume` 钩子 | E：可以继续批量（E4-g2 已测）；降级后被拒 | 一致 | 只需补矩阵用例 |
| 提前结束 `POST /tasks/{id}/finish` | `tasks.py:141-148` → `finish_paused_task:734-790` | 同上 | E：理论上可以 | 一致 | **全仓没有协作者调用 `/finish` 的用例** |
| 删除 `DELETE /tasks/{id}` | `tasks.py:163-185` → `delete_task:1321-1355` | 读本人任务；`_lock_user_task_project`（不要求活动 grant），只能删除已结束任务 | V/E：可以删除本人已结束任务（`test_viewer_can_delete_own_finished...`）；R：404 | 一致 | 无 |
| 清理 `DELETE /tasks` | `tasks.py:40-46` → `clear_finished:1410-1427` / `_lock_accessible_history_projects:1360` | 可见谓词 + 逐项目锁，非作者要求 accepted | 同上 | 一致 | 无 |
| 系统裁剪 | `prune_finished:1430` / `_lock_history_projects:1380` | 按本人 ID、终态、UUID，不要求作品权限 | — | 与 §6.3「后台按 Task 本人 ID…裁剪」一致 | 无 |
| 富文档提案读取 `GET /tasks/{id}/rich-proposals/{token}` | `tasks.py:151-155` → `rich_document_review.get_review:916-946`；`_owned_proposal:809`；`_require_review_readable:876-913` | 初次读取冻结 `_ReviewReadIdentity`，构建响应后用**独立会话**单条 SQL 复核：本人、原 token、OPEN 时 `active_grant_required`、提案和章节的身份 | E：可以读本人 paused 任务的提案；V：只能读已结束任务的提案（OPEN 状态被隐藏）；R/A：404 | 一致 | 无（PG 已有 `test_collaboration_review_read_postgresql.py`） |
| 富文档提案采纳 `POST .../accept` | `tasks.py:158-161` → `accept_review:949` → `_accept_review:1020-` | `require_project(ai.use, for_update, visibility_write, grant_identity=原 grant)`；ForbiddenError 转为 404；非作者要求原 token_version；锁后比对任务、提案初始身份；`_require_proposal_round` 校验执行收据；非作者必须带 `execution_receipt` 和 `_model_binding` | E：可以采纳本人原轮次的提案；V/R/A：404 | 一致 | E4-g2 记录：「批量审核点的富文档提案对协作者是否可采纳未专门覆盖」（implement.md:321） |
| Agent 回合任务 | `agent/service.py:701,1314,1447` 调用 `get_owned_task` | Agent 对协作者整体关闭 | — | 归属 E5a | 不属于本片 |
| 管理员任务监控 `/admin/tasks*` | `api/v1/admin_tasks.py:23-34`；`services/admin_task_service.py:73-74`（不读取 title/input/result/output_text/checkpoint） | RBAC `tasks.read`/取消权限 | 只能看到元数据；可以按管理员规则执行取消（drain） | design 只禁止管理员「人工读取权」旁路，管理员任务运维不在此列 | 需要裁决，见 §6 |

**共同特点**：所有写入口都按以下顺序取锁与重验：`authz.require_authenticated(write)` → `project_access.resolve_access(for_update[, visibility_write])`（gate → User → 可选发送写门禁 → 项目父行锁 → 重读成员）→ `refresh_same_task`，然后比较原 `task_key`/`grant`/`queued_at`，不一致时返回 409 `task_round_changed`（`task_service.py:815-849`），符合 design §5.1。

---

## 2. SSE 与帧发布身份

### 2.1 两套管线

| | 非受控（生产作者路径） | 受控（目前只在测试中挂载） |
|---|---|---|
| 入口 | `tasks.py:75` `sse_response(engine.subscribe(... guarded 默认 False))` | `stream_lease.task_sse_response(engine.subscribe(..., guarded=True), sessionmaker, binding)`（`stream_lease.py:387`） |
| 订阅时绑定 | `_capture_stream_binding`（`stream_lease.py:115-148`）冻结 actor、token_version、task_id/key、project_id、作者 ID、`last_deleted_at`、`queued_at`、task_grant、member_grant、`active_grant_required` | 同左 |
| 每帧证明 | 在 `engine.subscribe` 内的 `checked_frame`（约 engine.py:2700-2840）中，用独立会话执行 `_visible_task(task_visible_to_actor...)` + `matches_binding`，用 `project_payload` 投影，投影后再复验。**发送时不持锁**，复验和 ASGI send 之间存在窗口 | 除左侧步骤外，每个非心跳帧还会在**实际 ASGI send 的同一协程内**持有 `_task_frame_lease`（`stream_lease.py:152-221`）：认证发送屏障 → `ai_dispatch._lock(read)` → `Project FOR SHARE` → 作者和本人配置共享门禁 → `Task FOR SHARE`（附可见谓词）→ 成员行按原 `invitation_id`/`revision` 精确匹配；随后 `verify_stream_frame`（`stream_evidence.py:145-236`）校验帧证据，send 前后两次 `_require_frame_integrity` |
| 撤权串行化 | 无（窗口内可能发出撤权后的一帧） | 撤权要持有项目 FOR UPDATE，与帧租约的 FOR SHARE 互斥；`test_sqlite_task_frame_send_and_revocation_are_serialized_over_tcp`（`tests/test_task_sse_scope.py:184-`）、`test_task_sse_scope_postgresql.py` 已证明 |
| 失权表现 | `error {"task": null, "detail": "任务访问权限已失效", "code": "permission_denied"}` | 同左（`stream_lease.py:333-343`）；HTTP 已发出 200，不会改成 404，符合 §5.3 |

### 2.2 受控管线目前能证明哪些帧

`verify_stream_frame` 的分支（`stream_evidence.py:169-232`）：

1. `TaskViewEvidence` 且任务为 `queued`：只接受 `snapshot`，并在锁内重算 `to_out` + `project_task_out`，摘要必须相等。
2. `TaskViewEvidence` 且任务非 queued：**只接受**「作者在删除作品期间收到的固定取消帧」，**其余一律返回 False**。因此在受控路径下，已结束任务回放（`_finished_events`）、个人任务回放、以及从排队转为已结束时的回放都会失败。
3. `PublicationSource`（只能由 `issue_publication` 签发，engine.py:1335-1348，前提是 handler 实现了 `stream_guard`）：
   - 若仍是原执行，调用 `guard.require_task_read(db)`（`collaboration_ai_guard.py:530-577`）；
   - 若已进入终态，要求 `hub.terminal_source`（engine.py:1726-1738 在终态提交后由 `issue_terminal` 签发），再调用 `guard.require_terminal_read`（`collaboration_ai_guard.py:579-`）；
   - 终态尚未登记时抛出 `TerminalEvidencePending`，等待 `hub.terminal_ready` 后重试一次。

**已实现 `stream_guard` 的 handler 只有两个**：`handlers/outline.py:202`、`handlers/entities.py:197`。其余 handler（chapter、analysis、batch、wizard、kb、book_import、agent）在 `_consume.checked` 中走 `hub.source = current`（engine.py:1349），领取时设置 `hub.source = execution`（engine.py:1106），都**不是** `PublicationSource`，所以这些任务的运行帧在受控路径下必定失败。

`SendGuard.require_task_read` 还会显式拒绝 `_chapter`、`_document_claim`、`_embedding_inputs`（注释：「章节和索引有额外资源占用；尚无对应帧租约验证协议」，`collaboration_ai_guard.py:541-544`）；`require_terminal_read` 另外拒绝 `_analysis_execution`（`:592-596`）。因此，即使给章节、分析、批量补上 `stream_guard`，也必须同时扩展这两个读证明协议。

### 2.3 订阅与撤权的现有机制

- 订阅时，若 `project_id` 不为空，会登记到 `engine._collaboration_subscribers[(project_id, actor_id)]`（engine.py:2907-2909 一带）。
- `notify_collaboration_change(project_id, actor_id)`（engine.py:2529-2550）在协作变更提交后（`api/v1/collaboration.py:35,44,55,90,99`）完成三件事：
  - 向订阅队列塞入 `_COLLABORATION_CHECK_EVENT`，订阅侧收到后执行 `read_visible()`，失败时发 `permission_denied`；
  - 唤醒调度器（E4-a：排队任务在领取锁内复核）；
  - 对本进程中运行的同 actor 任务启动 `_check_collaboration_runs`（engine.py:2552-）：只有数据库确认原轮次失效或作者模型授权已关闭，才执行 `cancel_running`。
- **跨实例或重启**：通知只在本进程内生效。另一实例上的订阅依靠两点兜底：
  - 每帧复验（受控路径下为锁内复验）；
  - 心跳超时执行的 `read_visible()`，`heartbeat_seconds = 15.0`（engine.py:201）。
  因此空闲流最晚约 15 秒后才发现撤权。
- `task_visible_to_actor` **不检查 `allow_owner_ai`**。关闭模型授权会使 revision 递增（测试中的 `model_permission` 参数就依赖这一点），所以订阅侧通过轮次不一致失败。若未来出现「不递增 revision 的模型开关」（E4-g2 用例中有「直接关模型授权不递增 revision」的数据库篡改场景），订阅侧只能等运行体失败后的终态帧。

### 2.4 设计要求下帧应如何证明（盘点出的可行做法）

依据 design §6.3 / §5.3：撤权或降级后不能再订阅活动 AI；降为 viewer 后仍可读本人已结束记录；已发出的数据不承诺撤回。

- **订阅时绑定授权轮次**：已经实现（`_StreamBinding.task_grant`/`member_grant`/`active_grant_required`）。活动态进入的流会一直要求原 editor 轮次，**任务在流中途结束也不会放宽**（`task_visible_to_actor` 的 `active_grant_required` 注释）。因此，从活动态开始订阅、中途被降为 viewer 的用户，看不到终态帧，只能收到 `permission_denied`，需要重新订阅（届时按已结束任务回放）。这符合 design，但前端需要处理成「刷新后重读」（见 §4）。
- **每帧锁内复查数据库**：受控管线已经实现，并已证明与撤权串行化。这是 E5 应采用的方案；不需要「定期复查」，心跳复查只作为空闲流的兜底。
- **需要补的帧证明类型**：
  1. **持久终态回放证明**（已结束任务、个人任务、排队后结束、收据收尾）。可沿用 queued 分支的写法：在帧租约内用 `capture_task_view(task) == evidence.source`，确认任务处于 FINISHED（或 paused）状态，然后在锁内重算 `_finished_events` 的投影（`to_out` + `project_task_out(active_grant_required=binding)` + `result`/`output_text`），比较摘要。这样不依赖发送时的 `SendGuard`，也就**不要求当前可见性戳或模型绑定仍然有效**（收据收尾正是这些失效后才会发生）；作品读取权仍由帧租约中的 `task_visible_to_actor` 保证。
  2. **运行帧证明扩展到有资源占用的 handler**：章节（`_chapter` claim）、分析（`_analysis_execution`/pending）、批量（`_batch_attempt` + chapter claim）、向导（`_resume`/pending）。每个 handler 需要：
     - 实现 `stream_guard`，返回本次执行固定的 `SendGuard`；
     - 在 `require_task_read`/`require_terminal_read` 中各自增加「锁内只读核对 claim 仍属本执行」分支。可以复用 E4 中 `require_task_write` 的核对代码，但必须改用共享锁或只读核对，不能取写锁，否则会和帧租约的 FOR SHARE 形成自锁。
     - 知识库（`_document_claim`/`_embedding_inputs`）属于 E6 范围；拆书是个人任务；Agent 属于 E5a。

### 2.5 收据收尾帧怎样带上任务快照、且不发给失权订阅者

现状（engine.py）：

- `_run` 内的四条收尾分支设置 `status="receipt_completed"` 后 `return`（:1483 章节、:1490 分析、:1497 向导、:1511 批量；另有提交未确认时的回退分支 :1570-1591、:1636-1672）。`return` 跳过了 :1726 的 `issue_terminal`，`finally` 分支（:1740-1748）因 status 不是 succeeded/paused，执行 `hub.close("error", {"task": out(None), "detail": error})`。
- `recover()` 中的 `close_hub(... "error", {"task": None, "detail": *_DETAIL})`（:412、:423、:474）只在本进程存在残留 hub 时生效，通常没有订阅者。
- 规范 `.trellis/spec/backend/error-handling.md:401-409` 把「Hub 以不含任务快照的 error 关闭，已授权订阅者重订阅时按数据库终态回放」写成了契约。

可行做法（以下只盘点改动点，不替代裁决）：

- **方案 A（推荐盘点结论）**：受限收尾函数（`_complete_from_chapter_receipt`、`_complete_from_analysis_receipt`、`_complete_from_wizard_receipt`、`_complete_from_batch_checkpoint`）提交后，回读刚提交的 Task 行（按原 UUID、轮次，`status='succeeded'`，或批量的停止终态）。然后：
  - 关闭时改发 `done {"task": out_json(fact), "result": fact.result}`（批量非 succeeded 时发 `error {"task": out, "detail": error}`）；
  - 证据为 `capture_task_view(fact)`（即 §2.4 的持久终态证明），**不使用** PublicationSource/SendGuard。
  - 在受控路径中，帧租约按订阅绑定执行 `task_visible_to_actor(active_grant_required=binding)`：已撤权、已降级（活动态进入的流）、token 已变的订阅者都会失败，并收到 `permission_denied`。仍有授权的订阅者（例如作者被撤销的是可见性戳而非成员关系）能拿到带快照的 done。
  - 在非受控路径（作者）中，`checked_frame` 已对 done 执行 `_visible_task` + 投影，同样不会发给失权的作者（token 已变时失败）。
  - 需要同步改写 error-handling.md 中「不含快照的 error」这一契约。
- **方案 B（最小改动）**：维持 error 形状，但带上投影后的 `task` 快照（status=succeeded），并让前端把 `error` 中 `task.status==='succeeded'` 判为成功。缺点是事件语义被混用，前端 `subscribeTask` 的 error 分支（`frontend/src/api/tasks.ts` 中 `case 'error'`）把非 cancelled/interrupted 统一当作 `failed`，需要专门修改。
- 无论哪种方案，**都不能**为收据收尾签发 `PublicationSource`/`TerminalEvidence`：这两者依赖 `SendGuard.require_terminal_read`，后者会重验当前可见性戳、模型绑定和认证，而这些失效正是走收据收尾的原因，所以收尾后的帧必定无法通过验证。

---

## 3. 富文档提案（任务路由下）

- 读取：`get_review`（`rich_document_review.py:916-946`）。冻结身份后读取载荷，`_require_review_readable`（:876-913）在独立会话（`visibility_sessionmaker`）中执行单条 SQL：本人、原 token、`task_visible_to_actor(active_grant_required=OPEN)`、提案/章/run/token/status/content_revision 一致、场景属于该章。PG 下 RR 隔离的情况由 `test_collaboration_review_read_postgresql.py` 覆盖。
- 采纳：`_accept_review`（:1020-）。`require_project(ai.use, for_update, visibility_write, grant_identity=原 grant)`；作者和协作者通过 `(project.user_id == user_id) != (original_grant is None)` 互斥校验；协作者要求原 `_task_actor_token_version`，并要求提案带 `execution_receipt` 与 `_model_binding`，且后者与 `_project_model` 逐字一致；锁后用 `_owned_proposal(lock=True)` 复读，`_require_proposal_round` 校验执行收据；批量调用 `batch_service.revalidate`。
- SSE 中的 paused 富文档帧：`checked_frame` 对 `paused` + `reason=='rich_document_review'` 做联表复核（Task.checkpoint.rich_review 与 SceneReviewProposal pending 一致，并满足可见谓词），engine.py 约 2760-2790。受控路径中，paused 终态帧依靠 `hub.terminal_source`。章节和批量的 paused 帧在 `require_terminal_read` 中会被 `_chapter` 拒绝，问题同 §2.2。
- `resume`/`finish` 遇到 pending 提案时分别返回 409 `rich_review_pending`，或丢弃提案并以 cancelled（单章）/succeeded（批量）结束。
- 相关测试：`test_collaboration_review_read*.py`、`test_collaboration_review_claim*.py`、`test_collaboration_editor_proposal*.py`、`test_collaboration_log_privacy.py`、`test_collaboration_analysis_execution.py`。**缺口**：批量审核点由协作者采纳和 finish 的专门用例（implement.md:321）。

---

## 4. 前端涉及位置（只列文件）

规范：`.trellis/spec/frontend/{index,hook-guidelines,state-management,type-safety,component-guidelines,quality-guidelines}.md`（`hook-guidelines.md` 与 `index.md` 提及 SSE）。

- **能力推导（阶段性门禁）**：`src/utils/projectAccess.ts:44-46`（`canUseAIEntitled = can_use_ai`；`canUseAI = role==='owner' && can_use_ai`，注释说明阶段 E/G3 完成前只对作者开放）、`src/hooks/useProjectAccess.ts`、`src/stores/project.ts`（`isProjectScopeCurrent(scope, 'canUseAI' | 'canUseAIEntitled')`）。
- **任务中心与订阅**：
  - `src/components/AppLayout.tsx:42-48`（`showTaskPanel = projectId===null || canUseAI`，导航同样受此控制）；
  - `src/components/TaskPanel.tsx`：取消/重试/继续/结束按钮在 238-340 行；`TaskOutputModal` 在 419-469 行，其中**已结束任务输出也要求 `canUseAI`**；
  - `src/stores/tasks.ts`、`src/hooks/useTaskStream.ts`（遇到 404/409/422 判为 lost）；
  - `src/api/tasks.ts:326-465`（`subscribeTask`：`error` 中 `code==='permission_denied'` 触发 `onProjectAccessDenied` 并抛出 403；任务为 null 的 error 判为 `failed`）；
  - `src/api/sse.ts`。
- **富文档审核**：`src/components/scenes/RichReviewModal.tsx:25,68-83`（已按 `canUseAIEntitled` 和 role∈{owner, editor} 判断）、`src/api/richDocument.ts`。
- **各 AI 按钮与任务入口（目前都按 `canUseAI` 控制）**：`src/components/WizardGenerator.tsx`、`components/batch/BatchBanner.tsx`、`components/outlines/OutlineTaskModal.tsx`、`components/entities/{EntityTaskModal,TaskResultModal,CharacterRelationsTab}.tsx`、`components/chapter/{RewritePanel,VersionDrawer,AnalysisDrawer}.tsx`、`components/scenes/{SceneWritingPanel,RichSectionRewriteModal}.tsx`、`components/agent/{useAgentConversation.ts,AgentTimeline.tsx,AgentChangeCard.tsx}`（Agent 属于 E5a，保持关闭）、`components/StudioNavigation.tsx`、`hooks/useChapterAnalysis.ts`、`pages/project/{ChapterEditorPage,ChaptersPanel,CharactersPanel,OrganizationsPanel,CareersPanel,RelationshipsPanel,WorldPanel,OutlinesPanel,OverviewPanel,KnowledgePanel,MemoriesPanel,ProjectLayout}.tsx`、`pages/project/knowledgeUpload.ts`（知识库属于 E6）、`pages/ReaderPage.tsx`。
- **错误码处理**：`src/api/client.ts`（`captureProjectFailureForRequest`）、`src/stores/projectFailure.test.ts`；业务码 `permission_denied`、`task_round_changed`、`task_admission_changed`、`task_actor_unavailable`、`rich_review_pending`、`rich_review_task_changed`、`project_readonly`。
- 既有前端记录：`research/phase-g-access.md:8`、`phase-g-project-ui.md:6,31`、`phase-g-integration.md:19,28,36,43-44`（SSE 作用域、握手 403/404、`permission_denied` 清理）。

---

## 5. E5 相关 strict xfail 与关闭态断言

### 5.1 strict xfail（reason 以「待实现：协作者任务流需引擎帧发布身份证明…」开头）

| file:line | 用例 | 参数 | 转正前需改写的点 |
|---|---|---|---|
| `tests/test_collaboration_notifications.py:50-110` | `test_http_stream_wakes_after_project_grant_change` | revoke / downgrade / model_permission（3） | 用 `engine.ensure_hub(..., text="已发送快照")` 伪造 running hub，没有 PublicationSource。若协作者走受控管线，snapshot 帧必定失败，需要改为真实引擎执行（参照 `test_task_sse_engine_http.py` 的 fake_llm + Gate），或改用 outline/entity 等已有 stream_guard 的任务 |
| `tests/test_collaboration_notifications_postgresql.py:20-` | `test_postgres_stream_wakes_only_after_commit` | revoke / downgrade（2） | 同上 |
| `tests/test_collaboration_task_postgresql.py:40-124` | `test_postgres_http_stream_rechecks_after_cross_instance_revoke` | 1 | 伪造 hub + `hub.append`，问题同上；monkeypatch 的 `engine._visible_task` 在受控路径下不再是唯一检查点（帧租约另行查询），断言时序需要重写 |
| `tests/test_collaboration_task_privacy.py:130-146` | `test_finished_stream_is_authorized_in_http_and_denied_after_revocation` | 1 | 已结束任务回放依赖 §2.4 的「持久终态证明」 |

（implement.md:216/269 中「3 个 SSE xfail」「1 xfailed」指上面对应的 PG 子集。全仓 strict xfail 还包括 `test_api_key_actor_fencing.py`、`test_agent_mcp_skills.py`、`test_agent_authenticated_actor.py:324`、`test_shared_http_knowledge_e6.py`、`test_user_token_identity_reuse.py`，均与 E5 无关；`test_collaboration_editor_proposal.py` 的 xfail 已在 E4-g1 转正，当前不存在。）

### 5.2 关闭态断言与注释（E5 落地后需要改写，不能删除）

- `tests/test_task_sse_scope.py:163-170` `test_collaborator_stream_fails_closed_without_engine_frame_proof`：valid/finished 两种任务均断言 404。E5 后应改为「受控 200 + 帧证明」，同时保留「伪造证明返回 permission_denied」的负对照（同文件 `:54` 已有）。
- 生产代码 `backend/app/api/v1/tasks.py:70-72`（`if binding.member_grant is not None: raise NotFoundError`）。
- 依赖非受控回放、必须保持通过的作者/个人用例：`tests/test_task_sse_scope.py:173` `test_personal_completed_task_stream_remains_available`。另有约 64 个测试文件使用 `stream_task`/`run_task`/`/stream`（`tests/helpers.py:335-343`）。
- 注释型（不是断言，转正后同步更新说明）：`tests/test_collaboration_chapter_execution.py:62`；`test_collaboration_{chapter_entry_e4f:3, analysis_entry_e4g:9, batch_entry_e4g2:7, outline_entry_e4g3:8, entity_entry_e4g3b:10, wizard_entry_e4g3c:11}.py` 文件头的「协作者 SSE 属 E5…经任务详情观察终态」。
- `tests/test_collaboration_chapter_entry_e4f.py:531` 定义了 `RECEIPT_DETAIL = engine_module.RECEIPT_FINALIZED_DETAIL`，但没有找到对帧形状的 HTTP 断言。收据收尾帧的形状目前**没有测试锁定**。
- 规范契约：`.trellis/spec/backend/error-handling.md:401,403,407,409`（四种收据收尾都写明「Hub 以不含任务快照的 error 关闭」）。

---

## 6. 风险与需要设计裁决的点

1. **作者是否也切换到受控管线**：受控管线目前不能回放已结束任务，也不能证明章节等运行帧。如果统一切换，约 64 个使用 `stream_task` 的测试文件和全部作者 SSE 行为都会受影响。可选做法：
   - E5 只让 `member_grant is not None` 的订阅走受控管线，作者维持现状（风险最小）；
   - 先把四类证明补齐，再统一切换。
   需要裁决。
2. **管理员兼协作者**：`_capture_stream_binding` 只看成员行；`task_visible_to_actor` 不认管理员身份，所以 A+M 按 M 处理，A 为 404。管理员任务监控（`/admin/tasks`）只读元数据，但可以按 RBAC 取消（drain）他人任务。design 只禁止「人工读取权」旁路个人任务，管理员运维取消是否算在「控制他人任务」之内，需要确认（现状为允许）。
3. **降为 viewer 的 paused 任务**：paused 属于 OPEN，降级后对本人隐藏，`resume`/`finish`/`cancel` 都返回 404；`_check_collaboration_runs` 只处理 running，E4-a 只处理 queued。这类任务会**永久停在 paused**，本人看不到，也无法结束，作者同样看不到（不是作者的任务）。design §6.3 写有「待审核/运行中任务先走系统失败收尾再参与裁剪」，目前没有找到 paused 的系统收尾实现。需要裁决：撤权或降级时由系统将其 CAS 为 failed/cancelled 并丢弃提案，还是降级后允许以 viewer 身份 `finish`/`cancel`。
4. **从活动态进入的流在任务结束后的语义**：中途降为 viewer（仍是 accepted）的用户收不到终态帧（被 `active_grant_required` 拦截），会收到 `permission_denied`。前端 `subscribeTask` 会触发 `onProjectAccessDenied` 并刷新作品；之后能否以 viewer 身份重新订阅、回放已结束任务，取决于 §2.4 的持久终态证明。需要确认这是期望的用户体验。
5. **跨实例**：通知只在本进程生效；另一实例的活动流靠逐帧锁内复验，空闲流靠 15 秒心跳。受控管线下撤权之后的帧一定发不出（锁互斥），但空闲期间的感知延迟最长约 15 秒。是否需要缩短心跳，或用 PG `LISTEN/NOTIFY` 跨实例唤醒，需要裁决（design 只要求以数据库为准）。
6. **已发出的数据不承诺撤回**：受控管线只保证撤权提交之后不再发帧。撤权之前已排队或已发出的字节，以及已到达供应商的请求，都无法撤回（design §5.3）。前端在 `permission_denied` 时清除本地输出（phase-g-integration.md:28），但不能声称服务端已撤回。
7. **模型授权关闭但 revision 未递增**：`task_visible_to_actor` 不检查 `allow_owner_ai`，订阅侧只能等运行体失败后的终态帧。正常 PATCH 会递增 revision，只有直接改库才会出现。是否要在 `_task_frame_lease` 中额外检查 `task_requires_owner_ai` + `owner_ai_allowed`，需要裁决。
8. **帧租约开销**：受控管线每帧要开一个会话并取多把共享锁（SQLite 下是全库写锁语义的认证屏障）。delta 合并上限为 128 条或 16KB（engine.py 约 2990-3010），协作者高频流在 SQLite 上的吞吐与撤权等待需要实测（`frame_timeout=5s`）。
9. **收据收尾帧形状变化**会改变 error-handling.md 契约和前端终态判定（方案 A 把 error 改为 done；方案 B 在 error 中携带 succeeded 快照）。需要裁决，并同步改规范。
10. **`require_task_read` 的写锁、读锁协议**：章节、分析、批量的 claim 核对在 E4 中都按 `require_task_write`（写事务）实现。帧租约持有的是 FOR SHARE 共享锁，在 SQLite 下如果复用写路径会自锁（E4-g3a 已记录类似问题）。需要新的只读核对原语。
11. **Agent 帧**：`proposal`/`executed` 事件在 `checked_frame` 中有专门的投影（engine.py 约 2735-2760），受控管线下还需要 Agent 的 `stream_guard`。按 E5a 处理，E5 期间对协作者 Agent 任务保持 404 或 409。

---

## 7. 切片方案

原则沿用 E4：每片可独立合并和复核；协作者入口在本片证明齐全之前保持失败关闭；SQLite 逐文件 + 真实 PG 16 隔离库；负对照（屏蔽关键校验后用例必须变红）。

### E5-a 读写面矩阵补测与 paused 收尾裁决落地（后端，基本不改主路径）

- **改动点**：主要是测试。
  - 若 §6-3 裁决为「系统收尾」：在 `notify_collaboration_change` 和领取扫描中加入 paused 协作任务按 UUID + queued_at + grant CAS 结束、丢弃 pending 提案（`rich_document_review.discard_pending`），并 `record_state_change`。
  - 若裁决为「viewer 可结束」：在 `finish_paused_task`/`cancel_task` 中为 paused 放宽 `active_grant_required`，但不允许 `resume`。
- **测试矩阵**：O / E / V / R(pending, declined, revoked, 非成员) / A(只读、可写) / A+M(viewer, editor) × {list, detail, output, cancel, retry, resume, finish, delete, clear, rich-proposal get/accept} × 任务状态 {queued, running, paused, finished} × {原轮次, 重邀后旧轮次, 降级}。补齐 `/finish` 和批量审核点采纳的协作者用例。
- **PG**：在 `test_collaboration_task_postgresql.py` 中增加 finish/resume 与撤权的两种先后顺序（锁内重验）。
- **依赖**：无（可最先开始）。
- **测试变更**：只新增，不转正 xfail。

### E5-b 持久终态帧证明（受控管线基础件）

- **改动点**：
  - `stream_evidence.verify_stream_frame` 增加非 queued 的 `TaskViewEvidence` 分支：状态为 FINISHED 或 paused，`capture_task_view(task)==source`，在锁内重算 `_finished_events` 投影（`to_out` + `project_task_out(active_grant_required=binding.active_grant_required, expected_grant, expected_queued_at)` + result/output_text），比较摘要；
  - 个人任务（project 为 None）同样适用；
  - `engine.subscribe(guarded=True)` 的回放路径已用 `persisted_source(row)`，只需确保投影一致。
- **测试**：
  - 受控回放：作者、E、V 的已结束任务 200；
  - 撤权后 404 或 `permission_denied`；
  - 伪造 view、篡改 result 或 output 返回 `permission_denied`；
  - paused 富文档帧回放（联表复核仍需通过）。
- **PG**：已结束回放与撤权并发（帧租约 FOR SHARE 与撤权 FOR UPDATE 的两种先后顺序）。
- **依赖**：无；E5-c、E5-d 依赖本片。
- **测试变更**：本片还**不转正** `test_finished_stream_is_authorized...`，因为生产路由尚未挂载受控管线，可以先用 `/_test` 路由验证。

### E5-c 运行帧发布身份扩展（按 handler 分子片）

- **E5-c1 章节 + 分析**：
  - `ChapterHandler`、`AnalysisHandler` 实现 `stream_guard`；
  - `SendGuard.require_task_read`/`require_terminal_read` 去掉 `_chapter`、`_analysis_execution` 的一刀切拒绝，改为锁内**只读**核对 claim 和 pending 仍属本执行（共享锁）；
  - paused 富文档帧（单章）走 terminal 证明。
- **E5-c2 批量**：`BatchHandler.stream_guard` 绑定 `_batch_attempt` 与当前 chapter claim；`chapter_start`/`chapter_end`/`batch_end` 等自定义事件也必须带 PublicationSource（通过 `hub.publish` 携带 `hub.source`）。
- **E5-c3 向导**：`WizardHandler.stream_guard`，`_resume` 与 pending 凭据走只读核对。
- **测试**（每个子片）：
  - 真实引擎执行（fake_llm + Gate）经受控 `/_test` 路由与 TCP（参照 `test_task_sse_engine_http.py`），覆盖 snapshot、delta、progress、自定义事件、done/paused；
  - 流中 × {撤权, 降级, 关模型授权, 改密, 重邀} × {通知, 无通知} → 后续帧发不出、发出 `permission_denied`、零泄漏；
  - 负对照：让 `stream_guard` 返回其他 guard，或跳过 claim 只读核对。
- **PG**：每个子片至少一项「发送帧与撤权串行化」和「跨实例撤权」。
- **依赖**：E5-b（终态和回放共用基础）。三个子片可以并行，但都要修改 `collaboration_ai_guard.py` 的读证明，需要串行合并或预先拆出公共函数。
- **测试变更**：只新增。

### E5-d 收据收尾帧

- **改动点**：
  - 四个 `_complete_from_*_receipt`/`_complete_from_batch_checkpoint` 成功后，回读已提交行，用于 `hub.close` 的数据（按 §2.5 方案 A 或 B 裁决）；
  - 证据使用 `capture_task_view(fact)`（E5-b 的持久终态证明）；
  - 非受控作者路径的 `checked_frame` 已执行可见性复验，无需修改；
  - 同步修改 `.trellis/spec/backend/error-handling.md:401-409`。
- **测试**：
  - 四种 kind × {作者, 协作者} × {撤权, 降级, 关模型授权, 改密, 可见性变化} 下的收尾：仍有授权的订阅者收到带快照的成功帧；已失权的订阅者收到 `permission_denied`，且帧中不含 result；
  - 伪造收据时保留 running 屏障，不发成功帧；
  - `recover()` 路径（无订阅者）行为不变。
- **PG**：收尾提交与撤权并发时，订阅者只会得到两种结果之一，即成功帧或失权帧，并且与数据库的提交顺序一致。
- **依赖**：E5-b；协作者订阅侧的验证依赖 E5-f 挂载路由，也可先用 `/_test` 路由。
- **测试变更**：在相应 E4 文件中增加帧断言（目前只定义了 `RECEIPT_DETAIL`，没有断言）。

### E5-e 撤权通知与订阅收尾（可并入 E5-f）

- **改动点**：
  - 确认 `_COLLABORATION_CHECK_EVENT` 在受控订阅（`subscriber.guarded`）下以三元组入队（engine.py:2533 已处理）；
  - 视 §6-7 裁决，在 `_task_frame_lease` 中增加作者模型授权检查；
  - 视 §6-5 裁决，调整心跳或跨实例唤醒。
- **测试**：本进程通知后 3 秒内收到 `permission_denied`；无通知时下一帧或心跳失败关闭；作者任务不受成员变化影响。
- **依赖**：E5-c 的任一子片（需要可证明的真实运行流）。

### E5-f 生产路由开放与 xfail 转正

- **改动点**：`api/v1/tasks.py:59-80`。当 `binding.member_grant is not None` 时改用 `task_sse_response(engine.subscribe(..., guarded=True), sessionmaker, binding)`，作者是否同时切换按 §6-1 裁决。对 Agent 和知识库这类尚无 stream_guard 的 kind，协作者订阅保持 404 或在首帧失败关闭，需要明确选择并写入测试。
- **转正与改写**：
  - `test_collaboration_notifications.py:50`（3）、`test_collaboration_notifications_postgresql.py:20`（2）、`test_collaboration_task_postgresql.py:40`（1）：去掉 xfail，并把伪造 hub 改为真实引擎执行；
  - `test_collaboration_task_privacy.py:130`（1）：去掉 xfail；
  - `test_task_sse_scope.py:163`：改写为「协作者受控 200；伪造证明返回 `permission_denied`；Agent/知识库类按裁决断言」；
  - 更新 E4 文件头注释与 `test_collaboration_chapter_execution.py:62`，并可把「经任务详情观察终态」改为直接订阅 SSE（新增断言，保留原详情断言）。
- **回归**：作者 SSE 全部相关文件（约 64 个），按 slim-regression 先跑受影响文件与哨兵测试，阶段收口时再跑全量；PG 跑 `test_task_sse_scope_postgresql.py`、`test_collaboration_task_postgresql.py`、`test_collaboration_notifications_postgresql.py`、`test_task_client_identity_postgresql.py`、`test_collaboration_review_*_postgresql.py`。
- **依赖**：E5-b、E5-c（至少 c1）、E5-d、E5-e。

### E5-g 前端

- **改动点**：
  - `projectAccess.ts:46` 的 `canUseAI` 改为按 `can_use_ai`（需要 Agent、知识库仍按 E5a/E6 单独关闭，可新增 `canUseAgent` 等细分能力，或保留这些组件的 owner 门禁）；
  - `AppLayout` 的 TaskPanel 挂载改为「`can_read` 时挂载，只读模式下隐藏 AI 操作」，使 viewer 能看到本人已结束的任务；
  - `TaskOutputModal` 读取已结束任务输出改为按 `canRead`；
  - `subscribeTask` 适配收据收尾帧的新形状；
  - 按钮显示规则：E 有 `can_use_ai`；V 只读；A+M 按成员身份；
  - 409 `task_round_changed`/`rich_review_*` 时刷新事实。
- **测试**：vitest 能力矩阵、旧响应和授权代次、`permission_denied` 清理、收据帧终态；真实浏览器分别验证作者、viewer、editor 和管理员交集路径。
- **依赖**：E5-f（后端实际开放）和 E5-d（帧形状）。

### 推荐顺序

E5-a（可与 b 并行）→ E5-b → E5-c1 → E5-d → E5-e → E5-f（先开放章节、分析、大纲、实体）→ E5-c2 / E5-c3（批量、向导开放后再放行对应 kind）→ E5-g。

一种更保守的拆法：先只为**大纲/实体**（已有 stream_guard）和**已结束回放**开放协作者 SSE（b + f-lite），其余 kind 在首帧失败关闭，等 c 系列逐个补齐后再放开。好处是早期就能转正 `test_finished_stream...` 和以大纲任务改写的通知用例。

---

## Files Found

| File Path | Description |
|---|---|
| `backend/app/api/v1/tasks.py` | 任务路由；SSE 在 59-80 行，协作者在 70-72 行返回 404 |
| `backend/app/api/sse.py` | 通用 SSE 与改写流的 `_FrameGuardedResponse`（非任务） |
| `backend/app/services/task_service.py` | 列表、详情、输出、取消、重试、继续、结束、删除、清理、裁剪；`_lock_user_task_project:815` |
| `backend/app/services/collaboration_task_access.py` | `task_visible_to_actor`、`task_grant_identity`、`bind_task_grant`、`owner_ai_allowed` |
| `backend/app/services/task_engine/engine.py` | `subscribe:2602`、`checked_frame`、`notify_collaboration_change:2529`、`_check_collaboration_runs:2552`、收据收尾 1483-1672、终态证明签发 1726-1738、`finally` 关闭 1740-1748、`_finished_events:3060` |
| `backend/app/services/task_engine/hub.py` | TaskHub；guarded 订阅者以三元组携带 `hub.source` |
| `backend/app/services/task_engine/stream_lease.py` | `_capture_stream_binding:115`、`_task_frame_lease:152`、`_TaskFrameGuardedResponse:250`、`task_sse_response:387` |
| `backend/app/services/task_engine/stream_evidence.py` | `issue_publication`、`issue_terminal`、`capture_task_view`、`verify_stream_frame:145` |
| `backend/app/services/collaboration_ai_guard.py` | `SendGuard.require_task_read:530`（拒绝章节、文档、向量）、`require_terminal_read:579` |
| `backend/app/services/task_engine/handlers/{outline,entities}.py` | 仅有的 `stream_guard` 实现 |
| `backend/app/services/rich_document_review.py` | `get_review:916`、`_require_review_readable:876`、`_accept_review:1020` |
| `backend/app/services/admin_task_service.py` | 管理员任务只读元数据与取消 |
| `frontend/src/utils/projectAccess.ts` | `canUseAI` 阶段性只对作者开放 |
| `frontend/src/components/TaskPanel.tsx`、`AppLayout.tsx`、`api/tasks.ts`、`hooks/useTaskStream.ts` | 任务中心、订阅与终态判定 |

## Related Specs

- `.trellis/spec/backend/error-handling.md:401-409`：四种收据收尾契约（不含快照的 error 帧）。
- `.trellis/spec/backend/agent.md`：并发视图与 Agent 帧（E5a）。
- `.trellis/spec/backend/rich-documents.md`、`batch-generate.md`、`admin-task-monitoring.md`：提案、批量审核点、管理员任务监控。
- `.trellis/spec/frontend/hook-guidelines.md`、`state-management.md`：订阅和作用域失效。
- 既有研究：`research/phase-e-task-privacy.md`、`phase-e-task-review.md`、`phase-e-review-read.md`、`phase-g-access.md`、`phase-g-integration.md`。

## Caveats / Not Found

- 未运行任何测试。对「受控管线拒绝已结束回放、拒绝章节等运行帧」的判断来自代码阅读（`stream_evidence.py:169-206`、`collaboration_ai_guard.py:541-544,592-596`、engine.py:1349），落地前应先用 `/_test` 路由写一个失败用例确认。
- 没有找到 paused 协作任务在撤权或降级后的系统收尾实现，也没有找到对应测试。
- 没有找到任何断言收据收尾帧形状的测试。
- `test_collaboration_notifications_postgresql.py` 的完整用例体只读了声明部分。
- Agent（E5a）、知识库（E6）、拆书（个人任务）的帧发布身份未展开盘点。
