# 需求文档（PRD）：协作者的实时进度订阅

## 目标与用户价值

补齐协作体验中的两项实时进度遗留能力（对应 `TODOS.md` 第 22 项）：

1. **协作者智能体轮次实时流式订阅**：已获授权的作品编辑者（含管理员兼编辑者）在项目智能体与任务面板中，可像作者一样实时接收文本流、阶段进度、工具步骤与变更提案，告别 3 秒轮询延迟；一旦被撤权、降级为查看者、关闭作者模型授权（轮次依赖作者模型/向量时）、重邀换轮次、停用账号或改密，进度流必须立即断开且不泄露后续帧。
2. **修复 SQLite 慢模型下协作者任务进度流中断**：解决 SQLite 上模型出站等待响应头期间阻塞协作者 SSE 帧租约导致的超时断流 / 误报 `permission_denied`，使所有已开放协作订阅的任务类型（含一致性检查、修改影响评估、单章、分析、批量、向导、大纲、实体与智能体）在慢模型下均能稳定接收实时进度，同时不放松撤权即时断开与帧发送串行化保障。

## 已确认事实

- 生产订阅路由 `GET /api/v1/tasks/{task_id}/stream`（`backend/app/api/v1/tasks.py:60-89`）已对协作者挂载受控管线（`engine.subscribe(guarded=True)` + `task_sse_response`），按 `stream_evidence.stream_kind_open(kind)`（即 handler 是否实现 `stream_guard`）开放类型。
- `AgentHandler`（`backend/app/services/task_engine/handlers/agent.py:334`）已具备协作执行与撤权受限收尾能力，但尚未实现 `stream_guard`；前端 `COLLABORATOR_STREAM_KINDS`（`frontend/src/api/tasks.ts:117`）与 `useAgentConversation.ts:89-109, 253-276` 因此对协作者禁用实时流并退回 3 秒轮询。
- SQLite 上 `_GuardedProvider._send_lease`（`backend/app/services/ai_dispatch.py:520`）在等待 HTTP 响应头期间通过 no-op `UPDATE` 持有 SQLite 唯一 `RESERVED` 写锁；`_task_frame_lease`（`backend/app/services/task_engine/stream_lease.py:160`）与 `SendGuard.require_task_read` / `require_terminal_read` 在 4 处共享读路径上也执行 no-op `UPDATE`，导致与 `_send_lease` 互斥，在响应头超过 2–5 秒时触发 `VisibilityBusyError` / `SQLITE_BUSY`（误发 `permission_denied`）或 `frame_timeout`（切断连接）。一致性检查中的 `SQLITE_SEND_GAP = 0.25`（`backend/app/services/consistency/check.py:75, 678`）仅是局部缓解。
- 协作权限变更提交后由 `TaskEngine.notify_collaboration_change`（`backend/app/services/task_engine/engine.py:2893`）立即唤醒本进程该作品+成员的受控订阅，跨实例/无通知场景由帧租约与心跳复核兜底。

## 功能需求

### R1：为协作者开放 `agent.turn` 受控实时流式订阅

- **R1.1 服务端受控帧证明**：`AgentHandler` 实现 `stream_guard`，使 `agent.turn` 纳入 `stream_kind_open` 白名单与 E5-e 早订阅（`QueuedSource` / `ClaimSource` / 领取瞬间等待）。
  - 覆盖 `agent.turn` 全部事件：`snapshot`、`progress`、`reset`、`delta`、`tool_start`、`tool_end`、`proposal`、`executed`、`paused`、`done`、`error`。
  - 支持 `agent.turn` 单次执行内多次 `_checkpoint()` 推进后的在途帧验证（同执行后继来源与 `PublicationSourcePending` 等待），以及多轮对话累计 `delta` 后的中途重连快照证明（不因跨轮切来源触发 `_mixed_text_source` 失败）。
  - 帧租约内核对：原认证与令牌版本、作品活跃与可见性戳、成员原 `(invitation_id, revision)` 与活动态 `editor` 角色、作者模型/共享向量授权与配置指纹、会话归属/当前邀请轮次/开放历史代次；`proposal` / `executed` 帧另在锁内复核对应 `AgentChange` 仍归属本轮次与本会话。
  - 已结束与待确认（`paused`）的 `agent.turn` 支持受控持久回放：本人降为 `viewer` 或重邀后仍可回放本人已结束轮次；活动态（`queued`/`running`/`paused`）严格要求原轮次 `editor`；已撤权/非成员/仅管理员身份一律 404。
  - 作者路径与个人任务维持原非受控管线，帧序列、内容与行为保持不变；`kb.index` 与 `book.import` 对协作者继续保持 404。
