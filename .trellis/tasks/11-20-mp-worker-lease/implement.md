# C1 执行者登记、租约与启动恢复不误伤：执行计划

> 需求见 `prd.md`，设计见 `design.md`。按步骤顺序执行；每步结束跑该步列出的测试，全量回归只在第 8 步跑一次。不提交 git，由主会话在收口时统一处理。

## 步骤

### 0. 研究（不改产品代码）

- [x] 0.1 已在 `research/takeover-fencing.md` 核对 11 个 handler 及公共写入路径；`research/recovery-reentry.md` 记录了单章及向导的真实数据库故障对照。
- [x] 0.2 在 0065 上完成 SQLite 双进程争用基线。32 条任务主样本使用模拟心跳，另有独立持锁校准，详见 `research/sqlite-contention.md`。第 7.3 步仍须用正式租约复跑。
- [x] 0.3 迁移与夹具影响见 `research/migration-fixture-impact.md` 与围栏审计末节。
- [x] **评审点 A**：单章与向导两组故障对照触发回退。2026-11-23 用户确认修订后的父子设计及 AC4、AC7，进入实现阶段。

### 1. 数据层

- [x] 1.1 `backend/app/models/entities.py`：新增 `TaskWorker`；`Task` 增加 `worker_key`、`recovered_at`。
- [x] 1.2 `backend/alembic/versions/0066_task_workers.py`：建表、加两列；降级先取锁再检查，存在未停止未围栏的登记或带 `worker_key` 的运行中任务时拒绝。
- [x] 1.3 `tests/test_task_worker_migration.py`：`0065 → 0066 → 0065` 往返；两种拒绝降级且拒绝后数据与版本号不变；空库全链往返仍通过。
- [x] 验证：`uv run pytest tests/test_task_worker_migration.py tests/test_migrations.py -q`

### 2. 执行者登记与心跳

- [x] 2.1 新增 `backend/app/services/task_engine/workers.py`：`WorkerLease`（登记、心跳协程、`valid`、停止）、存储内的截止时间与存活核验。
- [x] 2.2 `engine.py`：`start()`／`stop()` 按 `design.md` 第 8 节的顺序接入；新增类属性参数；启动时清理过期登记行。
- [x] 2.3 区分显式正常关停与失去租约；失租时阻止派发和旧执行体写入，退出本地执行体后以新身份登记。覆盖已围栏、任务仍指向旧执行者的间隙。
- [x] 2.4 `tests/conftest.py` 增加 `second_app` 夹具；新增 `tests/worker_helpers.py`。
- [x] 2.5 `tests/test_task_worker_lease.py` 第一组：启动即登记并心跳；正常停止写入停止时刻；同一引擎停止后再启动得到新身份；登记被围栏后引擎停止领取并以新身份重新登记（`prd.md` AC5）。
- [x] 验证：`uv run pytest tests/test_task_worker_lease.py tests/test_tasks.py -q`

### 3. 领取归属与进程级围栏

- [x] 3.1 `execution_identity.py`：`TaskExecutionIdentity` 与 `from_queued_task` 增加 `worker_key`；`matches_running` 比对它。
- [x] 3.2 `engine.py` `_claim_and_run`：领取更新写入 `worker_key`、清空 `recovered_at`，条件加入本进程登记仍有效；提交确认丢失后的核实循环同步比对。
- [x] 3.3 按 `research/takeover-fencing.md` 的逐类清单，在各实际写事务校验冻结的任务归属；验证迟到的生成失败日志、断点、收据、提案清理与成功派生均受约束，保留原撤权清理和已发生的费用记录。
- [x] 3.4 用例：领取后任务行带本进程身份；登记已围栏时领取条件更新影响 0 行；任务行的 `worker_key` 被改写后，原执行体的事件写入、刷库与终态写入全部失败关闭且库内正文不变。
- [x] 验证：`uv run pytest tests/test_task_worker_lease.py tests/test_task_execution_identity.py tests/test_tasks.py tests/test_chapter_claim_wiring.py tests/test_batch_chapter_claim.py -q`

### 4. 孤立判定与接管

- [x] 4.1 `workers.py`：`prepare_orphan_scan` 分存储确认失联，`adopt` 据原归属认领。
- [x] 4.2 `engine.py`：`recover()` 重构为 `_reap_orphans()`，启动调用一次、调度循环每周期调用；`_adopted` 集合；保留待核对的分支写入 `recovered_at` 并清空 `worker_key`；`recovering` 只登记认领成功的任务。
- [x] 4.3 `engine.execution_is_uncertain` 读取 `recovered_at`；确认 `task_service` 的 `TaskOut` 展示随之生效。
- [x] 4.4 用例对应 `prd.md` AC1–AC4、AC6：存活执行者的任务不被处理；租约过期后一个周期内收尾且原执行体写入失败关闭；两个引擎并发恢复只生效一次；接管者中途失联后由第三个引擎完成；待核对标记跨引擎可见且不被重复处理。
- [x] 4.5 在接管恢复路径，复用单章与向导已有的事务级清理函数。把资源释放、终态、统计放在同一提交内。认领者存活时，临时数据库错误由该认领者在后续周期重试；认领者失联后由新认领者重试。
- [x] 4.6 将研究中的单章与向导对照转成 AC4 回归。分别在认领后、资源修改后尚未提交、提交后丢失确认处注入故障。比对任务与资源状态、执行键、作品清理屏障、重试资格和统计；另验损坏凭据仍保留既有保护。第 7 步在 PostgreSQL 16 复验。
- [x] 验证：`uv run pytest tests/test_task_worker_lease.py tests/test_tasks.py tests/test_task_statistics.py tests/test_task_statistics_identity.py tests/test_task_abort_identity.py -q`

