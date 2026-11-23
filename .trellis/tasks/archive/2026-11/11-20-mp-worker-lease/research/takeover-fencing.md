# Research: 执行者接管后的写入围栏

- Query: 按 C1 执行计划 0.1、0.3 核对任务执行、取消、完成与恢复的数据库写入，确定哪些路径需要额外保护。
- Scope: internal，源码静态审计，覆盖已注册的 11 个 handler 文件、任务引擎与其持久化服务。
- Date: 2026-11-23

## Findings

### 评审点 A

接管时保留 execution_key 是必须的，旧执行者和接管者都持有这个值。只比较 task_id、statistics_key、execution_key、checkpoint 或资源占用凭据，无法区分两者。在 **实际写事务** 中锁定并验证 task.worker_key 与执行开始时冻结的 worker_key，可以让这些普通事务拒绝旧执行者。凡系统清理须在用户撤权后仍生效，沿用原有授权语义，只加执行者校验。不能在调用 handler 之前另开会话检查一次就放行它后续自己开启的写会话。

另有两个恢复断点必须调整计划。单章释放占用同时删除 Task.input 的占用指针，向导撤销 pending 同时删除未提交证明。两者当前都早于 Task 终态在独立事务中提交；接管者恰在两次提交之间崩溃时，下一接管者已无法证明上一次完成了什么。详见同目录 `recovery-reentry.md`。建议在恢复专用路径让资源清理和任务终态、统计同事务提交，复用已有接受 db 的释放函数；也可选择写可验证的持久清理收据，让第二次接管能按原决定续做。只加 WHERE 条件不能解决证明丢失。按 implement.md 风险文件段，先回到规划，修订设计、验收中断点和实施步骤。

设计 §4 说失去租约但尚未被认领的旧执行体可按停机语义写中断；PRD R7 要求自我围栏后不再产生任务写入。建议统一以 R7 为准：显式正常停机仍可收尾，失去租约后由持有效租约的接管者收尾。区分「已被围栏但尚未改写 tasks.worker_key」窗口，按 R7 如需禁止此窗口的写入，还要验证执行者有效性与本地失效信号，并考虑登记行锁与认领、业务行锁的顺序，不能仅比较 tasks.worker_key。补测暂停在发现/围栏与认领之间的旧执行者。

### 公共路径与持久证据

以下代码路径以 `backend/app/services/` 为基准。

- `task_engine/execution_identity.py:83,126,146,155`。身份由领取事务冻结；将 worker_key 放入 matches_running_base 才能覆盖批量与发送门禁。advance_checkpoint、same_execution 使用 dataclass replace；构造函数另有测试 `backend/tests/test_engine_event_lock_order_e4ib2.py:102`。守卫 `collaboration_ai_guard.py:333,858` 使用同事务原 Task 锁。
- `task_engine/engine.py:1176,1319` 领取 CAS 与提交未确认的回读必须比对同一 worker_key。登记有效、任务尚无旧领取、清 recovered_at 等谓词需随领取提交。恢复必须在数据库确认认领后重读指定 worker 的 Task 快照，供后续事务核验。
- `engine.py:1512,1663` 普通事件调用 require_write_execution；单章 done/paused 使用专用 query，须补 owner。刷库原生 UPDATE 也绑定 owner（尤其无特化回调的分支）。`engine.py:420` 查持久富文档提案时须筛 owner，避免把任务重新读成另一个执行者的快照。
- `engine.py:2112,2187,2261,2346` 受限单章、分析、向导收据和批量断点终态都有自己的条件与统计事务，不能靠完整 matches_running。把原 owner 放入各自锁定、最终 UPDATE 和提交确认回查。`engine.py:2458,2571,2599,2713,2745` complete_state 同理，特别是单章/分析/系统清理本就允许跳过完整身份比较的路径。先前已合法改变输入、检查点或授权时，仍然必须保留精确的收据恢复语义。
- `engine.py:2769,2781,2809` 的成功回调、清 execution_key、失败提示按同一已提交成功轮次运行。若保留回调期内的 worker_key，可在成功凭据锁/条件更新中核验，回调完成后再清。若成功提交即清 owner，必须以其他持久证据区分普通成功与接管成功，避免失去资格的旧进程误派生自动分析/索引。不要给已提交成功用 require_write_execution（它要求 running）。
- `agent/service.py:147` 的过期外部调用扫描必须传 `engine=self` 并确认 `self.ctx.task_engine is self`，否则 stale_only=True 会直接返回 0。既测未过期调用原样保留，也测超时、无本地在途且身份可证的调用确实推进。当前期限检查与本地在途判定属于 C1，精确跨进程发起者归属由 C3 处理。
- `task_engine/engine.py:403,654` 与 `task_service.py:333`。待核对依赖进程内集合；只有在仍为本接管者的 running 行上写 recovered_at 并释放 owner 后，第二进程才可可靠展示。接管/待核对的任务没有本地执行体，不能加入 _running、_running_serial 或 admitted。已有资源保护、删除与管理员排空仍遵守各自约束，相关后续多进程改造属于 C3。

