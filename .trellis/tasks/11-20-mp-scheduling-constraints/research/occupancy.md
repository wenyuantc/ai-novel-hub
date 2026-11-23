# Research: 多进程任务领取、配额与串行键占用

- Query: 全局名额、用户配额、串行键究竟从何时占到何时，两种数据库怎样维持单进程已有行为？
- Scope: internal，C2 的领取、收尾、恢复和展示。只读当前源码与测试。
- Date: 2026-11-23

## Findings

### 占用与数据库状态的对应关系

调度有两道检查。第一道按 ID 扫描并启动候选协程，使用本地 `_running`、`_running_users` 和 `_running_serial` 做预算。第二道在数据库授权门内重新检查额度，提交真实领取后才进入 `_admitted`。旧配额页读取后者。执行协程结束时 `_on_done` 同步释放这四份记录。见 `backend/app/services/task_engine/engine.py:824`、`:900`、`:927`、`:1441`、`:1505`、`:1540` 及 `backend/app/services/quota_service.py:60`。

| 现场 | 全局和该用户实际名额、配额页已用 | 串行键及队列行为 |
| --- | --- | --- |
| 入队或候选已登记，领取尚未物理提交 | 0。本轮预筛会扣本地候选预算，其他进程可竞争同一候选。 | 同键的较小可调度 ID 仍挡住后项。HTTP 全站位次仍含该 queued 行。 |
| driver 已提交 running，领取方尚未收到返回 | 旧内存暂为 0，提交返回、会话退出前变为 1。其他本地领取无法穿过仍持有的 runtime。C2 应以已提交占用为准，两个进程都读 1。 | 同键互斥已经成立。`_claiming` 是活游标的取消保护，贯穿 commit 返回及会话退出，不得用其判断数据库是否已提交。 |
| 原执行体有效、running，包括运行中取消意图与执行中的 on_abort | 1 | 有键就占。降额只阻止新领取，现有任务照常收尾。 |
| 已提交 succeeded，原执行体正在成功回调、写派生警告、清执行键、最终裁剪或发送终态帧 | 1，直至退出。无 hook 的任务也有短暂的裁剪和终态处理。 | 有键就继续占。原章生成成功回调入队分析，分析成功回调入队索引。子任务按自身 actor 和键参与排队，只有其真实领取才另占 1。 |
| 已提交 paused、failed、cancelled、interrupted 或按收据成功，原执行体仍在裁剪或关闭 hub | 1，直至退出。 | 有键就继续占。暂停供用户长期审核时通常原执行体早已退出，此时两项为 0。 |
| 执行体已退出，仍留 failed/cancelled 等业务屏障，或 running 留给核对 | 0。提交未知、身份无法证明可正常 return 留下 running/有效 worker/`recovered_at=NULL`，也可能异常退出后入 `_adopted`。 | 释放。保留原执行键、收据、章节占用、重试限制是业务义务。 |
| C1 外部接管完成归属交换，接管者登记健康、尚在清理；或独立管理员 drain | 0。这些协程没有走原任务的调度准入。 | 无调度占用；C1 的资源归属及写围栏仍有效。独立 drain 按赢家补调 `_on_success` 本身也不会再占 1。 |
| C1 `recovered_at` 非空且原归属已清，旧 running 没有归属，或原执行者已由 C1 确认停止/围栏 | 0 | 不挡同键和异键。只有仍为可调度 queued 的前序行能挡后项。 |

前述行为可定位到以下路径。

