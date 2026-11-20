# 技术设计（Design）：协作者的实时进度订阅

## 1. 架构与边界总览

本设计分为两大正交模块，均沿用既有 E5 受控流式管线，不新增外部依赖或数据库迁移：

```
                    ┌─────────────────────────────────────────────────────┐
                    │                GET /tasks/{id}/stream               │
                    │  (作者/个人 → 非受控 sse_response；协作者 → 受控管线)   │
                    └──────────────────────────┬──────────────────────────┘
                                               │
                    ┌──────────────────────────▼──────────────────────────┐
                    │           TaskEngine.subscribe(guarded=True)        │
                    │  - 唤醒检查：read_visible (+ allow_owner_ai 复核)     │
                    │  - 帧过滤：checked_frame (投影 + public_changes)     │
                    └──────────────────────────┬──────────────────────────┘
                                               │ (event, data, TaskStreamEvidence)
                    ┌──────────────────────────▼──────────────────────────┐
                    │      _TaskFrameGuardedResponse._send_frame()        │
                    │  - _task_frame_lease():                             │
                    │      PG: FOR SHARE + advisory_xact_lock_shared      │
                    │      SQLite (非 WAL): BEGIN + SELECT (SHARED 读锁)   │
                    │  - verify_stream_frame():                           │
                    │      ClaimSource / QueuedSource / TaskViewEvidence  │
                    │      PublicationSource (含 agent.turn 后继来源)       │
                    │  - ASGI send() 在同一租约内完成                       │
                    └─────────────────────────────────────────────────────┘
```

---

## 2. 模块一：修复 SQLite 慢模型下协作者任务进度流中断

### 2.1 根因回顾

SQLite 锁兼容性矩阵（默认回滚日志模式 `delete`/`truncate`/`persist`/`memory`）：

| 既有锁 \ 新请求锁 | `SHARED` | `RESERVED` | `EXCLUSIVE`（`COMMIT`） |
|---|---|---|---|
| `SHARED`（`BEGIN` + `SELECT`） | 兼容（0ms） | 兼容（0ms） | **阻塞**（等待所有 `SHARED` 释放） |
| `RESERVED`（未提交 `UPDATE`） | **兼容（0ms）** | **阻塞** | **阻塞** |

慢模型断流的原因是 `_send_lease`（等待 LLM 响应头，持 `RESERVED`）与 `_task_frame_lease`（发送每帧 SSE）在 SQLite 上都取了 `RESERVED` 写锁。

### 2.2 SQLite 共享读帧租约设计

1. **私有登记表（`backend/app/services/task_engine/stream_lease.py`）**：
   - 新增模块私有 `_SQLITE_SHARED_FRAME_LEASES: WeakKeyDictionary[AsyncSession, _SqliteSharedLease]`，记录 `(owner: asyncio.Task, session_transaction, connection, connection_transaction, driver_connection)`。
   - 仅允许在 `_task_frame_lease` 中登记，且仅当全部前提成立：
     1. `db.get_bind().dialect.name == "sqlite"`；
     2. 查询 `PRAGMA journal_mode`（在 `BEGIN` 之前通过 `conn = await db.connection(); mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()`）结果小写不为 `"wal"`；
     3. 在同一物理连接上执行 `await conn.exec_driver_sql("BEGIN")` 启动真实 SQLite `DEFERRED` 事务；
     4. 立即执行一次只读标量查询（如 `SELECT 1` 或紧随的 `SELECT` 认证行，使 SQLite C 层实际取得 `SHARED` 锁且 `raw.in_transaction` 为真——需注意 SQLite `BEGIN DEFERRED` 在首个 `SELECT` 才真正获取 `SHARED` 锁，因此在登记前先以 `SELECT 1 FROM users WHERE id = :id` 或 `SELECT 1` 触发 `SHARED` 锁，或者 `SELECT` 认证行即完成首读！为了最稳妥，在 `BEGIN` 后立刻执行 `SELECT 1 FROM sqlite_master LIMIT 1`，确保 `SHARED` 锁在进入任何业务验证前就已真实持有！）；
     5. 在 `finally` 中无条件从 `_SQLITE_SHARED_FRAME_LEASES` 移除，并先 `rollback()` / `close()` 释放 `SHARED` 锁。
   - 暴露严格校验函数 `is_sqlite_shared_frame_lease(db: AsyncSession) -> bool`：
     - 核对 `db` 在 `_SQLITE_SHARED_FRAME_LEASES` 中、`owner is asyncio.current_task()`、`root = db.get_transaction()` 与 `session_transaction` 一致且 `is_active`、无嵌套事务、`physical` 连接与 `connection_transaction` 及 `driver_connection` 均未变、且底层 `sqlite3` 连接 `in_transaction` 为真。
     - 任何校验失败返回 `False`（不跳过写锁，失败关闭）。
   - 若 `journal_mode == "wal"`，不登记 `_SQLITE_SHARED_FRAME_LEASES`，`_task_frame_lease` 走原写锁路径，保证 WAL 模式下帧发送与撤权仍串行化。