### 逐类写入核查

| Handler | 正常执行与完成 | 取消、错误或接管时需处理的独立写入 |
|---|---|---|
| chapter | `generation_service.py:1469,1490,1524,1553,1589` 正文、版本、日志、收据或富文档提案同事务，任务执行有 for_task 写屏障。 | 作者生成器 finally 在 `:1754` 先开会话还原占用，再在另一会话写失败日志。分别对 `chapter_claim.py:443` 锁住的 Task 及 `generation_log_service.py:144` 日志事务检查冻结 owner。日志检查不可依赖完整 input，前一事务已合法移除指针。协作者恢复 `generation_service.py:1391` 已比较 matches_running/base；外层 `handlers/chapter.py:422` 仍在独立会话释放，必须同样保护。成功后派生自动分析走 `analysis_service.py:943` 的原成功收据。 |
| batch | `batch_service.py:643,661,1431,1649,1690` 断点、章 attempt 和分析收据走批量专用锁；为 `_require_batch_round` 的 Task 条件加入 job 的冻结 owner，原 CAS 保留。嵌套生成沿用 chapter；嵌套分析 `:1549` 传入 job.execution_identity。 | `handlers/batch.py:323`、`batch_service.py:694,708,1249,1516` 有独立会话的占用释放，恢复与构建失败都可到达。将调用者的冻结 owner 传入实际释放事务；行号 1249 只是验证释放与同步内存，不能当作先前释放的写围栏。清理和终态重入见专项记录。 |
| analysis | `analysis_service.py:1298,1360,1502,1584,1603,1697,2243` 有独立 AnalysisExecution。单章与批量来源均携带原 task_identity，写 pending、分析/变更/记忆、收据前要在定制行锁内校验其 owner。批量另保留 `:1396` 的章节与分析凭据校验。 | handler.on_abort 为空，但模型失败、取消、生成器关闭和构建失败都会到 `:1642`，用不要求当前授权的 _system_receipt_task 清除此时原 pending。这个事务必须比较 owner。成功回调 `:1857` 只为原已证实的分析派生索引。 |
| wizard | `wizard_service.py:185,1547,1582,1695` 的 pending、作品替换与完成收据已有通用事务屏障。 | `:216,232,1778` 生成器关闭在独立事务清 pending；给 _lock_original_task 加冻结 owner。`:388` 恢复撤销 pending 需比较接管者 owner，并与随后中断状态原子衔接。handler 自身 on_abort 无业务写。 |
| agent | `handlers/agent.py:512,553,650` 构建/重试轮次绑定，`agent/runner.py:400,421,437,883` 消息、断点、工具效果与日志持有原执行锁。 | `handlers/agent.py:45,747,761` 撤销提案另开事务，应在现有 Task 锁比较 owner/原轮次，同时保留合法撤权后的系统清理。`agent/runner.py:255,292` 未发送或结果未知的自动外部调用独立收尾，通过 _task_conditions 对 Task 取锁，须比对 claim.job.execution.worker_key；失配交给过期恢复，勿重放外部调用。 |
| kb | `kb_index_service.py:1457,1551,1684,1830,1909` 文档状态、分块和向量写入经执行身份或受控 guard。 | `handlers/kb.py:117` 调 `kb_index_service.py:1949,486` 独立标记文档失败并清文档占用。原 EXISTS 仅比较文档/轮次；在锁住同一 Task 并校验 owner 后再更新文档，避免仅靠跨表 EXISTS 快照导致与认领错序。区分受理队列的合法取消路径。 |
| outline | `handlers/outline.py:130,192,211`、`outline_ai_service.py:670` 提供草稿。 | on_abort 无业务写；事件、公开草稿和受限失败均由引擎/guard 负责。 |
| entities | `handlers/entities.py:129,184,207`、`entity_ai_service.py:1395,1426` 验证来源后产生草稿。 | on_abort 无业务写；草稿 Task 终态用引擎路径。 |
| consistency | `consistency/check.py:577,652,755` 创建检查、台账和问题合并都有同事务 guard；`consistency/impact_task.py:381` 的台账/评估同样有守卫。 | `handlers/consistency.py:150` 的 close_run（`consistency/check.py:479`）只按 task_id + running 独立更新运行状态和统计。活执行者给 cancelled/failed，恢复者给 interrupted，先到即改变可见结果；在持有原 Task 行锁的事务内证明 task UUID、轮次、owner 后才更新 ConsistencyRun。impact 的 on_abort 为空。 |
| book_import | `book_import/reverse.py:627,640,693,1091,1186` 各步写摘要、设定、日志、子任务与断点前经 _lock_job/require_write_execution。 | `handlers/book_import.py:111` 保留已提交的业务数据，on_abort 没有后续写入。 |
| scene_restructure | `handlers/scene_restructure.py:80,112,136,150` 生成场景建议并返回草稿，经原身份守卫后发布。 | `:162` 的 on_abort 无业务写，实际应用建议由用户另外确认。 |

