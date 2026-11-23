# C1 执行者登记、租约与启动恢复不误伤：技术设计

> 细化父任务 `design.md` 第 2 节。需求见本目录 `prd.md`。第 9 节列出的研究项会在实现第一步完成，结论若改变本设计，先回写本文件与父任务设计再继续。

## 1. 范围与边界

改动集中在任务引擎的生命周期、领取和恢复，以及任务读取时的「结果待核对」展示。不改调度约束的计数方式（C2），不改取消、管理员 drain、作品删除排空里的孤立判断（C3），只向它们提供判定函数。

| 模块 | 改动 |
|---|---|
| `backend/app/models/entities.py` | 新增 `TaskWorker` 和 SQLite 配对记录 `TaskLeaseStore`；`Task` 增加 `worker_key`、`recovered_at` |
| `backend/alembic/versions/0066_task_workers.py` | 新表与两列；降级保护 |
| `backend/app/services/task_engine/workers.py`（新） | 执行者登记、心跳、自我围栏、存活与孤立判定、围栏与认领的条件更新 |
| `backend/app/services/task_engine/sqlite_lease_pair.py`、`sqlite_worker_leases.py` | 文件配对、共享生命周期锁、SQLite 权威租约短事务 |
| `backend/app/services/backups/sqlite_restore.py` | 从单文件原生快照冷恢复并建立新配对 |
| `backend/app/services/task_engine/engine.py` | `start`／`stop` 接入登记；调度循环加入周期接管；领取写入归属；`recover` 改为只处理已认领的孤立任务 |
| `backend/app/services/task_engine/execution_identity.py` | 执行身份携带并核验 `worker_key` |
| `backend/app/services/task_service.py` | `TaskOut` 的待核对展示读取持久标记 |
| `backend/app/services/agent/service.py` | 启动恢复不再使用 `stale_only=False` |

## 2. 数据结构

```
task_workers
  worker_key   String(36)  主键
  started_at   DateTime(tz) 非空，数据库时间
  heartbeat_at DateTime(tz) 非空，数据库时间
  stopped_at   DateTime(tz) 可空
  fenced_at    DateTime(tz) 可空
  hostname     String(255)  非空，仅诊断
  pid          Integer      非空，仅诊断
  peer_url     String(500)  可空，C4 使用，本子任务恒为 NULL

tasks
  + worker_key   String(36)   可空
  + recovered_at DateTime(tz) 可空
```

- 两列用普通 `ADD COLUMN` 添加，不建外键、不加 CHECK，避免 SQLite 重建 `tasks` 表。
- 不另建索引：运行中任务经现有 `ix_tasks_status_id` 定位，集合规模受并发上限约束。
- `heartbeat_at`、`fenced_at`、`stopped_at`、`recovered_at` 使用存储所在数据库的时钟。SQLite 主表只镜像登记、停止和围栏等事实。
- 登记行的清理：引擎启动时删除已停止或已围栏超过 7 天、且没有运行中任务引用的行。

### 降级

存在 `stopped_at IS NULL AND fenced_at IS NULL` 的登记行，或存在 `status='running' AND worker_key IS NOT NULL` 的任务时拒绝降级并说明原因：前者表示可能仍有进程在运行，后者表示有任务的执行者未被确认停止。正常停止全部进程后两者都不存在，可以降级；进程崩溃后需先启动一次当前版本完成接管再正常停止。降级时先取锁再检查，写法与 0052 等现有迁移一致。

## 3. 参数

### SQLite 租约与主库配对（2026-11-23 修订）

PostgreSQL 的执行者登记与心跳位于同一数据库。SQLite 的主库负责业务任务、原执行键、资源占用和终态；独立的 SQLite 文件保存权威心跳与不可逆的租约围栏。实际模型发送保留现有授权事务到响应头。独立短事务续约，不获取主库写锁，业务也不在等待模型时持有租约文件的事务。

两个文件分别提交，使用同一启动身份和文件配对标识衔接。只有完成双方登记的执行者可以领取任务。先在租约文件确认失联，再将围栏传播到主库并按原归属认领。后继进程可继续未完成的传播。每条旧执行写入仍在主库锁内按冻结身份核验，避免通过重读新归属授权给旧执行体。主库被其他事务占用时，业务接管在主库可写后继续，其他健康执行者仍能独立续约。

已获授权的业务事务在提交主库前还要检查本地原身份与安全期限。此时已知失租就回滚。已经交给驱动提交的事务，由主库写锁保证它排在接管之前。新事务须重新查权威租约。这样既保留已发生的提交，也不会让慢主库提交占用租约库的写入机会。

