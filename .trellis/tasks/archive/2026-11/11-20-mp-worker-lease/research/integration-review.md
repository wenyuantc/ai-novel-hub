# C1 集成审查与评审点 B

2026-11-23 第一轮评审对照子任务 PRD、设计、实施步骤和父任务引擎盘点第 10.1、10.2 节，检查了 0066、任务领取与接管、独立业务写事务、作品备份及既有测试的调整。下列 SQLite 数据对应心跳写在主库中的版本。

## 修复的回归

单章或批量章节清理失败时，任务可以先写入 `failed`、`cancelled` 或 `interrupted`，保留原执行键、原章节占用和本进程已捕获的清理快照。此后若同一应用重新登记了执行者，删除作品会重试这一份快照。原实现要求已停止的旧执行者仍在线，导致两类任务都无法释放原占用，作品持续返回 409。

`test_task_worker_handler_fencing.py::test_project_drain_retries_exact_terminal_claim_after_worker_stops` 在修复前分别复现单章、批量两种失败。修复后，项目排空把刚读到的准确终态交给章节释放事务，该事务锁住项目和任务，校验相同终态、原执行者、原执行键、章节占用以及管理员取消意图。普通执行体继续核验登记的存活状态。测试同时覆盖旧执行体自行释放和传入错误终态，均保留原占用。既有作品删除测试仍经历首次失败和第二次成功，并检查第二次传递的是原 `cancelled` 终态。

## 当次独立验证

命令均从 `backend/` 执行。PostgreSQL 使用本机专用 `ainovel_test` 库，迁移与应用用例分别建立随机 schema 并清理。

| 检查 | 结果 |
| --- | --- |
| `uv run ruff check .`、仓库根目录 `git diff --check` | 通过。 |
| 前端 `./node_modules/.bin/tsc -b --pretty false` | 通过。后端项目未配置专用静态类型检查命令。 |
| `tests/test_task_worker_handler_fencing.py`、`tests/test_task_abort_identity.py`、`tests/test_batch_chapter_claim.py` | 88 项通过。增加错误终态断言后，再跑对应单章、批量及旧占用被替换三项，全部通过。 |
| 回收站首次失败后重试、排队取消收尾失败、新一轮取消与 `tests/test_admin_task_check_regressions.py` | 7 项通过。 |
| `tests/test_project_transfer.py::test_round_trip_all_entities` 与回收站终态重试 | 2 项通过，作品 v16 导出使用 `app_schema=0066`，导入伪造的实例级登记和任务时不会恢复这些行。 |
| PostgreSQL 双回收者与向导释放后进程退出，两种数据库下的 `tests/test_task_worker_migration.py` | 4 项通过。迁移中途失败回滚、旧行及自引用保留、两类拒绝降级和空库往返均有断言。 |
| PostgreSQL 16 `uv run pytest -n 0 -q tests/test_task_worker_lease_postgresql.py` | 独立串行复跑全部 14 项，104.76 秒通过。包含健康执行者、定时接管、双回收者、单章与向导各五个提交故障点，以及发送锁与心跳共存。 |
| `sqlite_contention.py --tasks 32 --control --require-lease` | 进程 62295 和 62296 各完成 16 条、各刷正文 16 次，分别完成 4 次及 5 次正式心跳。任务全部唯一执行，正常写没有驱动错误。独立持锁对照 5,196.37 ms 返回 SQLITE_BUSY，释放后新事务成功。 |

第 6 步的既有用例逐项核对了变更所在测试函数。手工领取先登记，模拟拥有者崩溃时先停止旧执行者，故障注入保留原收据、正文、统计和占用的断言。原有批量提案续跑、管理员恢复排队、智能体外部操作身份替换和终态排空重试的补充断言已记录在 `test-adjustments.md`。全量 SQLite 回归的收集、跳过项和最终计数由主会话统一记入 `verification.md`。

## 剩余问题

SQLite 快模型与短事务压力可通过。实际出站钩子在等到模型响应头前持有主库写锁，慢请求会同时阻塞当前与其他 SQLite 执行者的心跳。32 秒对照中，SQLite 在约 25 秒处误中断且保留旧稿。PostgreSQL 等满 32 秒后正常写入完整回复。用户已确认独立 SQLite 租约文件方案，见 `sqlite-slow-provider.md`。双文件版本需再验证慢请求、进程失联接管与备份恢复等情形，并重新运行全量检查后评定 R11。README 的单进程部署限制继续适用。

## 第二轮评审点 B · SQLite 独立租约文件

