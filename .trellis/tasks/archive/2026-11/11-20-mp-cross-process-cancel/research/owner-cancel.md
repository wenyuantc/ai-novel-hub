# 本人取消、处理器清理与执行者接管

- 调研问题：取消请求落在另一个进程时，谁有权停止任务，哪些业务结果须保留？
- 范围：仓库内的 HTTP 与服务入口、全部已注册处理器、C1 接管与 C2 名额。以下路径相对仓库根目录。
- 日期：2026-11-23，代码基线 `c593a20`。

## Findings

### 入口、身份和等待

`backend/app/api/v1/tasks.py:92` 的 `POST /api/v1/tasks/{task_id}/cancel` 接收原任务 UUID 和可选的审核身份。正文中的任务 UUID 与查询参数不符时返回 404。`backend/app/services/task_service.py:1012` 先读取本人任务，冻结作品、`queued_at` 与协作邀请身份，再在事务内验证原令牌和任务归属。作者、协作编辑和无作品的个人任务各有既有核验路径（同文件 `:827`、`:847`）。有审核身份时要在锁内复核原提案。返回前还要按原身份过滤私人字段并复核授权（`:388`）。删除进行中仅有原作者取得已提交取消结果的受限分支（`:1039`）。`backend/app/services/agent/service.py:1315` 在旧会话最后一项提案被处理后也调用本人取消。`task_service.py:1658` 的作品删除排空直接用共享的 `_cancel_loaded_task`，其受理身份是作品删除流程。

`task_service.py:1120` 先检查管理员意图，再处理 queued/paused/running。queued 的单章、批量与知识库文档有各自的条件更新及原资源归还，旧文档另有专用处理。paused 的富文档提案在同一事务丢弃，已释放的章节不再清理。running 分支 `:1303` 释放请求侧事务后，以本机 `_running` 是否存在来决定直接回退或发信号，远端正常执行时由此发生误判。分析和向导还会在进入该分支前因本机缺执行体返回 409。`backend/app/services/task_engine/engine.py:3191` 核对整数 ID、任务 UUID、入队轮次、执行 UUID。正在领取时先记取消信号，正在收尾时不打断。等待为 `cancel_wait_seconds`（默认 10 秒，`:262`），超过后本人接口返回当前 `TaskOut`。已有管理员意图的 `_wait_admin_cancel`（`task_service.py:1336`）目前等待本机 drain。

拟在原受理事务内只对原任务、原排队轮次、原执行 UUID 和当前状态登记取消。相同请求可以认领已提交的相同意图；锁等待、提交回执丢失以及客户端断开均不应让新轮次收到旧意图。HTTP 等待期间释放事务，每次用新数据库快照读取，复用同一个单调时钟截止点。执行进程按租约和本机原执行体身份领取信号并沿用 `cancel_running`。多个请求可等待同一轮结果，执行侧只发一次停止信号。后台调度每周期扫描，不在调度主循环里等待一个或多个 10 秒。暂停后继续会换 `queued_at`（`task_service.py:741`），重试会新建任务。清除意图和写终态均须受原轮次约束。C2 占用一直保留到原执行体最终退出。

### 已注册处理器的清理矩阵

下表文件前缀为 `backend/app/services/task_engine/handlers/`。登记入口为 `__init__.py` 和 `registry.py:93`。每类均仍需执行有身份保护的引擎终态与统计写入。

| 处理器与任务 | 清理及并发风险 |
| --- | --- |
| `chapter.py:422`，`chapter.generate/polish/revise` | 验证原章节 claim 与任务、发起人、执行键、worker 相符后调用 `release_chapter_claim`。原运行者仍可能提交正文或生成记录，远端不能并发释放。最终已有完成收据时沿用引擎的受限收尾。 |
| `batch.py:323`，`chapter.batch` | 运行者使用内存 job 上的原 attempt，只恢复当前章并同步断点；已认领的恢复者可从持久快照取 claim。已完成的章保留。后来的同一章占用不得被迟到的清理释放。 |
| `kb.py:117`，`kb.index` | 文档模式核验原任务和文档占用，释放原 claim；其他模式的 `on_abort` 无文档写入。见 `backend/app/services/kb_index_service.py:1974`。旧排队文档有 `task_service.py:1080` 的兼容证明。 |
| `consistency.py:150`，`consistency.check` | 只结束本轮仍在运行的检查。活 job 根据取消或失败写状态并保留统计，缺 job 的恢复路径使用 interrupted。重复清理可能抢先写错检查终态。已保存的问题和台账保留。 |
| `consistency.py:201`，`consistency.impact` | 不占章节、不维护活动检查记录。影响记录及已有台账保留。 |
| `agent.py:755`，`agent.turn` | 已执行效果保留。对协作轮次仅在授权已失效时按原身份作废待确认提案。正常本人取消保留有效提案。外部调用的未知结果须沿用独立恢复协议。 |
| `analysis.py:167`，`chapter.analyze`；`wizard.py:347`，`wizard.outline/characters/world/careers` | 两个 `on_abort` 都不写业务结果。分析结果、向导写入和收据由各自的保存事务及引擎收尾保护。只有活执行者能像本地一样被取消，已提交或不可证明的结果由收据判定。 |
| `book_import.py:111`，`book.import` | 清理无业务写入，已经导入的章节、项目、设定及断点保留。 |
| `outline.py:225`，`outline.continue/expand`；`entities.py:228`，`character.generate`、`organization.generate`、`career.generate`、`relations.parse/generate`；`scene_restructure.py:162`，`scene.restructure` | 都只产出草稿或建议，`on_abort` 无业务写入。仍要停止真正的模型请求，保留任务绑定和原有终态发布。 |

