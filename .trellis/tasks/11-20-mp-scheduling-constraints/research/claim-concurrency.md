# Research: 跨进程领取、队首与执行者判活

- Query: C1 合入后，怎样在真正领取的事务里判定全局名额、用户限额和同键队首，并验证两个引擎真的发生过竞争？
- Scope: mixed，仓库源码、现行规范及数据库官方文档。本文是 C2 设计输入。
- Date: 2026-11-23

## Findings

### 源码与规范

| 位置 | 已有行为 |
| --- | --- |
| `backend/app/services/task_engine/engine.py:824` | 扫描排队行，按 id 升序，用本地运行表预留候选。遇到同键队首停滞或用户满额时，封住该键，本轮仍继续看其他键。 |
| `backend/app/services/task_engine/engine.py:1355` | 真领取从全新 session 先取进程内 runtime、数据库授权门，再锁用户、必要的可见性写门、项目、原排队 Task 及本执行者。确认输入、协作身份等后，在第 1441 行重新读取用户额度，目前仍以本地设置和 admitted 数判断。 |
| `backend/app/services/task_engine/engine.py:1467` | 原轮次条件更新写入 running、execution_key、worker_key，同一事务激活章／文档凭据并写统计。第 1505 行处理提交确认丢失，第 1545 行的 finally 覆盖 session 退出。 |
| `backend/app/services/authorization_service.py:105` | gate 持有本 session 所属 runtime，对授权单行作无实质变更的 UPDATE，直到根事务结束。绑定 RR/SERIALIZABLE 引擎且 session 尚无事务时，先取表锁避开等待中冻结的快照。第 130 行按升序锁 User。 |
| `backend/app/services/quota_service.py:28` | 限额三态，0 为禁止新领取，unlimited 仍受全局硬顶。第 88 行配额写入经同一 gate 和 actor/target 用户锁。 |
| `backend/app/services/task_engine/workers.py:65` | PG 用数据库墙钟和主库登记判活，本轮真实写入需在锁定 Task 之后锁自己的执行者。第 132 行以后是有副作用的围栏和接管流程。 |
| `backend/app/services/task_engine/sqlite_worker_leases.py:16` | 配对侧库已有只读／读写短事务包装，读前后核对物理文件与代次。第 57 行是权威活性条件，第 94 行示范按 500 键分批。 |
| `backend/app/services/task_engine/engine.py:561` | 恢复先扫描，再逐任务独立提交接管，最后另开事务收尾。确认待核对时第 609 行同时清归属、写 recovered_at。 |
| `backend/app/services/task_service.py:96` | 调度队列接受无项目任务或仍开放的项目。第 344、349、961 行的用户列表与单项位次均以全站已提交 queued 行按 id 计算；广播在 `engine.py:883` 查询后额外去除本进程候选。 |
| `backend/tests/conftest.py:138` | 两应用共享数据库、持独立连接池、运行时、深复制设置和执行者；对端引擎由测试启动。 |
| `.trellis/spec/backend/database-guidelines.md:553`、`.trellis/spec/backend/rbac-quotas.md`、`.trellis/spec/backend/system-settings.md`、`.trellis/spec/backend/quality-guidelines.md:228` | 租约权威、原领取 CAS、按 id 的同键队首、成功回调占用、设置提交确认和真实锁边界测试。父设计第 2.4 节明确接管者只做收尾、不算执行体，第 3 节的仅本地回调占用需按 C2 的跨进程合同修订。 |

### 正常领取与接管需要可判别的持久事实

`workers.adopt` 只把 `Task.worker_key` 换成接管者，保留 execution_key，未取得调度 gate（`workers.py:171`）。恢复事务可以暂时失败，健康接管者在下次扫描重试（`engine.py:573`、`backend/tests/test_task_worker_lease.py:205`）。因此只把 running 行与当前健康登记相连会发生下列情况。

