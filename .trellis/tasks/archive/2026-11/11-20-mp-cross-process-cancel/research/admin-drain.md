# Research: 管理员取消的受理、清理归属和完成证明

- Query: 两个应用处理同一个管理员取消意图时，哪一步可以重复，哪一步需要一方独占，另一方和重启后的进程如何确认结果？
- Scope: 本地源码与已有测试。下文将实读行为和 C3 的设计建议分开记录。
- Date: 2026-11-23

## Findings

### 已有的外部契约

`backend/app/api/v1/admin_tasks.py:34` 的取消 POST 返回 202。受理事务复核当前用户的 `tasks.cancel` 权限，对单章再按用户、作品、任务顺序取锁，按任务 UUID、原状态、`queued_at`、原执行 UUID 和空意图条件更新。操作号、时间和 `task.cancel_request` 成功审计共用一次提交，单章同时冻结原占用。重复请求遇到同一 pending 时返回原操作号，已清理的审计可返回空日志 ID。提交后才通知本机，客户端断开后扫描仍可继续。一次失败的提交回执不能作为写入相反审计的依据。见 `backend/app/services/admin_task_service.py:188`、`backend/app/services/audit_writer.py:256` 和 `backend/app/models/audit.py:21`。

| 对外情况 | 需要保留的表现 |
| --- | --- |
| 权限或参数变化 | 会话失效 401，写前丢失权限 403，非法 UUID、未明确确认或不合法原因 422，找不到任务 404，已结束且无意图 409。审计写入失败时请求失败，真正回滚的意图不能触发清理。 |
| 受理、重复受理、清理变慢或失败 | 受理即返回 202。详情继续报告 `cancellation_pending`、`cleanup_pending` 和「取消中」。只有终态提交后才显示真实结果。 |
| 运行任务真正停止 | `TaskEngine.cancel_running` 对匹配轮次只发一次取消，领取或最终保存窗口有额外保护。它最多等 `cancel_wait_seconds`（默认 10 秒），到时执行体仍活就保留 pending。调度循环可继续处理别的任务。 |
| 本人取消同时遇到管理员意图 | `task_service._wait_admin_cancel` 原先回滚会话、请求本地 drain、等最多 10 秒，再按原 UUID 返回当前任务，同 ID 被替换则 404。C3 改为在同一截止时间内从新事务查询原轮次；本地唤醒只加快执行。等待结束不生成第二条管理意图或审计。 |

代码位置分别见 `admin_task_service.py:95`、`task_engine/engine.py:262,3191`、`task_service.py:1336`。后端规范 `.trellis/spec/backend/admin-task-monitoring.md` 的「可靠受理」「真实执行」及「错误矩阵」给出相同边界。

### 现有处理者怎样工作

- `backend/app/services/task_engine/cancellation.py:92,100,151` 按 `(task_key, operation_id)` 在进程内登记异步任务。扫描会发现全站所有 pending。C1 已令 `_load` 跳过属于另一 worker 的 running，以及本机租约无效时带 worker 的 running。旧版 `worker_key` 为空的 running、已转为待核对的行、queued、paused、带章节收据的终态仍可能被任一进程读取。
- `cancellation.py:298` 取消原执行体，并在等候后重读原轮次和 worker。`_claiming`、`_finalizing`、`_running_execution_keys` 保护单章、分析、批量的本地时间窗口。C2 的 `task_execution_slots` 还覆盖任务已变为终态、原回调没有结束的窗口。别的进程不能从自己查不到协程推出原协程已停止。
- 单章通过 `cancellation.py:37,202` 核对原章节、发起人、轮次、资源 claim 及操作号。释放章节、终态、统计和 `_admin_chapter_receipt` 已在同一事务；`_clear_chapter_receipt`（第 162 行）只消耗已证明的收据，提交回执丢失时独立会话回读。两阶段均应保留，后继章节占用不能被旧任务释放。
- `cancellation.py:338` 的分析有完整原收据才恢复成功，无证据继续阻挡；向导的未知发送或写入保留。批量待审候选使用原提案证明；生成阶段核实原 attempt，已发送但结果不明的保存未知结果收据并保护重试边界。通用 queued/paused 分支在第 437–459 行，先调用 `handler.on_abort` 再提交 `complete_state`。rich review 已释放的资源跳过回退，批量暂停带 current、未知 kind 或缺证明的异常终态保留 pending。
- `backend/app/services/task_engine/engine.py:561,631,704` 的 C1 接管者也可能独立调用 `on_abort`。管理员从 HTTP 唤醒的 drain 和执行体自己的 `_finalize` 不都经过 `_reap_lock`。只约束两个扫描器的最终 UPDATE 不能阻止先发生的重复回退。

### 四类内存记录逐项处理

| 现有记录及源码 | C3 建议 |
| --- | --- |
| `work`，`cancellation.py:84,92,147` | 保留作本机去重、唤醒和关机等待。跨进程一个操作的处理权由数据库清理登记决定。本人等待与删除等待从独立事务读取进度。 |
| `recovering`，`engine.py:631`、`cancellation.py:401,427,451` | 接管时在同一数据库事务固定本次操作的处理模式、原执行轮次和新持有者。正常执行者取消与重启接管中断能得到不同的终态。清理失败重试和接管者再次退出均保留来由。本地映射可作为缓存。 |
| `winners`，`engine.py:2097`、`cancellation.py:318` | 原进程可按任务 UUID、轮次、执行 UUID 重试已产生的 done/paused，防止取消冲掉已经提交的正文或候选。仅当前仍有权的持有者可以消费本地结果，提交与成功回调继续核对完整身份。进程丢失时使用 C1 的业务收据和既有孤立规则，无法证明成功的保留原有不确定语义。 |
| `completed`，`cancellation.py:139,199`、`engine.py:3106`、`task_service.py:1537` | 与原失败快照匹配的清理完成时，在完成事务消除对应的持久失败屏障，或保存同一操作、轮次、资源已处理的证明。若完成和清理分两次提交，后一事务必须能仅凭已提交的收据完成。项目删除不能只靠本机 completed 或一次泛化的终态查询来重试旧 abort。缓存可按现有规则分批（每批至多 500）清理。 |

