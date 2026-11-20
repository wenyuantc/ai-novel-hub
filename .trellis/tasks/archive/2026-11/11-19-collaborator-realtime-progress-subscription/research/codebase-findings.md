# 调研记录：协作者的实时进度订阅（第 22 项）

日期：2026-11-19

## 1. 现状与根因定位

### 1.1 协作者智能体轮次（`agent.turn`）为何当前只能轮询

- **后端入口白名单**：`backend/app/api/v1/tasks.py:71-82` 对 `binding.member_grant is not None`（协作者，含管理员兼协作者）调用 `stream_evidence.stream_kind_open(task.kind)`，为 `False` 直接抛 `NotFoundError("任务不存在")`（404）。
- **白名单定义**：`backend/app/services/task_engine/stream_evidence.py:457-473` 中 `stream_kind_open` 只检查 `callable(getattr(get_handler(kind), "stream_guard", None))`。`TaskEngine._claim_source`（`backend/app/services/task_engine/engine.py:1373-1388`）同样按 handler 是否实现 `stream_guard` 决定是否签发 `ClaimSource`。
- **缺失实现**：`AgentHandler`（`backend/app/services/task_engine/handlers/agent.py:334-750`）已声明 `execution_identity_aware = True`、`admission_preflight_aware = True`、`collaboration_execution_ready = True`，实现了 `verify_event`、`verify_finalization`、`on_abort`、`finalize_revoked_pause`，但未实现 `stream_guard`。
- **`SendGuard` 支持现状**：
  - `collaboration_ai_guard.for_task`（`backend/app/services/collaboration_ai_guard.py:1510-1558`）已校验 `agent.turn` 的 `_agent_actor_token_version` 与 `_task_actor_token_version`、`_project_model`、`_shared_embedding`（`extra_bindings`）。
  - `collaboration_ai_guard._make`（`:1413-1416`）目前仅允许 `("chapter.batch", *_WIZARD_KINDS)` 使用 `publication_only=True`。
  - `SendGuard._stream_readable`（`:574-586`）当前未针对 `agent.turn` 做专属形状校验（`_agent_stream_shape`），也未核对 `AgentConversation` 存在性、归属、邀请轮次与历史代次。
  - `AgentHandler.build_job`（`backend/app/services/task_engine/handlers/agent.py:532-668`）对作者和协作者均已冻结 `job.model_binding`、`job.cipher`、`job.allow_private`、`job.stamp` 与 `job.execution_state`。
- **多步断点推进与 `TaskHub` 文本源**：
  - `backend/app/services/agent/runner.py` 在单次执行内最多有 6 处 `_checkpoint()` 推进（`:945` `mcp_tool_map`、`:806` `auto_execute_used`、`:855` `pending_change_ids`、`:861` `processed_assistant_id`、`:1105` `assistant_message_id`/`finished_message_id`、`:1126` 达工具上限终态），均保持 `same_execution(original, current)` 与 `step_cursor == 0`，只改变 `checkpoint`。
  - `stream_evidence.py:788-803` 目前仅对 `_is_batch_guard(guard)`（`chapter.batch`）支持同执行后继来源（`publication_successor` + `PublicationSourcePending`）。非批量 guard 遇到 `Task.checkpoint` 已推进时会落入 `TerminalEvidencePending`，等到整个轮次结束并触发 5 秒 `frame_timeout` 中断。
  - `TaskHub.append` / `TaskHub.snapshot_source`（`backend/app/services/task_engine/hub.py:96-118`）：多轮对话中第 1 轮与第 2 轮都产生 `delta` 且中间不发 `reset`，若换了 `PublicationSource` 会把 `_mixed_text_source` 置真，导致第 2 轮中途重连时 `snapshot_source()` 返回 `None` 而 `permission_denied`。
- **撤权即时断开与 `allow_owner_ai` 唤醒**：
  - `collaboration_service._write` 提交后调用 `engine.notify_collaboration_change(project_id, actor_id)`（`backend/app/services/task_engine/engine.py:2893-2918`），向 `_collaboration_subscribers[(project_id, actor_id)]` 投递 `_COLLABORATION_CHECK_EVENT`，并异步复查运行中任务与暂停任务。
  - `engine.subscribe`（`:3383-3387`）收到 `_COLLABORATION_CHECK_EVENT` 后调用 `read_visible()`；对于通过 HTTP 修改 `allow_owner_ai` 的情况，成员 `revision` 已递增，`task_visible_to_actor(active_grant_required=True)` 会立即返回空；若数据库直接翻动 `allow_owner_ai=False` 且未递增 `revision`，`read_visible()` 当前不核对 `owner_ai_allowed`，需一并补齐以保证唤醒与心跳同样即时关闭。
