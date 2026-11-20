# 实施计划（Implement）：协作者的实时进度订阅

> 执行前提：需用户审批规划汇总并运行 `task.py start` 后再开始编码。
> 工作区保护：严禁修改或暂存第 21 项（`11-19-collaboration-audit-query-ui/`）已有的 27 个未提交文件（若 `README.md` 与 `.trellis/spec/backend/index.md` 需改动，仅做最小增量编辑并保留第 21 项已有修改）。

## 阶段 1：SQLite 共享读帧租约（解决慢模型断流）

1. **`backend/app/services/task_engine/stream_lease.py`**
   - 新增私有 `_SqliteSharedLease` 与 `_SQLITE_SHARED_FRAME_LEASES`、以及严格校验的 `is_sqlite_shared_frame_lease(db)`。
   - 在 `_task_frame_lease` 中：SQLite 且 `PRAGMA journal_mode != "wal"` 时，以 `exec_driver_sql("BEGIN")` + `SELECT 1 FROM sqlite_master LIMIT 1` 建立真实 SQLite `SHARED` 读事务并登记，在 `finally` 中无条件注销并关闭释放；WAL 模式回退既有写锁路径。
2. **4 处共享读路径接入 `is_sqlite_shared_frame_lease(db)`**
   - `backend/app/services/authorization_service.py`（`require_authenticated_for_send`）
   - `backend/app/services/ai_dispatch.py`（`_lock` 的 `write=False` SQLite 分支）
   - `backend/app/services/ai_config_service.py`（`lock_config_owner` 的 `write=False` SQLite 分支）
   - `backend/app/services/project_ai_config.py`（`_validate_binding` 的 `lock_config=True` SQLite 分支）
3. **移除临时缓解**
   - 删除 `backend/app/services/consistency/check.py` 中 `SQLITE_SEND_GAP`、`_needs_send_gap`、`_before_send` 及其在 `check.py`、`impact_task.py` 中的调用。
4. **阶段 1 后端测试**
   - 升级 `backend/tests/test_consistency_stream_slow_model.py`：验证单次响应头延迟大于 `frame_timeout` 与 `_LOCK_WAIT_SECONDS` 时（含连续多章出站、慢模型等待期间新订阅），协作者进度流完整不断线、零重连、零 `permission_denied`。
   - 补测 SQLite `SHARED` 帧租约安全回归（`test_task_sse_scope.py` / 新增 `test_sqlite_shared_frame_lease.py`）：
     - 帧租约发送期间写方（撤权/改权限/改密）仍被阻塞至帧结束，下一帧立即被拒；
     - `_send_lease` 与 `_task_frame_lease` 并发 0 阻塞；两个 `_task_frame_lease` 并发 0 阻塞；
     - WAL 模式下自动回退写锁路径，串行化不退化；
     - 伪造会话/跨协程/跨事务/提交后 `is_sqlite_shared_frame_lease` 恒为 `False`；
     - 取消与超时后无残留 `SHARED` 锁。
   - 回归：`test_task_sse_scope.py`、`test_task_sse_frame_guard.py`、`test_ai_visibility_dispatch.py`、`test_consistency_stream_slow_model.py`、`test_collaboration_stream_*.py`。

## 阶段 2：后端 `agent.turn` 协作者受控实时流与即时断开

5. **`backend/app/services/collaboration_ai_guard.py`**
   - `_make` 放开 `"agent.turn"` 的 `publication_only=True`。
   - 新增 `SendGuard._agent_stream_shape()` 并在 `_stream_readable()` 中路由。
   - 新增 `SendGuard._verify_agent_conversation(db)`，在 `require_task_read` 与 `require_terminal_read` 中对 `agent.turn` 执行会话归属、邀请轮次与开放历史代次只读核对。
   - 扩展 `publication_execution_matches` 与 `publication_successor` 支持 `agent.turn`。
6. **`backend/app/services/agent/runner.py` & `backend/app/services/task_engine/handlers/agent.py`**
   - `_AgentExecutionState` 增加 `publication_guard` 缓存字段。
   - `AgentHandler` 实现 `stream_guard(job, execution=..., current_execution=..., event=..., data=...)`，按 `(current_execution, model_binding, stamp, cipher, allow_private, extras)` 缓存并返回 `publication_only=True` 的 `SendGuard`。
   - `runner.py` 在断点推进后进入下一轮慢操作/工具调用前补发轻量 `stage` 刷新，立即推进 `hub.source` 唤醒在途帧。
7. **`backend/app/services/task_engine/hub.py`、`stream_evidence.py` & `engine.py`**
   - `TaskHub.append` 允许同执行 `agent.turn` 后继来源推进 `text_source` 而不误标 `_mixed_text_source`。
   - `stream_evidence.py` 将后继来源等待推广至 `agent.turn`，并在 `verify_stream_frame` 对 `proposal` / `executed` 帧做锁内 `AgentChange` 存在与归属复核。
   - `engine.subscribe -> read_visible()` 对活动协作任务同步复核 `owner_ai_allowed`（`task_requires_owner_ai` 为真时），保障直接改库关闭 `allow_owner_ai` 也能在唤醒/心跳立即断开。