文件缺失、格式损坏或配对失效时，已有执行者必须停止使用旧身份。正常启动与确认所有旧进程已退出的冷恢复使用独立入口；恢复主库快照后轮换运行代次。`<主库>.leases.sqlite3` 保存 `worker_leases`，`<主库>.leases.lock` 上的 SQLite 共享读事务持续到引擎停止。主库 `task_lease_store` 的代次、物理文件身份与侧库、锁文件相匹配。恢复命令和回滚步骤见 README「SQLite 文件恢复」，详细验证见 `research/verification.md`。

引擎类属性，测试可改：

| 名称 | 默认 | 含义 |
|---|---|---|
| `heartbeat_interval` | 5 秒 | 心跳间隔 |
| `lease_seconds` | 30 秒 | 租约期限 |
| `lease_safety_seconds` | 5 秒 | 自我围栏的安全余量 |
| `poll_interval` | 5 秒 | 调度周期到期时同时扫描过期外部调用，兑现 R9 所要求的一个周期上界 |

SQLite 主库的登记时间精确到秒，权威租约使用侧库的毫秒时钟。测试可通过测试助手修改权威存储中的心跳，也用真实时间验证默认 30 秒租期。

两进程基线使用 SQLite 现有的 5 秒锁等待。心跳和接管事务遇忙后回滚，下一个周期用新事务重新检查归属。记录心跳时保留请求发起的单调时间，等待锁及提交确认所耗时间计入 25 秒的本地安全期限。基线与持锁校准数据见 `research/sqlite-contention.md`。

## 4. 执行者登记与心跳

`workers.py` 提供 `WorkerLease`，由引擎持有：

- `register()`：按本次 UUID 登记并返回 `worker_key`。SQLite 先确认侧库，再确认主库，同一 UUID 的两边登记已确认后才可领取。在 `start()` 中、`recover()` 之前调用。
- 心跳协程 `task-engine-heartbeat`：每个间隔开一个独立短事务。PostgreSQL 更新主库 `task_workers`，SQLite 更新租约库 `worker_leases`。只接受未停止、未被围栏且仍在有效期内的原登记。
  - 影响 1 行：按已获确认的请求发起时刻计算本地剩余有效期。
  - 影响 0 行：已被围栏，触发失去租约处理。
  - 数据库异常：记日志并在下个间隔重试，不抛出。
- `valid`：距上次成功心跳未超过 `lease_seconds − lease_safety_seconds`。调度循环在每次派发前检查，无效即触发失去租约处理。
- `stop()`：`UPDATE ... SET stopped_at = now()`；在引擎 `stop()` 中、本进程执行体和清理全部结束后调用。

### 失去租约处理

1. 置位 `_lease_lost`，调度循环不再派发，领取入口直接返回。
2. 标记本进程全部执行体「失去租约」并取消。失租与正常关停分开处理：已自我围栏的执行体退出，由持有效租约的接管者决定业务终态。迟到的清理、写日志、刷库和终态回调在各自写事务内核验原执行者，防止它们处理已转移的任务。
3. 等执行体退出后清空内存登记（`_running*`、`_admitted`、hub），用新的 `worker_key` 重新 `register()`，清除 `_lease_lost`，恢复调度。

## 5. 领取

`_claim_and_run` 的条件更新改为：

- `values(...)` 增加 `worker_key = :me`、`recovered_at = NULL`。
- `where(...)` 增加「本进程登记行仍有效」的存在性条件（`stopped_at IS NULL AND fenced_at IS NULL`），使已被围栏的进程在数据库层面无法领取。
- 领取前先检查 `WorkerLease.valid`。

`from_queued_task` 冻结本轮 `worker_key`，在 `TaskExecutionIdentity` 及 `matches_running_base` 比对归属，使多步任务推进断点后仍能校验同一执行者。`complete_state` 的锁内重读、最终条件更新，以及提交确认丢失后的核实，均使用调用方在领取或接管时持有的身份。接管沿用原 `execution_key` 识别业务收据。