1. 上限为 1。A 原先运行 X，A 已围栏，X 尚为 running。
2. B 领取 Y。X 的原执行者已失联，Y 使用空出的名额。
3. C 提交接管 X 而暂缓收尾，X 的 worker_key 现在指向健康的 C。简单联表会计为 2；同键也会显示有两项运行。C 没有启动 X 的 handler。

建议将正常领取的占用与原执行者绑定。可用独立的短期占用记录，同一数据库事务完成原轮次 running CAS 和创建占用，主键使用不可复用的 execution_key，记录原任务 UUID、入队轮次、执行者、user_id 和可空 serial_key。配额和串行键都从这份事实读取，接管不新建占用、不转移原占用；原执行者退出或确认失联后按原身份释放。这样可同时跨越成功回调、原 Task 可能被删或被同一个数值 ID 取代的窗口。正常回调、失败、取消、异常退出、待核对、崩溃及提交确认丢失的清理，须与回调调研合并定义。单纯把 `succeeded AND execution_key IS NOT NULL` 纳入计数仍依赖业务行保留执行键，数值 id 或本地 `_admitted` 也不足以识别一轮执行。`engine.py:2259`、`engine.py:3068` 和现有测试 `test_rbac_concurrency.py:330` 可作为回调窗口的观察点。

D5 是停机升级边界。迁移前无占用证明的旧 running 保留原有 C1 恢复，明确区分“旧数据尚待恢复”和“新版本由正常领取产生的占用”。从现存 Task 的健康归属直接反向补造正常执行记录会误认接管者。若方案允许已带租约的旧进程继续服务，必须另外为混版本定义兼容；父任务当前的停机升级约定已排除该部署方式。持久表生命周期若超过 7 天，还应考虑 `workers.py:241` 的已终止 worker 清理，确保原登记被裁剪后残留占用可确定地收敛。

### 门内协议

1. **发现候选。** 在独立只读会话内扫描当前候选，筛选条件与 `engine.py:831` 相同。批量读取占用、用户额度并作初筛，保持同键按 id 顺序。当本地已有正在尝试的候选时按轮次去重，本轮只预留尚未包含在持久占用中的候选，避免同一执行重复扣位。`_running`、`_claiming` 等继续服务取消、等待和会话退出。预筛结果只减少无用尝试。
2. **实际领取。** 关闭发现会话，用原有 `runtime.hold() → authz.gate(新 session) → actor/owner User 升序 → 已有发送／可见性与项目锁 → 指定 Task → 本执行者登记` 顺序。对所有候选复核 id、statistics_key、queued_at、actor、项目、kind、queued、空 execution_key、无管理取消、项目仍有效。PG 下 gate 是此 session 的第一项数据库操作；SQLite 先获取主库写权再读权威决定。把设置刷新或门外候选 SELECT 挪进该 session 并排在 gate 之前，会破坏已有 RR 特殊处理或导致 SQLite 读事务升级争用。
3. **先按门内最新配额裁决。** 与既有真正领取的位置相接（`engine.py:1441`）。在授权门内读取已提交的两个系统并发默认值与 `UserQuota`，数据库无覆盖时使用本应用捕获的部署基线。显式有限逐值比较，0 有独立含义，unlimited 只免用户级限制。此读取使用领取事务，避免从旧的设置缓存或已加载 ORM 行回读。不要在门内再调需要设置管理器私有锁的发布方法，保存顺序是“设置锁 → runtime → 数据库门”。前序事务调低额度后，已在候选队列里的任务也要拒绝；后序调低正常保留已经准入的执行。新上限小于已用数时，等待已用降到限额以下。
4. **在同一短事务判定两类阻挡。** 读取合格占用得到全站数、目标用户数和同 serial_key 是否已占用。对非 NULL 键再做普通 `EXISTS`，找同键 `older.id < candidate.id AND older.status='queued' AND older.execution_key IS NULL AND older.admin_cancel_operation_id IS NULL AND 项目对调度器开放`。与扫描共享结构资格谓词，显式相关到 older 别名。与执行能力相关的额度、用户状态、任务凭据能否解码、各进程的 `_stalled` 均不属于较早行的过滤项。这样即使后项来自不同用户且有额度，前面受限或停滞的队首仍然生效。已受理管理取消的旧项、已关闭项目或已离开 queued 的行依现行扫描语义让开。不同键或 NULL 键可独立前进。
5. **只产生一种占用新增途径。** 门内复核通过后保留第 1467 行的原条件 CAS 和原统计／业务凭据事务，失败整笔回滚。只有正常领取在持有 gate 时增加占用；释放只减少，接管只做收尾。通过这条不变量，无需锁住别的 Task、用户或 worker 来计算占用。保持提交确认丢失时只查原 UUID、原轮次、原执行键及原 worker，不重放二次领取。进程内 `_claiming` 一直覆盖 session 退出，确认提交且仍持有效身份后才能调用 handler。
6. **定时重试。** `notify()` 可立即清本地停滞，普通 `_on_done` 仅唤醒。将周期清理绑定到单调时钟的到期条件。`engine.py:794` 只在等待 TimeoutError 时清理，若持续的本地完成事件让 wait 立即返回，下一个定时刻仍可能在没有清退避的情况下推进。远端饱和时，预筛要看到远端占用，否则“每次预选 → 门内拒绝 → 本地完成再唤醒”会热循环。用跨进程已用量或仅在事实变化／到期后重试来限住尝试，不用永久跳过整把串行键。