### 建议的唯一处理协议

1. 执行中的任务优先交给原 worker。旧版无主且尚未判为待核对的 running，先走 `workers.prepare_orphan_scan` 与 `workers.adopt`（`backend/app/services/task_engine/workers.py:162,185`）。SQLite 使用权威侧库已确认的停止/围栏；PostgreSQL 使用主库权威结论。`recovered_at` 已有值且 `worker_key` 为空说明已持久标为待核对，不参加普通接管，只有业务允许的完整收据能够推动受保护的核对/完成步骤。未知的外部发送始终保留。
2. 对 queued/paused 及确需独立重试的收尾，复用 C3 的持久清理登记。按任务 UUID、轮次、操作号绑定原项目、原执行/资源身份，并用处理者 worker 与一次认领的 token 区分新旧持有者。领取前获得授权门所需业务锁、核对实际登记；同一原操作只产生一个有效持有者。去掉本地映射依赖后，无作品任务也需要唯一性。该登记与 C2 的并发名额用途不同，queued/paused 的取消不能占用调度配额。
3. 正常 finalizer、接管者和管理员 drain 对同一原资源共享顺序。运行中先由原执行体完成其自己的停止/回滚，迟到的库内完成不得覆盖已提交赢家。排队或独立恢复走清理登记。把核对持有者、原 claim 和实际业务 SQL 写入放到同一事务；具有独立会话的 handler 在自己的真实写入点执行核对。数据库锁在等待协程退出期间释放。接管同时更新认领 token，旧 worker 即便重新获得 CPU 也不能对新 claim 生效。
4. 清理可跨多个事务时，操作认领仅保证活持有者互斥。必须安排“业务效果 + 该步收据”同事务，或使用可核对原身份的幂等清理。重启与回执丢失先读已提交事实，再决定后续步骤。无法区分做过和未做过时保留 pending/失败屏障，删除也受阻。不以第二次执行具有副作用的回调试探第一次是否成功。
5. 处理顺序保存既有可见差异。`test_admin_chapter_claim_cancellation.py:133` 的在线管理员接手旧式无主单章且能证明原 claim，要求 `cancelled`；`test_admin_task_cancellation.py:201` 的重启扫描遇到已有 running 管理意图，要求 `interrupted`。专门测试认领发起原因、先取得处理权的事务及重试期间模式固定。可用活租约配合暂停自动扫描重建旧夹具中“停掉引擎后直接调用 drain”的时间点，保留接口与资源断言。

### 验证安排

- **运行中（AC3、AC5）**。真双应用，B 提交管理请求，A 慢发送或慢最终保存；B 先完整扫描，独立观察者核对操作号、保留的原资源与 B 的回退调用数为零。随后放行 A，一次信号、一次有效释放、一项终态统计、一条成功受理审计。交换原 worker 和受理者；覆盖单章、批量、已保存正文、暂停候选及超出 10 秒仍安全的执行。围栏 A，再让接管者在认领后退出、由下一接管者继续。恢复者 A 已迟到的回写必须失败。
- **排队或待审（AC4）**。受控暂停两方在观察到同一 pending 后、首次业务清理前，令两方竞争认领。分别令 A、B 先获权。检查有副作用的 handler 实际调用/业务释放、资源最后状态及统计；在处理者断连、用户撤权、删除同时到来时仍只有原操作生效。对 rich review 验证候选正确而原资源回退次数为零。
- **失败与代次（AC7、AC10）**。分别在清理登记提交前、业务更改后但回执返回前、第二段清收据时中断。以另一应用复查失败屏障与完成证明。原整数 ID 复用、同 UUID 重新入队、替换章节 claim 及旧 winner 的延迟回调都不能操作新轮次。未知收据、未知 kind、queued 带本人清理 execution_key 保持屏障。用两端真实请求验同一个操作号，审计已被清理时允许空日志 ID。
- **回归入口**。`backend/tests/test_admin_tasks.py`、`test_admin_task_cancellation.py`、`test_admin_task_adversarial.py`、`test_admin_task_check_regressions.py`、`test_admin_chapter_claim_cancellation.py`、`test_admin_uncertain_execution.py` 已涵盖 API 错误、审计失败、慢回滚、断连、旧轮次和提交确认丢失。新交错用例在 SQLite 和 PostgreSQL 16 串行验证，通过事件屏障和独立数据库观察确认竞争确已发生。

## Caveats / Not Found

这次只核对本地项目协议与源码，未运行 C3 竞态测试，表名与迁移随实施落定。数据库旧记录或外部动作缺少可信结果时，不能从进程死亡或只剩一个执行者推断业务效果。相关规范还有 `.trellis/spec/backend/database-guidelines.md` 的执行者租约与 SQLite 侧库段、`.trellis/spec/backend/rbac-quotas.md` 的实际 actor、授权门和提交顺序段。本次研究无需外部资料。