### 5. 外部调用恢复收紧

- [x] 5.1 启动和周期恢复传入 `stale_only=True, engine=self`；分别验证未过期调用仍在执行、已过期且有完整身份的调用进入失败。
- [x] 5.2 用例对应 `prd.md` AC7；更新 `tests/test_agent_mcp_skills.py::test_executing_external_failed_on_engine_restart` 的前置条件（调用需超过过期阈值或其所属进程已停止），断言含义不变。
- [x] 验证：`uv run pytest tests/test_agent_mcp_skills.py tests/test_agent_recovery_claim_identity.py tests/test_task_worker_lease.py -q`

### 6. 既有恢复类用例回归

- [x] 6.1 逐文件运行父任务引擎调研第 10.1、10.2 节列出的用例文件，记录失败项。
- [x] 6.2 只在前置布置上做必要调整，不改断言含义；每处改动记入 `research/test-adjustments.md`，写明原断言、调整内容与原因。
- [x] 验证：`uv run pytest tests/test_batch_generate.py tests/test_batch_chapter_claim.py tests/test_rich_document_proposals.py tests/test_chapter_claim_author_wiring.py tests/test_chapter_claim_wiring.py tests/test_collaboration_chapter_entry_e4f.py tests/test_collaboration_analysis_entry_e4g.py tests/test_collaboration_wizard_entry_e4g3c.py tests/test_wizard_uncertain_cleanup.py tests/test_admin_task_cancellation.py tests/test_admin_uncertain_execution.py tests/test_project_lifecycle.py tests/test_agent_runtime.py tests/test_book_import.py -q`

### 7. PostgreSQL 与真实双进程

- [x] 7.1 `tests/test_task_worker_lease_postgresql.py`：沿用 `tests/test_collaboration_audit_query_postgresql.py:17-47` 的守卫与随机 schema 方式，覆盖 `prd.md` AC1–AC4。
- [x] 7.2 准备隔离的 PostgreSQL 16 容器与 `ainovel_test` 库，串行运行 7.1；在该库上实际执行 `0066` 的升级、拒绝降级与空库往返。容器启动命令记入 `research/verification.md`。
- [x] 7.3 重跑 0.2 的 SQLite 双进程脚本，确认最终实现下没有未处理的数据库忙异常（`prd.md` AC9）。
- [x] **评审点 B**：7.2、7.3 的结果与第 6 步的用例调整清单交由检查代理独立复核。

### 7a. SQLite 慢响应与独立租约文件（用户已批准）

- [x] 完成双文件配对与两阶段登记、续约、围栏、主库认领；所有权检查兼容停机恢复。
- [x] 验证双进程正常运行期间一个慢请求持有主库锁超过租约，两个执行者仍能续约；真实 HTTP 请求慢响应也应正常完成。
- [x] 覆盖两次提交间崩溃、文件缺失/替换、旧进程迟到写入、从原生 SQLite 备份冷恢复及重新启动。
- [x] 以最终布局复跑真实 SQLite 压力、PostgreSQL 串行验收、后端 Ruff 与全量 SQLite。单文件版全量在用户确认新布局后主动中止，已通过 2,110 项，仅作阶段记录。

### 8. 规范与收口

- [x] 8.1 更新 `.trellis/spec/backend/database-guidelines.md`「后台任务表」（表结构、租约与认领契约、必需测试）、`error-handling.md` 第 6 节重启部分、`admin-task-monitoring.md` 中启动遗留的含义、`directory-structure.md` 的模块说明。写明：多进程支持尚未完成，部署前提在父任务收口前不变；单进程崩溃后立即重启的收尾延迟。
- [x] 8.2 `cd backend && uv run ruff check .`
- [x] 8.3 `cd backend && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20`
- [x] 8.4 `research/verification.md`：逐条对应 `prd.md` AC1–AC13 的证据、用例数、跳过项、未覆盖情形。
- [x] 8.5 父任务 `implement.md` 勾选 C1；若实现偏离父任务 `design.md` 第 2 节，回写父任务设计。

## 风险文件与回退点

- `engine.py` 的 `recover`、`_claim_and_run`、`start`、`stop` 与 `execution_identity.py` 的 `matches_running` 是全部任务类型的公共路径。第 3 步合入前后各跑一次第 3 步的验证文件作对照。
- 第 1 步可单独回退（迁移往返已验证）。第 2 步只增加登记与心跳，不改变恢复行为，可单独保留。第 3、4 步需要一起生效：只做第 3 步不做第 4 步时，旧版本遗留的 running 行仍按原 `recover()` 处理，行为与现在一致，可以作为中间停靠点。
- 第 0 步的结论若显示某个 handler 无法在不改其收尾结构的情况下做到围栏，停下来回到规划，不在本子任务内硬改。

## 完成定义

`prd.md` 的 AC1–AC13 全部有证据；规范已更新；父任务清单已勾选；README 的单进程部署前提继续有效。