- `backend/tests/test_rbac_concurrency.py:197` 固定了候选阶段、提交返回窗口及降额；`:292` 明确要求成功回调被阻塞时配额页显示 1；`:479` 固定了两个领取窗口内归属与取消的先后。GET 配额经 `backend/app/api/v1/admin_quotas.py:15`、`backend/app/core/deps.py:133` 只读鉴权，因此旧配额 GET 并不天然等待写 runtime。跨进程统一在物理提交后读到 1 是有意修正的可见性差异。
- `backend/app/services/task_engine/engine.py:2113`、`:2259`、`:2266`、`:2286`、`:3068`、`:3117` 记录了裁剪、成功 hook、警告、清执行键与关 hub 的顺序。清键后仍有 await，按 `succeeded AND execution_key IS NOT NULL` 会提前放行。收据兜底如 `:2392`、`:2476` 可能直接清键，原执行体仍要返回。章节派生和分析派生分别见 `backend/app/services/task_engine/handlers/chapter.py:447`、`backend/app/services/task_engine/handlers/analysis.py:171`。已有 `backend/tests/test_tasks.py:872` 检查派生异常仍成功且只调用一次，失败任务跳过成功 hook。
- `backend/app/services/task_engine/engine.py:1661`、`:1690`、`:1970` 有返回而保留业务证据的分支；`:903` 在执行体异常退出时登记本地后续恢复。C1 归档 `.trellis/tasks/archive/2026-11/11-20-mp-worker-lease/design.md:115` 明确接管者没有执行体。实际 `workers.py:171` 只改 Task.worker_key，`engine.py:568` 至 `:599` 独立重试收尾，`:609` 至 `:625` 同事务置 `recovered_at` 并清归属。待核对跨引擎及同键新任务见 `backend/tests/test_task_worker_lease.py:323`，接管中使用新归属和原执行键见 `backend/tests/test_task_worker_recovery_crash.py:131`。
- `backend/app/services/task_engine/engine.py:2982` 说明收尾失败会保留 execution_key。`backend/app/services/task_service.py:1185` 的排队取消会把清理用 UUID 写入 queued 行。`backend/app/services/task_engine/cancellation.py:244` 也把它用于取消收据。业务清理重试见 `backend/app/services/task_service.py:1504`，失败、取消、中断的任务重试会创建新的任务行，见 `:668`、`:698`。暂停后继续则按同一个整数 ID、同一 statistics_key 改 queued_at 并清 worker_key，见 `:739`。会话内的 `task_engine/admission.py:83`、`:102`、`:213` 是入队/重试模型证明，不建立调度占用。
- `backend/app/services/task_engine/cancellation.py:298` 至 `:336` 仍依赖本地 `_claiming/_finalizing/_admitted` 防止与活游标并行 abort。C2 可以保留局部标记履行取消职责；独立 drain 的成功回调入口还包括 `:338` 至 `:356`。对这些路径的跨进程取消协议是 C3。

### 同键队首与排队位次

领取事务取候选锁后再次证明「无同键活占用且没有更小的同键可调度 queued ID」。队首的 SQL 范围与扫描一致：`status='queued'`、`execution_key IS NULL`、`admin_cancel_operation_id IS NULL`，项目为空或项目存在且未开始删除/未删除。保留不能证明执行身份、尚未具备 handler 能力、actor 限额为 0、已达限额等前序行。排队清理已持执行键、已有管理员取消意图、作品处于删除过程的行不在该 SQL 集合。参见 `engine.py:830`、`:850`、`:1403`、`:1429`、`:1452` 及 `task_service.py:96`。按 ID 的规则也适用于恢复为 queued 的旧 ID。父设计第 3 节的「可领取」在这里指进入候选扫描的资格。

HTTP 详情按全站较小的所有 queued ID 计数，列表从同样的全站队列取有序 ID，按零起点显示。它包含其他用户与暂不能调度的 queued 记录，用户能看到哪些内容仍由原权限检查决定。`task_service.py:344`、`:349`、`:942`、`:961` 和 `backend/tests/test_tasks.py:437`、`:463` 可定位。SSE 广播另在 `engine.py:885` 剔除本地 `_running`，导致 A 已选候选而 B 尚未选时的位次不同。广播应读相同队列并保留 hub 轮次及帧核验，C2 不需要增加跨进程唤醒。

### 最小可行持久方案

四项约束可共用一份原执行轮次占用。没有新的持久事实时，一条健康 worker 所属的 running 行可代表执行中，也可代表正常退出但结果未知或接管中；同样，正常回调已清键后还能继续收尾。单靠现有列及本地计数无法从另一进程分辨。若把标记加在 Task 上，还要处理原地续跑、清键后的物理删除及整数 ID 复用。`backend/app/models/entities.py:843` 明确实际身份是 UUID；关闭任务和历史裁剪可在清键后移除行，见 `task_service.py:1382`、`:1444`、`:1462`。把删除改为等待所有收尾，会扩大对现有 API 的影响。