2. **4 处共享读核验适配**（仅当 `write=False` 且 `dialect == "sqlite"` 且 `stream_lease.is_sqlite_shared_frame_lease(db)` 为真时跳过 no-op `UPDATE`，其余全部 `SELECT` 与校验逻辑完全不变）：
   - `backend/app/services/authorization_service.py::require_authenticated_for_send`：跳过 `await lock_users(...)`，仍执行 `SELECT User.is_active, User.token_version`。
   - `backend/app/services/ai_dispatch.py::_lock`（`write=False` 分支）：跳过 `_lock_sqlite` 的 no-op `UPDATE`，改为 `SELECT Project.id` 校验项目存在（尊重 `include_recycled`），不存在抛 `NotFoundError("作品不存在")`。
   - `backend/app/services/ai_config_service.py::lock_config_owner`（`write=False` 分支）：跳过 `UPDATE users`，改为 `SELECT User.id` 校验存在，不存在抛 `NotFoundError("用户不存在")`。
   - `backend/app/services/project_ai_config.py::_validate_binding`（`lock_config=True` 分支）：跳过 `UPDATE ai_configs`，后续 `SELECT *_CONFIG_FIELDS` 照常校验 `instance_key` 与 `_fingerprint`。

3. **安全与锁边界证明**：
   - `_send_lease` 不在 `_SQLITE_SHARED_FRAME_LEASES` 中 → 行为 100% 不变（仍阻挡并发可见性/权限写并维持 409 `ai_visibility_busy` / `collaboration_busy`）。
   - 所有写路径（`write=True`）永不走此分支 → 行为 100% 不变。
   - PostgreSQL 路径永不走此分支 → 行为 100% 不变。
   - 帧发送期间写方（撤权、降级、关 `allow_owner_ai`、改配置、改密、收紧可见性）提交时，SQLite `COMMIT` 申请 `EXCLUSIVE` 锁，必须等待 `_task_frame_lease` 完成 ASGI `send()` 并关闭会话释放 `SHARED` 后才能提交；提交后下一帧的 `BEGIN` + `SELECT` 立即读到新事实并拒绝。
   - 取消/超时释放：`_task_frame_lease` 的 `finally` 限时 `db.close()`（失败则 `db.invalidate()`），确保异常退出不残留 `SHARED` 锁。

4. **移除临时缓解**：
   - 删除 `backend/app/services/consistency/check.py` 的 `SQLITE_SEND_GAP`、`_needs_send_gap`、`_before_send` 及其在 `check.py` 和 `impact_task.py` 中的调用。

---

## 3. 模块二：协作者 `agent.turn` 实时流式订阅与即时断开

### 3.1 `SendGuard` 扩展（`backend/app/services/collaboration_ai_guard.py`）

1. **允许 `agent.turn` 使用 `publication_only=True`**：
   - `_make`（`:1413-1416`）将允许的 `publication_only` 类型扩展为 `("chapter.batch", "agent.turn", *_WIZARD_KINDS)`。
   - `AgentHandler.stream_guard` 构造 `publication_only=True` 的 `SendGuard`，确保该对象只能用于 `require_task_read` / `require_terminal_read`，绝不能用于模型发送或任务写入。

2. **新增 `_agent_stream_shape(self) -> bool`**：
   - 仅当满足全部条件才返回 `True`：
     - `self._task` 是 `TaskExecutionIdentity` 且 `self._task.kind == "agent.turn"`、`self._task.chapter_id is None`；
     - `self._publication_only is True`；
     - 无章节、批量、分析、文档、向量输入、实体来源、向导续写、场景戳、一次性回调；
     - 主绑定为 `ResolvedProjectModel`（`model_type == "text"`）；
     - 额外绑定长度 ≤ 1，若有则必为 `ResolvedSharedEmbedding` 且 `grant_identity == self._task.grant_identity`；
     - `self._task.input["request"]["conversation_id"]` 为正整数，`self._task.resource_key == f"agent:{cid}"`，`self._task.input["message_id"]` 为正整数，`self._task.input["history_epoch"] == self._stamp.epoch`。
   - `SendGuard._stream_readable` 中加入：
     ```python
     if self._task is not None and self._task.kind == "agent.turn":
         return self._agent_stream_shape()
     ```