- **R1.2 撤权/降级/关授权后立即断开**：
  - 本进程内撤权、降为 `viewer`、关闭 `allow_owner_ai`（轮次依赖作者模型或作者共享向量时）、改密/停用账号后，通过 `notify_collaboration_change` 立即唤醒订阅并输出 `error {code: "permission_denied"}` 关闭连接，不等 15 秒心跳也不再发送后续帧。
  - 跨实例或无进程内通知场景（包括直接修改数据库关闭 `allow_owner_ai`）在下一帧发送租约或心跳复核中立即以 `permission_denied` 关闭。
- **R1.3 前端实时订阅与失权处理**：
  - 将 `'agent.turn'` 加入 `COLLABORATOR_STREAM_KINDS`（`frontend/src/api/tasks.ts`），使智能体面板与任务面板均对有 AI 权限的协作者开启实时流订阅。
  - `useAgentConversation`（`frontend/src/components/agent/useAgentConversation.ts`）对有 `canUseAgent` 权限且快照就绪的作者与协作者统一在活动轮次（`queued` / `running`）使用 `useTaskStream().follow()` 实时接收文本增量、工具步骤、提案与终态；权限快照未就绪或已失权（`lost`）时不发起订阅；待确认（`paused`）及活动期间保留低频可见轮询兜底（5 秒，对齐作者节奏）以便跨标签页同步与异常恢复。
  - 订阅因失权（`permission_denied` / 403 / 404）中断时，前端立即断开订阅、清空临时工具步骤、触发 `accessLost()` 刷新作品权限并重读会话（切为只读或「会话已不可访问」），不重复死循环订阅；普通网络断线重连耗尽时显示错误提示并允许刷新/轮询恢复，不误报失权。

### R2：修复 SQLite 上协作者任务进度流在慢模型下中断

- **R2.1 SQLite 共享读帧租约**：在 SQLite 默认回滚日志模式（`journal_mode != "wal"`）下，`_task_frame_lease` 建立真实 SQLite `SHARED` 读事务（显式 `BEGIN` + `SELECT`），并让仅在已登记的 SQLite 共享帧租约内调用的 4 处共享读核验（`require_authenticated_for_send`、`ai_dispatch._lock(write=False)`、`lock_config_owner(write=False)`、`project_ai_config._validate_binding(lock_config=True)`）复用该 `SHARED` 读事务而不再执行 no-op `UPDATE`。
  - 模型出站 `_send_lease`（持 `RESERVED`）持续等待慢模型响应头（即使超过 `frame_timeout = 5s` 或 `_LOCK_WAIT_SECONDS = 2s`）期间，协作者 SSE 帧租约（持 `SHARED`）立即通过验证并发送，零阻塞、零超时、零误报 `permission_denied`；多个并发协作者订阅流之间也互不阻塞。
  - 保持撤权/降级/改配置/改可见性/改密等写方提交与正在发送帧的严格串行化（写方 `COMMIT` 需 `EXCLUSIVE` 锁，等待当前帧 `send()` 完成并释放 `SHARED` 后才提交，随后下一帧立即观测到最新提交并拒绝）。
  - 若检测到 SQLite 处于 `wal` 模式，保守回退至写锁路径，避免静默破坏帧发送与撤权的串行化。
  - `_send_lease` 本身、所有写路径及 PostgreSQL 路径的锁语义保持不变。
- **R2.2 清理临时缓解并全类型受益**：移除 `backend/app/services/consistency/check.py` 与 `impact_task.py` 中的 `SQLITE_SEND_GAP` 人工等待缓解，确保所有已开放协作流订阅的任务类型在 SQLite 慢模型下统一根治。

### R3：文档与规范同步

- 同步更新 `.trellis/spec/backend/agent.md`、`.trellis/spec/backend/error-handling.md`、`README.md` 与 `TODOS.md`。

## 验收标准