- **前端现状**：
  - `frontend/src/api/tasks.ts:117-128`：`COLLABORATOR_STREAM_KINDS` 未包含 `'agent.turn'`。
  - `frontend/src/components/agent/useAgentConversation.ts:19-20, 89-109, 160-162, 252-293`：仅 `author = access.ready && access.role === 'owner'` 订阅 SSE，协作者走 `agentProgressMode -> 'poll'` 与 3 秒轮询 `GET /projects/{pid}/agent/conversations/{cid}`。
  - `frontend/src/components/agent/useAgentConversation.ts:266`：`follow()` 返回 `status: 'lost'` 时只清空 `following.current` 并直接 `return`，未区分「失权导致 lost（需触发 `accessLost()`、清 `steps`、阻断重订）」与「网络重连耗尽（保留错误提示并由轮询/手动刷新恢复）」。

### 1.2 SQLite 上协作者任务进度流在慢模型下中断的根因

- **PostgreSQL vs SQLite 锁模型差异**：
  - PostgreSQL 上，出站模型发送租约（`_GuardedProvider._send_lease`，`backend/app/services/ai_dispatch.py:520-599`）与受控 SSE 帧租约（`_task_frame_lease`，`backend/app/services/task_engine/stream_lease.py:160-233`）都只取**共享锁**（`FOR SHARE` / `pg_try_advisory_xact_lock_shared` / `pg_advisory_xact_lock_shared`），互不阻塞；撤权/改权限/收紧可见性/改配置等写方取**排他锁**（`FOR UPDATE` / `pg_advisory_xact_lock`），与任一读租约严格串行。
  - SQLite 没有行级共享锁，现代码在 4 处共享读路径上用 **no-op `UPDATE`** 模拟共享锁：
    1. `backend/app/services/authorization_service.py:264-265`：`require_authenticated_for_send` -> `lock_users`（`UPDATE users`）
    2. `backend/app/services/ai_dispatch.py:349-385`：`_lock(write=False)` -> `_lock_sqlite`（`UPDATE projects`，且 `PRAGMA busy_timeout = 2000`，超时抛 `VisibilityBusyError`）
    3. `backend/app/services/ai_config_service.py:54-60`：`lock_config_owner(write=False)`（`UPDATE users`）
    4. `backend/app/services/project_ai_config.py:903-913`：`_validate_binding(lock_config=True)`（`UPDATE ai_configs`）
  - 每次模型出站在 `DispatchHooks._before_request` 到 `_after_headers` 之间持续持有 `_send_lease`，在 SQLite 上即持有全库唯一的 `RESERVED` 写锁。
  - 慢模型（首包响应头超过 2 秒或 5 秒，或连续多次出站）期间，协作者 SSE 任意帧进入 `_TaskFrameGuardedResponse._send_frame`（`frame_timeout = 5.0` 秒），其 `_task_frame_lease` 与 `guard.require_task_read` / `require_terminal_read` 调用上述 4 处 no-op `UPDATE`，与 `_send_lease` 争抢同一把 SQLite 写锁：
    - 撞上 `_lock_sqlite` 2 秒超时（`VisibilityBusyError`）或默认 5 秒 `SQLITE_BUSY` -> `_send_frame` 返回 `error {code: "permission_denied"}` 并断开流；
    - 撞上 `anyio.fail_after(5.0)` -> `_send_frame` 返回 `"abort"`，直接切断 HTTP 流。
  - 一致性检查此前在 `backend/app/services/consistency/check.py:74-75, 673-687` 与 `impact_task.py:407, 428` 加了临时缓解 `SQLITE_SEND_GAP = 0.25`（出站前 `sleep(0.25)`），单次响应头 > 5 秒或模型等待响应头期间新订阅/重连仍必断，且其他协作任务（`chapter.*`、`outline.*`、`wizard.*`、`agent.turn` 等）全受影响。