建议新增一张实例运行期占用表，Task 原有字段继续用于业务与恢复。每次真正领取插一行，保存原 `execution_key`（主键）、`worker_key`、`task_id`、`statistics_key`、`queued_at`、实际 `user_id` 和可空 `serial_key`。以 claim 时的不可变值记录，避免读取可能已经续跑或消失的 Task 来归还旧名额。worker 可外键级联到已安全裁剪的 worker 登记；占用生命周期独立于 Task、项目和用户的物理删除。对于旧占用尚在而同 ID 已续跑的情况，在领取门内按该候选的 task_id 检查有存活的旧占用；归还始终带原轮身份，即使 SQLite 复用整数 ID 也不能删到后来的轮次。串行键可以在历史复用，在全局门内按有效占用查冲突即可。表不进入项目备份、对外 API 或通用清理令牌。

1. **领取。** 使用原 `runtime.hold`、`authz.gate` 和 User/项目/Task/本执行者的锁序，读取当前全局和每用户有效限额；在门内以同一个已提交占用集合判全站数、actor 数、串行键及同 ID 原执行体，并做上述队首检查。0 禁止新领取，unlimited 仍受全局顶限制。将占用 INSERT 与 queued→running CAS、章节/文档活动绑定、TaskStatistic 放在同一个主库事务。占用行和 Task 均提交后才允许实际执行，CAS 不成功、业务检查回滚或提交未落定时不会产生半份事实。沿用 `engine.py:1467`、`:1485`、`:1505` 的确认分支；提交回执丢失时以原任务 UUID、入队轮次、执行 UUID 和原 worker 查占用与 Task，连同原业务绑定一起核实。
2. **候选与调度。** 本地预筛只限制进行中的领取尝试，并保护活游标。若同时读取全站占用作预筛，须按原执行 UUID 去重本机已提交份额，不把 `_running_users` 或 `_admitted` 整体叠加。远程满额或占键被门内驳回时，下一次预筛应能看到已占份额，或按该候选轮次退避到外部通知/轮询。否则 `_on_done` 自唤醒可能形成空转；相关哨兵见 `backend/tests/test_kb_legacy_document_cleanup.py:342`。
3. **正常与异常退出。** 原执行体在 `_run`、成功或失败回调、最后裁剪与终态发布全部结束后，在最外层异步退出阶段归还原占用，然后唤醒本机。如回调清键失败但执行体已退出，单独释放调度占用，保留原业务键。该路径须覆盖抛异常、取消和早返回；同步 `_on_done` 无法直接 await 数据库。可在每轮领取/执行的最外层 `finally` 完成精确 DELETE，覆盖直接调用 `_claim_and_run` 的测试与运行路径。处理真实 driver 提交后、调用尚未返回的异常时，要保留最初的执行凭据。对未完成的 DELETE 按同一令牌重试，提交确认未知时只重读本令牌；存储暂错仍保留占用和本机待清记录，后续周期重试。单次清理失败不能令健康 worker 所有后续任务永久无名额。
4. **崩溃与接管。** 只有 C1 已确认的 stopped/fenced 才解除丢失进程的占用。可在主库快照联结 `task_workers` 的开放登记决定有效占用，并按确认关闭的 worker 批量删除残行；旧 worker 已被围栏时也不会因迟到归还删除新 worker 的轮次。认领孤立任务时不插占用，不转让旧占用。由本机异常体入 `_adopted` 的场景也须归还已退出原体。待核对标记与清归属写入后，若有同轮遗留占用应在该恢复事务中一并排除/清理。崩溃落在成功回调时，旧占用按 worker 生命周期释放，只释放调度名额，不自动重播派生、不擦业务执行凭据。冷升级先停所有旧进程，迁移可从空表启动；旧 running 延用 C1 接管，给所有旧 running 全部回填会造出虚假执行体。反向回滚前要排空有效占用，检查旧版迁移/恢复/备份验收。
5. **两种数据库。** 准入判定与新占用都在主库，同次 `authz.gate` 是串行化点。`authorization_service.py:105` 对 PG/SQLite 共同更新授权门行；PG 特殊隔离级别需保证该门是首条语句，避免在等待门前冻结旧快照。PG 沿用 `workers.py:120` 的本执行者共享行锁、`:132` 过期围栏和 `:43` 的提交前校验；跳过仍有活事务锁的过期行，等 C1 能确认再按已关闭登记放行。SQLite 只在既有配对侧库判断心跳/确认终止，`workers.py:150` 将已确认结论同步进主库；`sqlite_worker_leases.py:16`、`:61`、`:85`、`:94` 说明独立短事务的边界。调度不尝试跨两份库原子提交或写独立侧库名额，也不能按主库登记心跳陈旧、侧库缺行得出死亡结论。健康时不需要为了统计而锁住所有其他执行者。扫描滞后或 SQLite 主库写锁未释放时，旧占用可短时保守保留，和 C1 失联判定共用时钟及边界。
6. **读取。** 配额 GET 和更新后回包使用同一有效占用集合，尽量在现有权限/用户快照查询中加相关 COUNT，避免每个角色 join 重复加一。不得按任务当前可见性、作者、Task.status 额外过滤。AC9 应表述为「该用户已实际领取且仍有效的原执行轮次数」，包括成功回调内、原执行体仍在收尾的终态任务。配额历史 `_admitted` 仅留给 C3 的本地防重入路径。