独立事务按 `research/takeover-fencing.md` 逐处核对。章节生成失败日志、批量断点、分析和向导凭据清理、文档索引清理、一致性运行记录、智能体提案与收据收尾，均在实际修改所在的事务内比较预期归属。失租取消先检查本地失租信号，数据库写入同时核验登记仍有效；测试覆盖登记被围栏至任务归属改变之间的窗口。系统清理沿用既有撤权语义，只允许清理原资源。任务执行体携带原领取身份，接管者携带成功认领取得的身份；两者都不能从当前行反推自己的身份。成功提交后尚有派生回调时保留原 worker_key，回调和清除执行标记的事务仍须核验它；已发出模型请求的实际费用照常按原请求记账。

提交确认丢失后的核实循环（`engine.py:1319-1352`）同样比对 `worker_key`。

## 6. 存活、孤立与认领

`workers.prepare_orphan_scan(db, seconds)` 是启动、定时恢复及后续 C2、C3 复用的入口。它返回调用方可应用于 Task 查询的条件。

- PostgreSQL 使用数据库 `clock_timestamp()` 比较租约。围栏前尝试对过期登记取 `FOR UPDATE SKIP LOCKED`，主业务写入和正常心跳可并发。被在途事务锁住的登记在后续周期复查，认领时还要确认原登记确已失效。
- SQLite 先在配对租约库将过期登记持久围栏，再把已停止或已围栏的登记同步回主库。主库有归属但侧库缺记录时保留现状。只有无归属或原登记已确认终止的 running 任务可以进入认领，`recovered_at` 必须为空。
- `lock_live_worker(db, key)` 在实际业务写事务持主库锁时核对原执行者。SQLite 另核验对应代次的权威侧库；主库的登记时间无法证明原执行者是否仍在续约。
- `adopt(db, task_id, statistics_key, execution_key, old_worker_key, me)`：
  `UPDATE tasks SET worker_key = :me WHERE id = ? AND statistics_key = ? AND status = 'running' AND recovered_at IS NULL AND execution_key 等于原值 AND worker_key 等于原值`（NULL 用 `IS NULL` 比较）。影响 1 行才算认领成功。认领后原执行体的身份核验不再通过。

认领成功的任务登记在引擎的 `_adopted` 集合中，表示「归属本进程但没有执行体」，本进程的其他逻辑不把它当作正在执行。

## 7. 恢复与周期接管

`recover()` 重构为 `_reap_orphans()`，`start()` 调用一次，调度循环每个周期调用一次（先于管理员取消扫描与派发）：

1. 调用 `prepare_orphan_scan`，查询孤立任务的身份快照，提交主库。
2. 扫描本轮候选与先前已认领、等待重试的任务。即使这次没有新的孤立候选，也按期检查已过期外部调用。
3. 对每条孤立任务调用 `adopt`。失败（已被其他进程认领、已结束、执行者恢复心跳）则跳过。
4. 认领后按业务收据选择对应结果。对可能耗尽唯一凭据的两条恢复路径，先执行下面的事务规则，再处理其他分支：
   - 单章在恢复事务内复用 `release_chapter_claim(db, claim)`。向导在同一事务内复用 `release_uncommitted_pending(db, task)`。把该次资源修改、任务终态 CAS 和 `record_state_change` 交给同一个外层提交；函数返回未命中时回滚，并按原有的未证明路径处理。
   - 沿用作品 → 任务 → 业务资源的锁序，在事务内核对任务 UUID、入队轮次、执行键、接管者与管理员取消意图。释放后重新取本事务内的输入和断点供终态核验。终态 CAS 未命中或出现异常时，整个事务回滚。提交确认丢失时，重读原轮次的终态、资源和统计事实；已提交的结果不再清理或重复计数。
   - 已完成的业务收据继续走原有收据核验；凭据原本损坏或缺失的任务继续按既有规则保留屏障。详见 `research/recovery-reentry.md` 的对照实验。
   - 「保留 running 待核对」的分支（未能证明的分析、向导、单章与批量完成凭据）在保留的同时以条件更新写入 `recovered_at = now()`、`worker_key = NULL`，并继续做现有的内存登记。
   - 「让给管理员 drain」的分支保持不处理；任务已归属本进程，本进程的 drain 会按现有孤立分支接手。
5. `admin_cancellations.recovering` 只为认领成功的任务登记。
6. 任务处理完成后从 `_adopted` 移除。已经认领、遇到存储暂时失败的任务继续登记，并在下个周期按原接管身份重试；否则本进程的有效心跳会使其一直被排除在孤立查询之外。重新接管时，旧接管者的任何后续写入均须因归属不符而退出。

接管进程自己失联时，它认领的任务 `worker_key` 指向它，租约过期后重新成为孤立任务，由其他进程再次认领。这要求第 4 步的每个分支对「上一次做到一半」是安全的，见第 9 节研究项。