### 与管理员、收据和接管的先后

- 管理员 `backend/app/services/admin_task_service.py:188` 在事务里条件写原轮次意图并提交一次受理审计。管理员意图已提交时，本人挂接该操作并等候持久结果。本人先登记且尚未终结时，管理员仍可依现有受理条件登记，其操作绑定同一轮停止和清理。若本人已经完成，管理员沿用终态冲突；清理与统计各产生一次。管理员不新增第二个受理审计。真实执行已获得成功或暂停收据时保留原有裁决。本人等待端不替管理员清 marker。
- `backend/app/services/task_engine/workers.py:162` 的 `prepare_orphan_scan` 先让租约层确认停止或失联，SQLite 按配对租约库同步主库。`:185` 的 `adopt` 以任务 UUID、旧 worker 和执行 UUID 条件认领。`engine.py:561` 的短事务提交后验证归属，`:631` 的 `_recover_adopted` 消费收据，普通崩溃落 interrupted，分析或向导等证据不足时 `:609` 写 `recovered_at` 并清 worker。取消先于恢复终态提交时，接管者须识别**该轮已受理的取消**并按已有孤立取消语义与收据收尾；普通恢复已经终结时保持真实终态。应在取消与恢复竞争的条件提交中仲裁，不能整批调用 `recover()` 后覆盖它已写的结果。
- `engine.py:1699` 对保存结果不明的执行会保留业务凭据，现有 `_uncertain_executions` 也有仅在原进程登记的窗口。即使原 worker 仍活、C2 回调名额已经归还，也未必仍有可取消的执行体。必须区分正常运行、正在接管、已待核对与存活 worker 上已经退出但写入结果不明。后两者沿用原有 409 或失败屏障；在获得原凭据或确定收据前不得改写结果。`engine.py:2022` 起决定审核提案及章/分析/向导完成收据的归宿；本人与管理员意图均不能覆盖已经提交的业务效果。

### 验收时可强制的顺序

1. 两端使用独立应用、worker、连接池和真实 HTTP，单章任务在 A 进入模型屏障，再经 B 连续发两个本人取消。分别记录受理提交、A 单次信号、处理器退出、资源释放及终态。断言 B 的 `on_abort` 为零，且只有原章节 claim 被释放、生成记录和终态统计各一条。阻塞 A 的清理超过等待上限，使 B 返回当前状态后立刻重读，待释放后最终收尾。未提交的取消不得触发信号，B HTTP 断开后已受理的意图仍能执行。领取提交但本机尚在确认的窗口使用 `test_rbac_concurrency.py::test_cancel_in_claim_gap_defers_signal_until_safe_finalize` 的屏障。
2. 参数化分析与四种向导，比较本机和远端在活模型保存前、业务已提交但终态未写、无法证明保存结果时的响应、资源及统计。保留 `test_wizard_execution_identity.py::test_owner_can_cancel_before_save_and_pending_is_released`、`test_wizard_uncertain_cleanup.py`、`test_collaboration_analysis_execution.py` 和 `test_admin_uncertain_execution.py`。
3. 分别控制管理员/本人先提交、普通终态先提交和提交确认丢失。双端检查同轮只有一次实际停止、一个有效清理、管理员一次受理审计。改换同整数 ID 的任务、恢复旧提案、撤销协作者邀请或令牌后重新读，确认原请求不能改写新轮和泄露其私人投影。沿用 `test_collaboration_task_review.py::test_cancel_does_not_target_resumed_round`、`::test_running_cancel_binds_task_uuid_and_queued_round`、`test_admin_task_cancellation.py::test_late_winner_clears_marker_in_original_finalization`。
4. 先等真正的原执行者运行，再分别使 SQLite 侧租约失效和 PostgreSQL 租约被围栏，B/C 两个进程争接管。测试取消先提交、恢复先提交、接管者提交后崩溃、已持有收据与真正待核对。旧 A 恢复后不得写回。复用 `test_task_worker_lease.py::test_periodic_recovery_releases_chapter_and_rejects_late_worker`、`test_task_worker_recovery_crash.py` 与 `test_admin_chapter_claim_cancellation.py::test_admin_orphan_running_uses_original_execution_and_claim` 的故障停点。

## Caveats / Not Found

本次为源码与测试契约核对，未运行 C3 双进程验收。相关规范为 `.trellis/spec/backend/database-guidelines.md` 中的「后台任务」和 `.trellis/spec/backend/admin-task-monitoring.md` 中的管理员受理与排空。外部资料未参与上述项目语义判定。C4 的即时传输属于后续任务，C3 的投递依赖调度扫描。