### 验收与旧用例调整

| 旧测试 | 保留观察与调整理由 |
| --- | --- |
| `test_rbac_concurrency.py:229,264,274` | 领取前两个候选已选但已用为 0；第一笔物理提交后 A/B 可读 1，第二候选仍 queued 且模型只调用一次。原 264 行单独观察 Python 字典落后于 commit，跨进程共识以提交为界。 |
| `test_rbac_concurrency.py:331,339` | 故意阻塞成功回调，A/B 配额均为 1、B 的同用户后项不能执行；释放后第二个任务达到模型闸门。另以宽松全局/用户上限验证同键本身，再以不同用户/不同键与全局 1 验全局阻挡。 |
| `test_rbac_concurrency.py:441,448,535,560` | 保留未确认设置阻止领取、确认后只执行一次。commit 返回与 session 退出两个窗口的 `_claiming` 保护和取消/关停结果继续验证；两窗口已提交的占用均为 1，最后为 0。 |
| `test_user_quotas.py:146,153,181,185,228,239` | 用配额接口和模型闸门验证 0、2、全局硬顶 1、自定义覆盖默认与恢复继承 3。降到 0 后原任务仍继续，排队后项等待。 |
| `test_tasks.py:769,816,840,859` | 保留顺序、异键并行和重启后原键再用。859 的本地 map 是否为空改验已归还的持久占用、同键新领取与重试，再保留 restart 路径。 |
| `test_task_worker_lease.py:349` | 两端均读无占用且同键新任务照常完成，还保留 recovered_at 与 worker_key 断言。 |
| `test_admin_chapter_claim_cancellation.py:361,369` | 此处人为写 `_admitted` 证明本机独立 drain 会等待执行游标；若保留本地兼容标记，原测试继续。它不证明对端占用，不宜删除其取消保护断言。 |

补充注入窗口应至少覆盖：提交未落地/已落地但确认丢失，不会多占或漏算；暂停后原地重入队但旧终态帧卡住，另一进程不能提前再领同一任务；失败/取消生成新重试行，旧收尾仅释放旧轮次；成功派生失败仍成功并在退出后释放；回调清键后又阻塞在裁剪/终态证据时全局/用户/键仍占；成功回调途中原 worker 被围栏、接管临时失败和原体异常出栈三种现场均能按生命周期放行，同一业务回调不因归还失败重复执行。用独立 session 双引擎、显式事件和原业务数据验证，记录模型实际启动次序与占用数。

## Caveats / Not Found

- 本调研核对当前工作区源码、C1 归档和测试定义，没有执行 C2 实现或两方言实测。架构选择为规划建议；新迁移需在实施时按当时 head 排号。
- 父设计第 3 节提出把本地已准入量与 running 合并；上述回调、接管和正常早退反例表明需要一份任一进程可见的原执行占用。原 PRD 的 R1、R2、AC9 使用「有存活执行者的运行中任务」作为简写，R6、R10 及现有成功回调用例对该定义做了实质补充。单进程旧提交回执窗口的配额数和 SSE 中排除本地候选的位次需要明确记录变化。
- 相关规范为 `.trellis/spec/backend/rbac-quotas.md:62`、`:90`、`:96` 与 `.trellis/spec/backend/database-guidelines.md:553`、`:559`、`:564`。本项目锁定 SQLAlchemy 2.1.6、aiosqlite 0.22.1、asyncpg 0.32.0，见 `backend/uv.lock:85`、`:158`、`:1240`。本调研使用项目实际代码与内部规格。