### 待核对展示

`engine.execution_is_uncertain(task)` 在现有内存判断之外，对 `task.recovered_at is not None` 返回真。`TaskOut` 因此在任一进程显示「结果待核对」。

### 外部调用

启动与周期调度均调用 `recover_external(db, stale_only=True, engine=self)`；该函数依赖当前引擎识别本地在途的调用。用一条未过期记录和一条可回收的过期记录分别验证保留和回收。按发起进程精确判定留给 C3。

## 8. 生命周期顺序

```
start():  initialize_tracking → 清理旧登记行 → register → 启动心跳协程
          → _reap_orphans → scan_admin_cancellations → 启动调度协程 → notify
stop():   停调度协程 → 中断本进程执行体、等 drain 与清理
          → 等在途心跳退出 → WorkerLease.stop() 写 stopped_at
```

登记后即开始心跳，使启动恢复耗时超过一个租约时仍可正常续约。启动中途失败须收回已启动的协程和自己的登记。心跳协程在 `stop()` 的最后才停。同一引擎对象 `stop()` 后再 `start()` 会得到新的 `worker_key`，上一个登记已标记停止，恢复不需要等待租约。

## 9. 研究项（实现第一步，结论写入 `research/`）

1. **围栏覆盖面**：逐个 handler 列出执行体在取消、失败、完成时的全部数据库写入，标出哪些经过 `require_write_execution` 或 `complete_state`，哪些只靠章节占用、文档占用或收据上的 `execution_key` 条件。对后者判断：任务被认领后，原执行体的这类写入与接管进程的 `on_abort`、收据收尾交错时结果是否唯一且一致。不一致的，在该写入上追加 `worker_key` 条件或改经执行身份核验。
2. **接管分支的可重入性**：`recover()` 每个分支在「做到一半后由另一个进程重做」时是否安全，重点是 `on_abort` 已执行但终态未写的情形。
3. **SQLite 双进程争用**：两个真实进程共享一个 SQLite 文件，并发运行领取、心跳、每 2 秒刷库与流式写入，记录锁等待分布与 `database is locked` 的出现位置。据此确定：是否需要显式设置统一的 busy 等待；心跳与接管循环遇到数据库忙时的重试；HTTP 请求遇到数据库忙时是否需要在全局错误处理中映射为固定的可重试提示。
4. **迁移冻结契约**：确认给 `tasks` 加列会触及哪些结构契约测试与快照（`.trellis/spec/backend/quality-guidelines.md:169-171`、`tests/test_migrations.py`），以及 `Base.metadata.create_all` 建表的测试夹具是否需要调整。

## 10. 兼容

- 升级前旧版本遗留的 running 行没有 `worker_key`，首次启动即被认领并按现有规则收尾，与现在的启动恢复结果相同。
- 单进程正常停止后重启：行为不变。
- 单进程崩溃后立即重启：遗留任务在旧登记的租约到期后才被收尾，主库可写时默认约 35 秒内收尾。若 SQLite 主库仍被其他事务锁住，恢复可写后继续接管。期间任务显示为运行中。
- 被认领后保留待核对的任务，`worker_key` 为空且 `recovered_at` 非空；它们不再被周期接管反复处理。重试、继续、取消对它们的现有判断（依据 `execution_key` 与状态）不变。
- 现有恢复类用例手工把任务写成 running 再启动引擎：这类行没有 `worker_key`，仍被立即认领，预计断言不需要改。直接断言 `recover()` 内部调用次数或读取 `recovering` 的用例按实际情况调整。

## 11. 测试

- `tests/conftest.py`：新增 `second_app` 夹具，用同一份设置（同一个数据库）构造第二个应用与引擎，默认不启动，由用例显式启停。
- `tests/worker_helpers.py`（新）：`expire_worker`（把登记行心跳改到过去）、`freeze_heartbeat`（暂停某个引擎的心跳协程）、`worker_row`。
- `tests/test_task_worker_lease.py`（新，SQLite）：对应 `prd.md` AC1–AC7。
- `tests/test_task_worker_lease_postgresql.py`（新，`AINOVEL_POSTGRES_TEST_URL` 启用）：对应 AC1–AC4。
- `tests/test_task_worker_migration.py`（新）：`0065 → 0066 → 0065` 往返、两种拒绝降级、旧 running 行升级后可被认领。
- 真实双进程的 SQLite 争用脚本放在本任务 `research/` 下，不进测试套件。