PG 的同键前序查询使用普通 SELECT。它可以读到其他事务锁住但仍已提交的队首版本，判定队首存在就等待。使用 `SKIP LOCKED` 读取队首会主动滤掉持锁行而让后项越队；C1 的 `workers.py:137`、`workers.py:179` 仅用于尽力扫描过期登记、争取单个恢复对象，可以继续存在。若必须给当前候选取锁，仍只锁该已选 Task。判活、前序检查及 quota GET 均不加入对所有运行 Task／所有 worker 的锁定扫描。

### 判活读法与快照时间

- **PostgreSQL 16。** 用普通 SQL 将紧凑的占用行和原 worker 登记关联，在经过 gate 和目标必要锁之后读取。全局、同用户和同键三个结果来自同一份关系和同一个取样时刻，可用一次条件聚合；调度初筛只取 worker、user、serial 及去重所需身份。普通观察不调用 `lock_live_worker`；它为需要实际写资源的原执行者取得 `FOR KEY SHARE` 并登记事务结束校验。不要锁其他 worker，尤其不要为纯读增加 `FOR UPDATE`。使用数据库时钟，并在等锁后取得；默认 READ COMMITTED 下用一条后置 SELECT 建立新语句快照。RR 模式下用长事务早期的 heartbeat 快照与现在时间相比会把健康者误作过期，首次操作应当是既有 gate；若已长时间等待，模糊的过期观察继续保留占用，或放弃本次并以全新短事务重新判定。[PostgreSQL 16 事务隔离](https://www.postgresql.org/docs/16/transaction-iso.html) [PostgreSQL 16 日期时间函数](https://www.postgresql.org/docs/16/functions-datetime.html)
- **SQLite。** 主库仅作已开放登记／不可逆终止的佐证，主库 heartbeat_at 只反映注册；门内主库写事务固定占用和登记集合。在现有已验证的 `SQLiteLeasePair` 上增加批量观察方法，一次 `_transaction(pair, False, …)` 查询所需的不同 worker_key，条件与 `sqlite_worker_leases.py:57` 一致，以侧库数据库时钟判定是否在有效期。列表过长时按 500 键分批，每个批次使用同个侧库事务和同一已取得的数据库截止时刻，避免各批时间截点不同。先读主库中待判定的归属，再短读侧库后立即关闭；不持侧库锁去排队主库写门。读路径不打开新配对、不创建或修补文件、不尝试接管。若无占用，直接得到 0。占用观察只需少量标量，不能把 Task.input、output、checkpoint 或全部历史 Task 拉到 Python。[SQLite 隔离与回滚日志](https://www.sqlite.org/isolation.html)
- **只读异常要可区分。** 已停止或已围栏的权威原登记允许排除；有效心跳计入。已过期但未确认围栏、在途心跳尚未提交、主登记开放但侧库缺键、文件代次无法核验不能一律折成“空闲”。特别是读到旧心跳超时后，另一事务仍可能提交它在有效期内写入的心跳。准入可以为这些占用保留位置／阻挡相同键，待 C1 原有周期围栏或下一次有效观察后再裁决；不能凭瞬时缺证开放最后一个槽。回收无需等新用户有额度。配额 GET 使用只读的已确认观测；若缺行、代次或存储错误使真实数无法确定，沿用受控存储错误，不用数值 0 冒充成功。对于单纯过期但未落围栏的观察，设计应明确保守占位与展示的口径，并在两个进程同时覆盖。若采用仅依据**不可逆停止／围栏**清理原占用，页面可按仍被保留的实际准入名额显示，与成功回调占用统一，并在 C2 的 R1/R9 说明这一短暂过渡。
- **观察与恢复分开。** `prepare_orphan_scan` 会写 PG 围栏；SQLite 会先写侧库、再把停止／围栏同步回主库。`_pg_orphan_running_condition` 的取反也不等于“正常执行中”——它包含队列外状态且无法区分接管。读配额、读队列和普通调度统计均不能借它来清理旧数据。保持原自停和数据库提交前 guard，保留 SQLite 原先的 5 秒 busy 行为，存储故障按正常轮询重试。C1 原来可能必须等待的主库写门／候选自己的有限资源锁仍然存在；C2 的统计不应再为远端长模型请求等待额外行锁。
- **展示的采样。** PG 尽量用单 SELECT 返回 quota 和 used，避免多个语句各看到不同提交。SQLite 若由多条主库 SELECT 加上侧库观察组成一份精确结果，应使用短的真实主库读事务固定主库集合，然后在内部完成一次配对侧库读；已持同一真实写事务的路径可直接复用。Python sqlite 旧版事务语义下，仅有逻辑 Session.begin 不保证首条 SELECT 前实际 BEGIN，见 `backend/app/services/setting_category_query_service.py:108`。接口只比较同一稳定状态下的结果，两个请求跨过真实领取／释放时刻可产生正常差异。列表与单项沿用已提交 queued、id 升序、从 0 起算的现有展示口径，广播去掉 `engine.py:886` 的本地候选扣除；展示前序与实际可领取队首是两个不同问题。

### 可证明发生重叠的测试

| 用例 | 强制时序与独立证据 |
| --- | --- |
| 两个进程争不同候选的最后一个槽 | 复用 `peer_app_factory` 并保证双方分别注册、worker_key 和 pool/runtime 不同。通过真实入口排入足够任务，测试层在“候选已选、进入实际 gate 前”按引擎和任务身份暂停，不让两边只争同一行 CAS。预先放一个真实运行任务使上限 2 只剩 1。A、B 各选不同候选，A 拿到门后在自己的真实 commit 前暂停。等 B 已向独立连接发出授权行 UPDATE（驱动前钩子标记其 SQL），PG 可另用观察连接证明 B 正被 A 阻塞。再允许 A 提交，保持 A 的 handler 被事件阻住；断言 B 已完成真实门内判断且仍 queued。数持久占用、真实 handler 进入／退出事件和跨 A/B 的峰值，随后逐轮放行全部 6 项。替换 A/B 赢家再跑。 |
| 已提交、调用者尚未收到确认 | 在 A 的真实 commit 返回驱动后、让调用者继续前暂停。B 由自己的 runtime 进 gate，能够观察刚提交的占用并拒绝另一个任务。此时 A 的本地发布可以尚未完成。补提交前失败回滚使 B 能进入，以及确认丢失／提交已生效只允原 execution_key 且 handler 调用一次。旧 `test_rbac_concurrency.py:197` 只证明同一 runtime 阻塞，不能替代跨进程证据。 |
| 降配额的两种先后 | A、B 预选后、入门前暂停，管理请求在另一个应用提交用户 limit=0 或系统上限下降；唤醒方不能先刷新 B 的缓存来替代真实二检。B 确实完成门内拒绝后测回调零新增，调高后又能领取。同样让 A 先拿门并提交领取、管理更新后提交，原 handler 继续、下一项保持 queued。用分别到达门口的事件和提交回执排序。 |
| 串行与队首停滞 | 对同项目多个真实分析按 id 入队。故意让 B 的较大 id 先到门内（既覆盖调度入口又定向调用实际领取函数，使内侧检查独立于初筛），在较小项仍 queued 时确认 B 拒绝。A 的成功收尾/回调阻住时再由 B 尝试；结束后一次只允许下一项进入，按 handler 的任务身份记录完整进入与完成次序，时钟同秒不影响断言。再用两个有不同有效额度的 actor 排同键，较小项用户为 0／满额，后项用户有空额，另键正常运行。构造旧轮次缺原身份的队首，在 A 标记本地 stalled、B 本地无此标记时，B 的后项仍不能越队。加入已管理取消等结构性不再可领取的对照。 |
| PG 锁住队首 | 独立事务只锁较小队首 Task，无需改其内容。让 B 尝试较大 id，证实普通 EXISTS 能读到已提交队首并及时拒绝，不等待外部模型，也不因 SKIP LOCKED 越队；不同键可继续。运行顺序只有在观察到 B 阻挡／后续释放后才放开队首锁。[PostgreSQL 16 SELECT 锁定子句](https://www.postgresql.org/docs/16/sql-select.html) |
| 判活、未知与恢复 | SQLite 主库 heartbeat 调旧、侧库续约有效，PG 普通发送锁已取得；配额 GET 与纯占用观察仍能读到 1，续约可继续。SQLite 配对侧库确实停止但主库暂未传播、主库开放而侧库缺行／代次异常、只看到过期但 UPDATE 续约尚未提交分别测试。PG 在独立事务内做心跳更新并延迟 COMMIT，使观察者读到旧版本，证明不会腾出最后槽。用存储观察器检查查询没有调用围栏、接管或改写恢复标记。A 失联后 B 已领取新项，在 C 的 adopt 已提交但收尾暂停时断言不会重计、不会强杀 B；最后 C 写 recovered_at、清原归属，同键仍可继续。 |
| 定时和无热循环 | 让对端占满且持续保持 handler，驱动本地无关任务完成唤醒；计数同一不可进候选的真实门内尝试次数，再受控推进一次轮询时钟，验证本地不自激且定时确实重试。已提交 queued 位次用三个读取入口（A、B 列表，以及本地排队帧计算）比对，在 A 仅作本地预登记时结果保持相同。 |

锁屏障用有超时的 Event 并在 finally 无条件释放，所有需要真实重叠的用例断言双方都到过对应闸门。只 `gather` 两次调度、让双方只对第一条 CAS、固定 sleep 后看结果或按数据库整秒 started_at 排序，都不能证明不同候选曾竞争最后一个名额。SQLite 的锁争测试可在 A 持主库写门时用 B 的 driver-before-execute 标记，直到 B 的门内后置标记已出现并完成裁决再结束断言；PG 可在第三个只读连接用 `pg_blocking_pids` 核实实际等待，避免只证明 Python 代码到达事件。PG 沿用 `backend/tests/test_task_worker_lease_postgresql.py:32` 的专用本地库守卫、随机 schema 和 `-n 0`，SQLite 用默认回滚日志文件。保留 C1 的 `test_task_worker_slow_headers.py` 与 PG 第 61 行慢发送／续约锁兼容测试。

## Caveats / Not Found

本文仅审计了代码与协议，没有运行并发验收。现有 C1 字段缺少“正常执行者还是只接管收尾”的持久标志，也没有在 Task 行离开可见范围后证明回调仍实际占用名额的独立记录。建议的占用记录、三类判活结果和强制交错均为待实现设计。多个请求在不同提交时刻的读数不能要求绝对相等。主库全局写锁被其他业务占用时，SQLite 新领取仍可能等待原有的 busy 超时。
