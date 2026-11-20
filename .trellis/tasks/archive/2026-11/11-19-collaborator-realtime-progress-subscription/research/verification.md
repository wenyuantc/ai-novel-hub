# 验收记录：协作者的实时进度订阅（TODOS 第 22 项）

## 1. 隔离真实浏览器验收（AC-6，本机 Chrome 1440×960）

- **环境**：系统临时目录独占 SQLite（从空库 `alembic upgrade head` 升至最新迁移头）、现场随机密钥与回环随机端口、`FRONTEND_DIST_DIR` 同源托管最新生产构建包、本地 OpenAI 兼容 HTTP 假模型（支持流式输出、工具调用与 5.5 秒首字节延迟）；本机 Google Chrome + Playwright，按身份隔离独立浏览器上下文（`qa_owner`、`qa_editor`），未触碰产品 `.env` 或用户运行实例，用后自动清理临时目录。
- **结论**：**3 / 3 场景全部通过，0 失败，`pageErrors: []`**。

| 编号 | 覆盖场景 | 实测结果 |
| --- | --- | --- |
| S1 | 编辑者发起项目智能体对话，实时流式接收文本、工具调用与变更提案卡片；点击「确认」后自动续跑并流式完成，作品概览简介实时刷新 | 通过：首段流与续跑流各建立 1 次 `GET /api/v1/tasks/1/stream`（`200`），实时渲染逐字文本、工具步骤与提案卡片，确认后续跑流式完结且作品简介即时更新 |
| S2 | 编辑者智能体轮次实时流式输出中，作者先将编辑者降级为查看者、再于新轮次中途彻底撤销协作权限 | 通过：降级后 `43ms` 内断开流并切为只读「此会话仅供查看」；撤权后 `30ms` 内断开流并切为不可访问提示；假模型 8 秒后输出的秘密尾段零泄露（`secretLeaked: false`） |
| S3 | SQLite 默认回滚日志模式下，假模型每次响应头延迟 `5.5s`（超过默认 `frame_timeout = 5s` 与 `_LOCK_WAIT_SECONDS = 2s`），编辑者依次发起一致性检查（2 章抽取 + 跨章比对）与智能体轮次 | 通过：一致性检查全程保持单条 `200` SSE 不断线直至「一致性检查已完成」（`consistencyStreamCount: 1`）；慢模型智能体轮次保持单条 `200` SSE 完整收到回复（`slowAgentStreamCount: 1`），全程零 `permission_denied`、零重连 |

- **第 33 项交互观测**：在 S1 活跃轮次中调用 `GET /api/v1/projects/1/agent/conversations/1` 观测到 1 次已知的偶发 `404`（另 1 次为 S2 撤权后预期 `404`），属 `TODOS.md` 第 33 项独立既有问题；实时 SSE 流本身未受影响并正常流式完成。

## 2. 自动化门禁与双库验证

- **前端全量门禁**：`cd frontend && npm run lint && npm run build && npm test`
  - `eslint .`：0 错误、0 警告。
  - `tsc -b && vite build`：构建通过，无超限 chunk 告警；最大 chunk `vendor-antd` 1,188.09 kB（< 1,500 kB），`RelationGraph` 独立 226.00 kB，`AgentPanel` 35.17 kB，`index` 502.40 kB。
  - `vitest run`：**262 文件 / 1822 用例全部通过**（覆盖更新后的 `useAgentConversation.test.ts`、`useTaskStream.test.ts`、`projectAccess.test.ts`）。
- **PostgreSQL 16 隔离专项**：在临时 `postgres:16-alpine` 容器的独立 `ainovel_test` 库（随机 schema，用后销毁容器）运行 `tests/test_collaboration_stream_agent_postgresql.py -n 0 -v`：
  - **1/1 通过**（耗时 8.49s；覆盖 PG 下慢模型 `_send_lease` 与 `_task_frame_lease` 并发、无通知直接改库关闭 `allow_owner_ai` 即时断开、PG `FOR SHARE` 帧租约与 `DELETE` 撤权严格串行）。
- **后端 SQLite 全量门禁**：`cd backend && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check . && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20`
  - `ruff check .`：`All checks passed!`
  - `pytest`：**6976 passed, 11 skipped, 19 xfailed**（0 failed，耗时 36m48s；新增测试文件 `test_sqlite_shared_frame_lease.py` 6 例、`test_collaboration_stream_agent.py` 16 例、`test_collaboration_stream_agent_postgresql.py` 1 例，扩展 `test_consistency_stream_slow_model.py` 2 例，并同步更新原断言 `agent.turn` 404 的 4 处旧测试，同时保持 `kb.index` / `book.import` 404 断言不变）。

## 3. 验证过程中发现并根治的缺陷

1. **SQLite `SHARED` 帧租约下 `_final_visibility` 三方死锁**：并发写方进入 `COMMIT` 持 `PENDING` 锁时，原 `personal_projection._final_visibility` 另开第二条 SQLite 连接触发 `SHARED → 新连接 → PENDING → SHARED` 死锁；已改为在 `is_sqlite_shared_frame_lease(db)` 成立（快照冻结）时复用当前 `db` 连接执行只读核验，并补齐回归用例（`test_sqlite_shared_frame_lease.py`）。
2. **`agent/runner.py` 阶段刷新保持作者帧序列零变化**：初版 3 处断点后 `stage` 事件引入了新阶段文案，会广播额外 `progress` 帧并触发多余 `_flush`；已改为与 `batch_service._refresh_stage` 一致的 `_stage` / `_refresh_stage`（重发当前阶段文案、零广播、零多余写库），保持作者路径帧序列与管理员安全阶段口径不变。
3. **`engine.subscribe` 初始快照投影期间任务推进误报 `permission_denied`**：受控订阅在 `project_task_out` / `out_json_live` 或 `checked_frame` 期间恰逢任务从 `queued` 被领取或刷新进度时，旧逻辑因 `persisted_source(row) != original_view` 直接返回 `permission_denied`；已改为原绑定仍有效时有界重试（至多 5 次）或转入持久回放，期间失权则零重试立即拒绝，并补齐双分支确定性回归用例（`test_collaboration_stream_agent.py`）。