- [x] **AC-1（协作者智能体实时流完整闭环）**：编辑者（使用本人模型与使用作者授权模型两条路径）发起多轮/含工具调用/含提案确认与续跑的 `agent.turn`，通过 `GET /api/v1/tasks/{id}/stream` 实时收到 `snapshot`、`progress`、`reset`、`delta`、`tool_start`、`tool_end`、`proposal`、`executed`、`paused`、`done`，文本与提案内容与作者路径一致；支持排队中、领取瞬间、首个事件前及多轮中途重连订阅。
- [x] **AC-2（智能体流撤权/降级/关授权即时断开）**：协作者订阅进行中的 `agent.turn` 时，触发撤权、降为 `viewer`、关闭 `allow_owner_ai`（依赖作者模型或作者共享向量时）、改密/停用账号或收紧 AI 可见性，本进程有通知场景立即以 `error {code: "permission_denied"}` 断开且零泄露后续帧（含 `delta`/`proposal`/`done`）；无通知/跨实例场景在下一帧租约或心跳即断开；作者对他人轮次订阅保持 404，协作者对 `kb.index` / `book.import` 订阅仍保持 404。
- [x] **AC-3（已结束回放与身份边界）**：降为 `viewer` 或重邀后的协作者可回放本人已结束 `agent.turn`，不可订阅活动/暂停轮次；已撤权与纯管理员身份一律 404。
- [x] **AC-4（SQLite 慢模型不断流）**：在 SQLite 上，模拟模型响应头延迟大于 `frame_timeout` 且大于 `_LOCK_WAIT_SECONDS`（如单次响应头 6 秒或短超时下 3 倍超时），协作者订阅 `consistency.check`、`agent.turn` 及其他协作任务时，能实时收到 `snapshot`、`progress`、`delta` 与 `done`，无中断、无重连、无 `permission_denied`，且移除 `SQLITE_SEND_GAP` 后依然全部通过。
- [x] **AC-5（SQLite 串行化与安全不退化）**：在 SQLite 上，帧租约持有 `SHARED` 锁执行 ASGI `send()` 期间，并发撤权/改权限/改可见性/改密等写操作必须等待该帧结束释放后才能提交，下一帧立即被拒（`test_sqlite_task_frame_send_and_revocation_are_serialized_over_tcp` 及新增用例通过）；`_send_lease` 对写方的 409 `ai_visibility_busy` / `collaboration_busy` 契约不变；PostgreSQL 双库回归全部通过。
- [x] **AC-6（前端与真实浏览器验收）**：前端全量 lint、build、Vitest 通过；在隔离环境 + 本地假模型 + 本机 Chrome 中实测：
  1. 编辑者发起智能体对话，实时看到逐字流式回复、工具步骤与提案卡片，确认后继续实时流式完成；
  2. 轮次流式进行中作者撤权或降级，编辑者界面立即断开流并切为只读 / 提示会话不可访问；
  3. SQLite + 慢模型（单次响应头 > 5 秒）下，编辑者发起一致性检查与智能体轮次，进度流全程不断线直至完成。

## 明确不在范围（Out of Scope）

- 不开放 `kb.index` 与 `book.import` 的协作者实时流订阅（继续返回 404）。
- 不修改第 33 项（`TODOS.md:309-315`，`GET /projects/{pid}/agent/conversations/{cid}` 轮次进行中偶发 404）；该项保持独立跟踪。
- 不涉及第 21 项工作区已有未提交改动（`11-19-collaboration-audit-query-ui/`）的文件或提交范围。
- 不修改 PostgreSQL / SQLite 的多进程调度机制（第 25 项），也不强制切换生产 SQLite 为 WAL 模式。

## 风险与已知交互

- **与第 33 项的交互**：`useAgentConversation` 在 `onSnapshot`、`onProposal`、`onExecuted` 及定时刷新时会调用 `GET /conversations/{cid}`；若恰逢轮次进行中 `Task` 状态变化，可能触发第 33 项既有偶发 404。规划与实施不以临时规避破坏现有会话刷新语义，验收时记录实际观测结果，根治留给第 33 项。
- **SQLite WAL 模式**：若外部部署手工开启 `PRAGMA journal_mode=WAL`，为保证撤权串行化不退化，帧租约会自动回退写锁路径（慢模型并发优化仅对默认回滚日志模式生效）。