2026-11-23 重新核对了实现与 R1–R13。SQLite 使用主库、独立租约数据库以及运行期间持有的生命周期锁文件。PostgreSQL 继续在主库维护登记和心跳。以下测试使用新的临时文件或专用 PostgreSQL 16 数据库的随机 schema。

### 评审中修复的两个窗口

- **同时首次启动。** 在 A 提交生命周期锁文件、尚未确认主库最终配对时暂停它，B 能在同一代完成配对。修复前，A 将 B 刚提交的有效配对判为损坏而启动失败。数据层修复后，A 会核对代次、主库及两个伴随文件的实际身份，再接受 B 已提交的相同结果。`test_concurrent_bootstrap_accepts_the_same_already_committed_binding` 固定了这个时序。命名共享内存库的两个独立引擎也验证了主库最终标记尚未写回时可以取得同一配对。
- **终态后的历史裁剪。** 在真实章节生成接口提交成功后、裁剪前围栏原执行者，复现了同一作者的另一条历史任务被旧执行者删除。原因是裁剪在后续事务中进行，终态提交时的身份核验已经结束。八处普通或按收据收尾的执行体入口现在均带入原任务。裁剪在将被涉及的作品上按固定顺序加锁，在同一主库事务内核对来源任务、执行轮次、结束状态及有效登记，随后才修改统计和历史。核验未通过会回滚。`test_fenced_finalizer_cannot_prune_previous_history` 通过真实接口分别检查成功、失败和凭完成收据收尾，三种状态均继续保留原历史。

### 独立复验

| 需求 | 证据 |
| --- | --- |
| R1–R5、R7–R9 | SQLite 的 `test_task_worker_lease.py`、`test_task_worker_lease_races.py`、`test_task_worker_recovery_crash.py` 共 28 项通过。覆盖持久登记、领取、换代、两个接管者竞争、单章与向导的事务故障位置、持久待核对和定期处理超过两分钟的外部调用。 |
| R1–R7、R12 | PostgreSQL 16 串行运行租约文件的 14 项，加 SQLite 与 PostgreSQL 迁移各一项，共 16 项通过，154.71 秒。迁移核对中断回滚、活动登记拒绝降级以及旧数据和空库往返。 |
| R6、R7、R10、R13 | 默认参数的 `sqlite_lease_processes.py` 使用两个真实进程。主库持写锁 65.00 秒，最初 35.21 秒中双方各提交 7 次续约。强杀运行任务的进程后，29.78 秒确认失联。主库释放后 0.21 秒内，受害任务为 interrupted，对方任务为 succeeded，清理仅一次。 |
| R10 | `sqlite_contention.py --tasks 32 --control --require-lease` 中两个进程各唯一执行 16 条、各提交 16 次正文刷库及 5 次心跳。主库正常写无驱动异常，10 次独立续约 P50 为 1.91 ms、最大 5.51 ms。故意持锁的对照在 5,172.13 ms 收到 SQLITE_BUSY，释放后新事务成功。 |
| R7、R11、R13 | SQLite 双库注册、分阶段崩溃、围栏传播、锁释放接管、原生快照恢复、迟到业务写及真实 SDK 慢响应头五个文件共 30 项通过。服务器收到请求后等待 32 秒才发响应头。期间两个执行者均按原身份续约，最终只收到一次请求，写入完整正文。 |
| R7、R11 | 配对、任务事件、权限与证据保留、任务统计共 60 项通过。另将三项失租裁剪及正常保留上限测试放在独立 PostgreSQL 16 schema 中串行运行，4 项通过。SQLite 对应四项在最终的显式回滚版本上再次通过。 |
| R13 冷恢复重试 | 在真实 0066 快照恢复中，于独立租约数据已提交、主库配对尚未完成处注入磁盘错误。立即重跑会拒绝覆盖。按 README 移存失败的整组文件，从同一原快照重试后得到独立的新代次，原快照 SHA-256 保持一致。 |

第 6 步的既有用例变更和新增租约夹具继续见 `test-adjustments.md`。本轮 `uv run ruff check .`、`git diff --check`、前端 `tsc -b` 均通过。后台全量 SQLite 回归由主会话执行，最终计数及 AC12 状态记在 `verification.md`。

成功提交后、派生回调尚未确认时失联，原 `execution_key` 会保留。原执行者无法清除它，现有恢复只接管 running 任务。此场景涉及如何证明成功回调已经完成，交由 C3 核对作品排空；当前没有重放未知的派生调用。