### 测试断点

1. 在原 worker 程序仍持有 Task、ChapterClaim、AnalysisPending、WizardPending 或 AgentJob 快照时，直接提交另一个 worker 的认领，再让原执行体尝试写。核验实际数据库中的字段、正文、日志行数与资源占用，不能仅断言返回异常。
2. 在认领后分开触发旧执行者的同章作者失败日志、批量 checkpoint/attempt、分析 pending 清除、向导 pending 清除、MCP 自动外部调用系统收尾、KB 占用释放、一致性运行结束。已有收据合法，但旧 worker 不得借收据完成本轮；接管 worker 应能按相同持久证据完成。
3. AC4 至少注入「接管者已认领」和「单章/向导清理后但终态前」的故障。若改用同事务，在提交前断开时两者均回滚；已提交则只认既有终态及统计。保留正常停机、撤权后清理、管理员取消和旧库 worker_key 为空等现有用例。
4. 模型计量按真实请求 attempt_id 记录，`ai_usage_recording.py:52,67` 在发送租约之外独立对账。它不写任务、正文或章节生成日志，不能因围栏丢弃已发出调用的费用事实。新的执行体业务请求仍必须过原任务发送守卫。

### 迁移与夹具影响（0.3）

`0026_admin_task_monitoring.py:16` 和 `0045_kb_index_identity.py:23` 已按原位加列保护 tasks 的子表。按 `0065_scene_moves.py:36` 的既有方式，降级先在 SQLite 取写锁，或在 PostgreSQL 锁 tasks、task_workers 两表，再查拒绝条件。`backend/tests/test_migrations.py:437` 的全链往返应继续通过，`test_setting_fields_migration.py:28,189` 的冻结结构不含 tasks，应维持其原契约。`tests/conftest.py:109`、`tests/schema_helpers.py:39` 按 ORM metadata 建表，新增无外键的登记表与两列自动覆盖。补充 0066 的结构、旧运行行、两种拒绝条件及版本与数据保持测试。具体依据另见同目录 `migration-fixture-impact.md`。

## Caveats / Not Found

本文件为修改前的内部源码依据，未运行压力脚本或测试。SQLAlchemy 与 Alembic 使用现有项目接口，不涉及查阅外部资料。相关规范为 `.trellis/spec/backend/database-guidelines.md` 后台任务表、`error-handling.md` 任务与 SSE 契约，以及 `quality-guidelines.md` 中的迁移冻结与 PostgreSQL 隔离约束。数据库锁等待、实际中断结果及全部 AC 以实现后的验证报告为准。