8. **阶段 2 后端测试（SQLite + PostgreSQL）**
   - 更新既有断言 `agent.turn` 404 的测试：`test_task_sse_scope.py`、`test_agent_entries_e4id.py`、`test_agent_revocation_e4ie.py`、`test_collaboration_stream_early_subscribe_e5e.py`（保留 `kb.index` / `book.import` 仍为 404）。
   - 新增专项回归 `backend/tests/test_collaboration_stream_agent.py` 与 `backend/tests/test_collaboration_stream_agent_postgresql.py`：
     - 完整实时事件流：`snapshot` / `progress` / `reset` / `delta` / `tool_start` / `tool_end` / `proposal` / `executed` / `paused` / `done` / `error`（本人模型 + 作者授权模型 + 共享向量）；
     - 早订阅（排队中 / 领取瞬间 / 首个事件前）、多轮跨断点在途帧、第 2 轮中途重连快照；
     - 慢模型（响应头 > `frame_timeout`）下 SQLite 实时流不断开；
     - 实时撤权矩阵（撤权 / 降为 viewer / 关 `allow_owner_ai`（含直接改库）/ 改密停用 / 收紧可见性 × 有通知与无通知）验证立即断开且零泄露后续帧；
     - 身份边界：降为 viewer 与重邀后可回放本人已结束轮次、不可订阅活动/暂停轮次；撤权与纯管理员 404；他人轮次 404；
     - PostgreSQL 真实行锁/共享锁并发验证（帧租约与撤权两种先后顺序、慢模型发送 lease 与帧租约并发无阻塞、跨实例无通知撤权）。

## 阶段 3：前端实时订阅与失权恢复

9. **`frontend/src/api/tasks.ts`**
   - 将 `'agent.turn'` 加入 `COLLABORATOR_STREAM_KINDS`。
10. **`frontend/src/hooks/useTaskStream.ts`**
    - 为 `TaskOutcome` 的 `lost` 分支增加 `accessLost?: boolean`，在 `onProjectAccessDenied` 或 403/404 `ApiError` 时置 `true`。
11. **`frontend/src/components/agent/useAgentConversation.ts`**
    - 放开已就绪且具备 `canUseAgent` 权限的协作者进入实时流订阅（`follow()`），接收实时文本增量、工具步骤、提案与终态。
    - 调整 `agentProgressMode` 与 `startAgentTurnPolling`（活动或待确认轮次使用 5 秒可见轮询兜底，失权后停止）。
    - 在 `follow()` 结束回调处理 `end.status === 'lost'`：清 `steps` 与 `following.current`；当 `end.accessLost` 为真时调用 `accessLost()` 立即刷新权限并切只读/不可访问。
12. **阶段 3 前端测试**
    - 更新 `frontend/src/components/agent/useAgentConversation.test.ts`、`frontend/src/utils/projectAccess.test.ts`；补充 `useTaskStream.test.ts` / `AgentPanel.test.ts` 覆盖协作者实时订阅、失权即时断开（`accessLost: true`）与普通网络断开区分。

## 阶段 4：文档同步与全量验证

13. **更新规范与文档**
    - `.trellis/spec/backend/agent.md`（更新裁决 10 与协作订阅契约）
    - `.trellis/spec/backend/error-handling.md`（补充 `agent.turn` 受控帧证明、SQLite 共享读帧租约）
    - `README.md`（更新「当前限制」第 1074 行，小心保留第 21 项未提交改动）
    - `TODOS.md`（完成验收后勾选第 22 项并记录实际验证结果）
14. **验证门禁命令**
    - 后端静态检查：`cd backend && uv run ruff check .`
    - 后端全量 SQLite 回归：`cd backend && uv run pytest -n 4 --dist loadfile --durations=20`
    - 后端 PostgreSQL 专项回归（若本地 PostgreSQL 可用）：`cd backend && TEST_POSTGRES_URL=... uv run pytest -n 0 tests/test_collaboration_stream_agent_postgresql.py tests/test_collaboration_stream_replay_e5b_postgresql.py tests/test_collaboration_stream_running_e5c1_postgresql.py tests/test_collaboration_stream_batch_e5c2_postgresql.py tests/test_collaboration_stream_early_subscribe_e5e_postgresql.py tests/test_consistency_collaboration_postgresql.py tests/test_agent_revocation_e4ie_postgresql.py`
    - 前端门禁：`cd frontend && npm run lint && npm run build && npm test`
    - 隔离环境 + 本地假模型 + 真实 Chrome 浏览器验收（覆盖 AC-6 全部 3 个场景，验证记录写入 `research/verification.md`）。

## 高风险文件与回滚点

- **高风险文件**：
  - `backend/app/services/task_engine/stream_lease.py`（帧租约与 SQLite 共享读事务边界）
  - `backend/app/services/collaboration_ai_guard.py`（`SendGuard` 安全形状与锁内只读核对）
  - `backend/app/services/task_engine/stream_evidence.py`（中央帧校验与后继来源等待）
  - `backend/app/services/authorization_service.py` / `ai_dispatch.py` / `ai_config_service.py` / `project_ai_config.py`（4 处共享读分支：严禁影响 `_send_lease` 或任何写路径）
- **回滚点**：
  - 检查点 A：阶段 1 完成后运行全部任务流与可见性并发测试，绿色后再推进阶段 2。
  - 检查点 B：阶段 2 完成后运行全部协作与智能体测试（SQLite + PG），绿色后再推进阶段 3。
  - 检查点 C：阶段 3 完成后运行前端全量与浏览器验收。
