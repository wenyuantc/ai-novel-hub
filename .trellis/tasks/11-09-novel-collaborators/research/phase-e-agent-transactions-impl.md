# Agent 事务与审计：基础切片（2026-11-10）

## 已实现

- Agent HTTP 独立会话复制认证时的 `AuthenticatedActor`，先重验原始 token version、释放只读快照，再执行业务。会话创建、更新、删除与发送在作者作品写锁前重新取得认证/User 写屏障；外部请求返回后也重新取得该屏障。后台任务没有 HTTP 会话，不伪造令牌快照。
- `AgentAuditCoordinator`、`AgentAuditScope` 和 `TransactionSession` 显式区分 preview / execute；执行的成功审计仅随业务根事务提交，预览成功回滚不记成功。失败只冻结安全快照，在所有保存点退出后写根事务；根事务回滚或提交确认不明时先释放／失效原连接，再以 operation UUID 查事实，不能把已提交成功补记为失败。重复取消不能打断受保护收尾；数据库不可用或连接释放无法确认时不声称审计必达。
- 提案确认的普通执行、预览刷新提前提交、外部执行的确认和中断收尾，以及后台工具循环的提案、工具结果、自动执行和 checkpoint 提交接入同一根事务协调器。错误经 `AgentAuthorizationChangedError` / `VisibilityPolicyError` 直接终止流程；MCP worker 的连接和工具调用及 `call_data` 不能把策略失效降级成普通插件错误。数据库 40001 映射为安全冲突，其他 DBAPIError 不向输出或引擎 traceback 传递 SQL、参数或原始异常；外部调用的未知异常只记录固定消息。
- 外部提案在发网前确认任务 UUID，写入每次新生成的执行 claim，响应后和受限失败清理均按作品、任务 UUID、提案身份、执行时间与 claim CAS。远端调用后令牌失效时不落远端结果、不续跑，只能把仍属于本次 claim 的状态安全标为结果未知。旧任务没有可证明 UUID 时，外部提案在发网前失败，需重新发起；不会删除旧任务或历史数据。
- 执行中 `AgentChange.result` 内的 `_external_claim_id` 是内部标识：Agent HTTP、任务事件与模型回执不显示，通用个人投影也作为保留字段过滤。外部结果已经发往第三方无法撤回，状态“结果未知”不表示远端副作用被回滚。

## 验证与边界

2026-11-10 在本机独立 `collab_agents` 测试库（PostgreSQL 16.15、随机 schema）运行 `AINOVEL_AGENT_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_agent_authenticated_actor.py tests/test_agent_audit_coordinator.py -k postgresql --tb=short`：**11 passed、36 deselected in 31.23s**。本结果覆盖这些 Agent 认证、提交和冲突测试；任务引擎执行身份接线后的跨模块回归仍需重跑。

在 `backend/` 下执行，所有 SQLite 数据均为测试 fixture：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_agent_audit_integration.py tests/test_agent_audit_coordinator.py \
  tests/test_agent_authenticated_actor.py -k 'not postgresql'
# 41 passed, 8 deselected in 40.06s；该次发生在随后 MCP worker 策略错误测试之前。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_agent_authenticated_actor.py -k 'not postgresql' \
  tests/test_agent_mcp_skills.py::test_external_proposal_confirm_resume \
  tests/test_agent_mcp_skills.py::test_auto_confirm_tool_reuses_one_connection
# 19 passed, 5 deselected in 20.67s；不含随后新增的自动 MCP 策略失效测试。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_agent_authenticated_actor.py::test_automatic_external_policy_failure_stops_background_turn
# 1 passed in 1.06s。

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_agent_api.py::test_confirmation_refreshes_stale_preview_then_undo_conflicts \
  tests/test_agent_api.py::test_confirm_concurrently_applies_only_once_and_turn_undo \
  tests/test_agent_api.py::test_start_task_is_confirmed_and_can_be_cancelled \
  tests/test_agent_api.py::test_cross_user_every_agent_endpoint_is_not_found \
  tests/test_agent_mcp_skills.py::test_stale_executing_external_failed_on_confirm \
  tests/test_agent_mcp_skills.py::test_recent_executing_external_still_409
# 6 passed in 10.05s；发生在外部 claim 最后一轮收紧前，最终集成仍需重跑。
```

新增真实 HTTP 测试覆盖认证后及写屏障前令牌撤销、外部调用中撤销、连接／调用阶段策略失效、自动 MCP 不继续下一次模型请求、同时间戳下 claim 替换和整数 Task ID 复用、缺 UUID 的旧任务不出网；确认和后台工具循环覆盖成功、内层失败、授权失效及 preview_changed。提交确认丢失是对真实 SQLite commit 前／后注入异常，不是实际网络分区。

本范围 Ruff 和 `git diff --check` 通过。此前一次隔离 PostgreSQL 的原始令牌撤销测试 3 passed；新增的会话四写入口、保存点与真实 40001 测试尚未在最新代码运行。另新增 PostgreSQL 双连接外部执行 claim 更换、Task UUID 更换和令牌撤销 3 个测试，仅完成收集，尚未执行。测试支持 `AINOVEL_AGENT_POSTGRES_TEST_URL`（专用 `collab_agents`）或标准 `AINOVEL_COLLAB_POSTGRES_TEST_URL`（`collab_test`），均限制本机安全数据库并使用随机 schema；由主任务错峰重跑。

本切片尚未开放协作者 Agent。`AgentAuthorizationChangedError` 还未接入实际 `ai.use`、任务绑定的邀请 UUID/revision、模型身份及每次出站／工具执行的锁内重验；个人任务创建、自动衍生、发送和受限清理仍在 E 阶段实施。未运行全后端测试，不能据此标记 E5a 或任务完成。