3. **新增 `_verify_agent_conversation(self, db: AsyncSession)`**（在 `require_task_read` 与 `require_terminal_read` 中，当 `self._task.kind == "agent.turn"` 时调用）：
   - 按 `(id=conversation_id, project_id=task.project_id, user_id=task.actor_id)` 只读查询 `AgentConversation`（PostgreSQL `FOR SHARE`，SQLite 复用共享帧租约）。
   - 核对：
     - 会话存在；
     - `conversation.collaboration_invitation_id == (None if grant is None else grant[0])`；
     - `conversation.ai_history_epoch == self._stamp.epoch`（若会话已有历史，确保未被收紧封存；与 `require_visibility_stamp` 配合，保证 `project.ai_history_epoch == self._stamp.epoch`）。
   - 不满足抛 `CollaborationSendBlocked()`。

4. **扩展 `publication_successor` 支持 `agent.turn`**：
   - 针对 `guard._task.kind == "agent.turn"` 与 `later_guard._task.kind == "agent.turn"`：
     - 两端均满足 `_agent_stream_shape()` 与 `publication_execution_matches`；
     - 同 `_actor`、同 `_binding`、同 `_extra_bindings`、同 `_stamp`、同 `_cipher`、同 `_allow_private`；
     - `same_execution(guard._task, later_guard._task)` 且 `same_execution(execution, later_execution)` 且 `same_execution(guard._task, execution)` 且 `later_execution.step_cursor == execution.step_cursor == 0`；
     - 断点单调不回退：`later_round >= old_round`（`round` 缺失按 0），且 `later_execution != execution`。
   - `publication_execution_matches` 对 `agent.turn`：要求 `guard._agent_stream_shape()` 且 `execution == original`（每个断点对应其自身的发布 guard，不放宽到任意断点）。

### 3.2 `AgentHandler.stream_guard` 与 `AgentJob` 缓存（`handlers/agent.py` & `agent/runner.py`）

1. **`_AgentExecutionState` 缓存发布 guard**：
   - 在 `backend/app/services/agent/runner.py::_AgentExecutionState` 增加 `publication_guard: tuple[tuple, object] | None = None`。
2. **`AgentHandler.stream_guard`**：
   - 校验 `type(job) is AgentJob`、`same_execution(execution, current_execution)`、`current_execution == job.task_execution_identity`、`job.model_binding`、`job.cipher`、`job.stamp` 合法。
   - 计算 `extras = agent_access.text_guard_extras(current_execution, job.model_binding)`。
   - 缓存键：`(current_execution, job.model_binding, job.stamp, job.cipher, job.allow_private, extras)`；命中则直接返回同一个 `SendGuard`（同一轮数百个 `delta` 复用同一 `PublicationSource`），未命中则构造 `collaboration_ai_guard.for_task(current_execution, job.model_binding, job.stamp, cipher=job.cipher, allow_private=job.allow_private, extra_bindings=extras, publication_only=True)` 并缓存。
3. **`runner.py` 断点推进后立即刷新来源**：
   - 在 `runner.py:806`（`auto_execute_used` 断点后）与 `:861`（工具全部处理完、进入下一轮 `build_messages` + 慢模型调用之前）以及 `:1105`（有 `calls` 进入 `_process_calls` 之前），若未紧跟立即事件，补发一次轻量的 `yield "stage", {"stage": ...}`（例如 `"正在思考…"` / `"正在执行修改…"` / `"准备调用工具…"`）：
     - 引擎 `checked("stage", ...)` 立即将 `hub.source` 推进为新断点的 `PublicationSource` 并触发 `hub.source_changed`，使在途的旧断点帧在 1ms 内经后继来源验证发出，绝不卡到下一轮慢模型响应！
     - 同时用户界面也能看到准确的阶段文案（例如工具执行完毕后恢复「正在思考…」）。

### 3.3 `TaskHub` 与 `stream_evidence` 适配

1. **`TaskHub.append` 支持 `agent.turn` 同执行跨轮文本累计（`backend/app/services/task_engine/hub.py:96-105`）**：
   - 当 `self.text` 非空且 `self.text_source != self.source` 时，检查二者是否同为 `agent.turn` 的 `PublicationSource` 且满足 `publication_successor(self.text_source.guard, self.text_source.execution, self.source.guard, self.source.execution)`：
     - 成立 → 更新 `self.text_source = self.source`（保持 `_mixed_text_source = False`），因为多轮文本均由同一执行、同一可见性戳逐帧核验产生，重连快照可由最新断点 guard 证明；
     - 不成立 → 维持原逻辑 `self._mixed_text_source = True`。