### 1.3 SQLite `SHARED` 读事务实证结论

已用本机 `uv run python` 对 `sqlite+aiosqlite` 文件库做独立实测：
- 默认回滚日志模式（`PRAGMA journal_mode` 为 `delete`；项目 `backend/app/core/database.py` 不开启 WAL）：
  1. 连接 1 执行未提交 `UPDATE`（模拟 `_send_lease` 持 `RESERVED` 写锁）。
  2. 连接 2 执行 `await conn.exec_driver_sql("BEGIN")` + `SELECT`（模拟 `_task_frame_lease` 持真实 SQLite `SHARED` 读锁）：耗时 **0.8 ms**，完全不阻塞，且 `root.is_active == True`、`physical.get_transaction() is not None`、`raw.in_transaction == True`。
  3. 连接 1 关闭后，连接 3 执行 `UPDATE` + `COMMIT`（模拟撤权/改配置/改密等写方提交）：被连接 2 的 `SHARED` 锁**阻塞**（需 `EXCLUSIVE` 锁），无法在帧发送期间提交。
  4. 连接 2 `close()` 释放 `SHARED` 锁后，连接 3 的 `COMMIT` 立即完成，且连接归还连接池后 `raw.in_transaction == False`。
- WAL 模式（若用户手工启用 `PRAGMA journal_mode=WAL`）：SQLite `SHARED` 锁不阻塞写方 `COMMIT`，因此仅在 `journal_mode != "wal"` 时启用共享读帧租约；若检测到 `wal` 则保守回退现有写锁路径（失败关闭，不削弱撤权串行化）。

## 2. 受影响的既有测试与文档

- 断言「协作者 `agent.turn` 订阅 404」的后端测试（需翻转或更新）：
  - `backend/tests/test_task_sse_scope.py:164-197`（将 `agent.turn` 移出关闭列表，保留 `kb.index` / `book.import` 404）
  - `backend/tests/test_agent_entries_e4id.py:465-485`（翻转为协作者可订阅已结束/进行中 `agent.turn` 且受控）
  - `backend/tests/test_agent_revocation_e4ie.py:164-166`（注意：该用例涵盖 4 种撤权 `revoke` / `downgrade` / `owner_ai` / `reinvite`；撤权后 404、降为 viewer 或重邀/关作者模型后对已结束任务应允许按持久回放或按原绑定规则校验，需按身份分别断言）
  - `backend/tests/test_collaboration_stream_early_subscribe_e5e.py:754`（将 `agent.turn` 移出未开放列表并纳入早订阅覆盖，保留 `kb.index` / `book.import`）
- 慢模型临时缓解测试：
  - `backend/tests/test_consistency_stream_slow_model.py:1-91`（删除 `_needs_send_gap` 依赖，升级为单次响应头耗时超过 `frame_timeout` 与 `_LOCK_WAIT_SECONDS` 的真正根治回归）
- 前端测试：
  - `frontend/src/components/agent/useAgentConversation.test.ts`（更新 `agentProgressMode`、流式/轮询协作规则与失权恢复用例）
  - `frontend/src/utils/projectAccess.test.ts:117`（`'agent.turn'` 对协作者返回 `'stream'`，`'kb.index'` / `'book.import'` 仍 `'status'`）
- 规范与文档：
  - `.trellis/spec/backend/agent.md`（更新裁决 10：协作者开放受控 `agent.turn` 实时订阅）
  - `.trellis/spec/backend/error-handling.md`（补充 `agent.turn` 受控帧证明、SQLite 共享读帧租约）
  - `README.md`（`:1074` 去除「智能体轮次对协作者只能轮询查看进度」限制说明；同步注意保留第 21 项未提交改动）
  - `TODOS.md`（交付后更新第 22 项状态与验收记录）

## 3. 关联风险项（独立待办，不并入本任务）

- **第 33 项（`TODOS.md:309-315`）**：轮次进行中 `GET /projects/{pid}/agent/conversations/{cid}` 可能因 `personal_projection.py:646-652` 对运行中 `Task` 快照的两次读取差异偶发 404「智能体轮次不存在」。前端 `useAgentConversation.ts` 在 `onSnapshot` / `onProposal` / `onExecuted` 及定时刷新中会调用该接口；本任务保持边界清晰，不修改第 33 项，在规划风险与验收记录中显式注明。