2. **`stream_evidence.py` 适配**：
   - 将 `_is_batch_guard(guard)` 扩展为 `_has_successor_guard(guard)`（支持 `chapter.batch` 与 `agent.turn`），让 `verify_stream_frame` 在 `agent.turn` 断点推进窗口内走 `publication_successor` + `PublicationSourcePending` 路径。
   - 在 `verify_stream_frame` 对 `event in ("proposal", "executed")` 且 `task.kind == "agent.turn"` 时，在帧租约锁内额外复核 `data["change"]` 对应的 `AgentChange` 行 `(id, change_key, conversation_id, project_id, turn_task_id, turn_task_key)` 仍存在且与载荷中的身份字段一致，防止提案被删或归属异常时发出脏帧。

### 3.4 撤权/关闭作者模型即时断开补强（`backend/app/services/task_engine/engine.py`）

- 在 `engine.subscribe` 的 `read_visible()` 中：当 `active_grant_required` 为真、读取到的 `row` 带 `_collaboration_grant` 且 `task_requires_owner_ai(row.input)` 为真时，同步核对 `await owner_ai_allowed(db, row.project_id, actor_id)`；若为 `False` 直接返回 `None`。
- 如此无论是经 HTTP 修改（`revision` 递增 + `notify_collaboration_change`），还是跨实例/直接改库关闭 `allow_owner_ai`，唤醒事件或心跳都会立即以 `permission_denied` 结束流。

### 3.5 前端接入与失权处理

1. **`frontend/src/api/tasks.ts`**：
   - `COLLABORATOR_STREAM_KINDS` 加入 `'agent.turn'`。
   - 更新注释说明（仅 `kb.index`、`book.import` 对协作者仍返回 404）。
2. **`frontend/src/hooks/useTaskStream.ts`**：
   - `TaskOutcome` 中为 `lost` 增加可选字段 `accessLost?: boolean`（当 `ApiError` 为 403/404 或 `code === 'permission_denied'` 时为 `true`，普通网络重连耗尽为 `false`）。
   - `onProjectAccessDenied` 触发或 403/404 抛错时标记 `accessLost: true`，供上层精准区分「失权断开」与「网络断开」。
3. **`frontend/src/components/agent/useAgentConversation.ts`**：
   - 允许**已确认权限快照就绪且具备 `canUseAgent` 能力**的作者与协作者订阅活动轮次（`canStream = access.ready && access.canUseAgent`），不再仅限 `author`：
     - `agentProgressMode(canStream, lost, active)`（或保留签名兼容并调整语义：`collaborator` 在 `queued`/`running` 时同样返回 `'stream'`，`paused` 返回 `'none'`，与作者一致）。
   - `startAgentTurnPolling`：
     - 失权（`lost`）立即停止。
     - 当存在 `activeId`（`queued`/`running`/`paused`，含跨标签页确认与流异常兜底）时，使用 `startVisiblePolling(refresh, visibility, 5000)`（作者与协作者统一 5 秒可见轮询，避免隐藏标签页空转，同时流中断/网络异常时仍可恢复）。
     - 无活动轮次时仅在页面切回可见时刷新一次。
   - `follow()` 回调处理：
     - `end.status === 'lost'` 时：清空 `following.current` 与 `steps`；若 `end.accessLost` 为真，立即调用 `void accessLost()`（停止轮询、刷新作品权限、权威重读一次切换只读或不可访问提示），彻底防止重复订阅。
     - 正常结束（`succeeded` / `paused` / `failed` / `cancelled` / `interrupted`）与作者逻辑完全一致：权威 `refresh()`、清空 `steps`、对本轮 `executed` 变更执行 `invalidate` 与 `onUpdated()`。
   - 保留 `polledTurn` 过渡监测（兜底：若流断开靠轮询发现轮次结束，同样执行 `invalidate` 与 `onUpdated()`）。
4. **`frontend/src/components/TaskPanel.tsx`**：
   - 因 `canStreamTask('agent.turn', true)` 变为 `true`，任务面板抽屉对协作者的 `agent.turn` 自动使用实时流订阅与回放，无需额外分支；核对既有 `TaskPanel` 对 `agent.turn` 的展示测试。

---

## 4. 兼容性、迁移与回滚

- **数据库迁移**：**无**（零表结构变更）。
- **API 兼容性**：
  - `GET /api/v1/tasks/{id}/stream` 对协作者的 `agent.turn` 从 404 变为 200 SSE（受控管线）；对 `kb.index` / `book.import` 与作者路径 100% 兼容。
  - 前端 `TaskOutcome` 的 `accessLost?: boolean` 为向后兼容的可选属性。
- **回滚方案**：
  - 如需回滚 `agent.turn` 协作者流，只需移除 `AgentHandler.stream_guard`（后端自动退回 404）并还原前端 `COLLABORATOR_STREAM_KINDS` 与 `useAgentConversation.ts`；无任何持久化数据格式耦合。
  - SQLite 共享帧租约完全内聚在 `stream_lease.py` 与 4 处显式守卫分支，独立可回滚，且遇到 WAL 模式自带自动退回原写锁路径的安全开关。
