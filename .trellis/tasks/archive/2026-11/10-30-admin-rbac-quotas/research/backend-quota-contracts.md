# Research: 用户作品数与后台任务并发配额

- Query: 找全真实作品创建与任务领取路径，为已确认的两项用户配额定义最小、原子、可验证的接入方案。
- Scope: internal；仅代码与既有规范调研，不修改产品代码，不激活任务。
- Date: 2026-11-01

## Findings

### 1. 现有数据和默认来源

- `backend/app/models/entities.py:141-152`：User 当前没有配额字段；不声明 ORM relationship 的项目约定见同文件 `:1-4`。
- `backend/app/models/entities.py:175-197`：Project 以 `user_id` 归属用户，外键为 RESTRICT；正常、软删除与删除中都仍占一行，作品数量应直接 `COUNT(Project.id) WHERE user_id=:owner`，不能复用正常书架生命周期过滤。
- `backend/app/services/admin_service.py:33-55` 已有全生命周期作品总数统计，`:79-125` 是三组资源投影。可复用计数口径，不应用分页小计，也不重复定义“全部作品”的过滤条件。
- `backend/app/core/config.py:43-45`：`task_max_concurrency` 代码默认 4，`task_max_per_user` 代码默认 2，两者部署值与后台持久覆盖均可能不同；不能把代码默认 2 当成当前实例有效值。
- `backend/app/schemas/system_settings.py:8-31`：系统设置只有六个白名单键、bool/正整数值；PATCH 中 `null` 表示移除持久覆盖。当前没有作品默认上限设置，也没有可以直接复用的 nullable 配额值结构。
- `backend/app/services/system_settings_service.py:32-61`：运行时始终发布到同一 Settings 对象；`:118-178` 在持久提交后无 await 发布并 notify。`backend/app/main.py:73-84` 在 bootstrap 和任务启动前 load 系统设置。

**与主会话协调后的最小方案**：不增加第七个全局设置。作品 `inherit` 的默认来源是代码定义的“不限额”；任务 `inherit` 的默认来源是现有系统设置的最新有效 `task_max_per_user`。用户显式额度替代该项默认，不再额外取与统一用户默认值的最小值；实例 `task_max_concurrency` 始终是独立硬约束。不要虚构可编辑的全局作品默认设置，也不要改写现有六项的 null/恢复语义。

### 2. 真正创建 Project 的四处入口

全量搜索 `backend/app` 中的 `Project(` 与 `create_project(` 得到以下四处实际 ORM 构造；`ApiKeyProject` 不是作品创建。

| 入口 | 实际落库位置与事务 | 配额接入要求 |
| --- | --- | --- |
| 普通创建 `POST /projects` | `backend/app/api/v1/projects.py:18-20` → `backend/app/services/project_service.py:216-238`，直接构造 Project，可能同时创建兼容旧风格，最终 commit | 在任何副作用前取得 owner User 写屏障、读取最新额度并统计，持锁直到 Project 和兼容风格一起提交；不能只在路由或前端检查 |
| 灵感转作品 | `backend/app/api/v1/inspiration.py:111-113` → `backend/app/services/inspiration_service.py:570-608`，构造 Project 并将灵感会话标为 converted，同事务 commit | 准入失败不能改变灵感会话状态或留下作品；并发等待屏障后须按最新资源与会话状态执行，不能继续用最初读取的旧状态 |
| 项目备份导入（预览令牌与直接 multipart 共用） | `backend/app/api/v1/transfer.py:37-58` → `backend/app/services/project_transfer/importer.py:867-884`；`_Importer.run` 在 `:847-864` 先导入风格再建作品，实际构造在 `:711-729` | 统一在 `import_backup` 的导入事务开始处准入，不能只在 `_project` 之前遗漏已发生的风格写入；失败整体 rollback。预览 `:611-631` 不创建作品，不预留名额；真正导入须重新核对。只有成功导入后路由才 drop token，配额拒绝不能先消费令牌 |
| 拆书开始导入 | `backend/app/api/v1/book_imports.py:78-80` → `backend/app/services/book_import/service.py:292-369`；`:315` 构造 Project，随后章节、大纲、版本、删除解析记录，与 `task_service.create_task` 的 commit 同一事务 | 准入失败须保留原解析记录，不能残留作品/章节或 queued 任务。正文拼接/计字在线程 `:312-313`，宜先完成耗时纯计算，再进入有序父用户屏障与重读阶段 |

以下不增加作品数，不应误扣名额：

- 创作向导 `backend/app/api/v1/wizard.py:13-23` 只为既有 `project_id` 创建后台任务；`project_service.update_wizard_progress:288-305` 更新已有作品。
- 灵感应用到已有作品 `backend/app/services/inspiration_service.py:645-658`；已有作品反向创建灵感会话 `:661`。
- 回收站恢复 `backend/app/services/recycle_bin_service.py:184-201` 只清除删除状态，作品本来就计入总数；即使当前已超额也不应再次扣作品额度。软删不释放名额，只有真实 purge/expire 物理删除成功后名额才释放。
- 普通章节/设定编辑不受作品数量上限阻挡；用户额度为 0 也仍能读写已有作品的原有个人资源能力。

### 3. 用户配额的原子存储与检查边界

建议由窄的 quota service 统一负责有效值解析、计数与准入；不改为数据库存储一个容易与 Project 实际数据漂移的“已用作品数”。

- 三种状态必须显式区分 `inherit`、`limit`、`unlimited`；`limit` 需要非负整数，0 不是无限。传输和持久化避免用同一个 null 同时表示继承与无限。必须拒绝 bool 冒充整数、负数、浮点、字符串数字、未知指标与未知模式。
- 可以新建用户级配额表（user_id 唯一主键、FK User CASCADE、revision、两项 mode/value）；无行表示全部继承。这样无需重建 User 父表，首次写入也能通过先锁父 User 串行化。若整体 RBAC 设计选择给 User 原位加字段，也必须满足相同状态/校验/屏障契约，不能混用两套来源。
- 配额变更使用 revision/CAS 防止同时编辑静默覆盖，并与安全审计同事务；操作 actor 与资源 owner 不得混淆。管理账号的配额只能由超管变更，目标是否拥有管理权限须在实际写入边界重读，不能凭旧页面或角色名判断。
- 统一父用户屏障可沿用项目现有的真实 no-op UPDATE 思路，而非仅 `SELECT FOR UPDATE`：`backend/app/services/admin_task_service.py:186-198` 已按 ID 升序对 actor/owner 写锁，`admin_service.py:201-202` 删号在 User 写屏障后统计作品。显式保持 `updated_at=User.updated_at`，因为 `backend/app/models/base.py:19-23` 的 onupdate 会把普通 no-op UPDATE 算成资料编辑。
- 作品创建只需 owner User 锁并持到事务提交：作品默认无限不可编辑，显式配额修改同样必须取得该用户锁，因此创建与额度变更可线性化。创建事务**不得在持有 User 后再进入 AuthorizationState gate**。主会话的管理写顺序是 gate → User ID 升序 → 目标资源，所有调用路径必须共同遵守。
- 不推荐为所有大型作品导入持有全站 AuthorizationState gate：备份可含大量子行，整笔导入事务期间会阻塞全站授权变更和调度。若实现选择统一持 gate，须显式评估并记录该代价，不能误称只持一个短锁。

#### 最坏情况充分性

对同一 owner 的所有四处创建入口，在同一父 User 写屏障下读取有效有限上限 L、统计全部现存作品数 n，仅当 n < L 才插入，并持锁到 commit/rollback。任意多个并发请求在该 owner 上串行：前一笔成功插入提交后，下一笔才重新统计；每次插入后的 n+1 ≤ L。失败 rollback 不增加占用，额度更新必须经过同一 User 屏障，故不能在旧上限检查与新作品提交之间插入一个未受序列约束的降额。降低到现有用量以下保留已有数据，但所有后续新增都被拒绝。物理删除减少总量，软删/恢复不改变总量。

仅先查 n 再普通 INSERT 没有该保证：两个请求可以同时在 n=L−1 时通过，提交后变成 L+1。仅前端检查、仅标准创建入口检查、仅正常书架计数同样不足。

#### 现有锁顺序必须调整的落点

`backend/app/services/book_import/service.py:81-86` 的 `get_owned_import(lock=True)` 用 FOR UPDATE，`start:293` 在父 User 锁之前先锁 BookImport。删号持 User 后会级联用户级解析记录；如果新增准入又按 BookImport → User 取锁，就可能与删号 User → BookImport 形成反向等待。应改成有序父 User 屏障后再锁/重读解析记录，并重验 revision 和归属。不要为配额重构整套拆书处理，聚焦准入顺序。

### 4. 后台任务数：必须限制真正领取，不拒绝排队

#### 当前队列路径

- `backend/app/services/task_service.py:146-192` 创建 queued 行、同步抢占资源、记录创建统计、提交后建 hub/notify；`:195-202` 重试复用创建；`:205-240` 暂停继续通过状态 CAS 重新入队。达到并发额度时这些请求仍可正常排队，不能在 create_task 抛“额度用尽”来代替调度约束。
- `backend/app/services/analysis_service.py:554-585` 与 `:588-626` 会直接构造 queued Task（拆书深度分析与生成后自动分析），不一定经过 create_task；统一在引擎领取处限制才不会漏掉这些入口。自动分析 `Task.user_id` 明确取项目 owner，不能计在执行人工管理操作的 actor 身上。
- `backend/app/services/task_engine/engine.py:210-247` 全队列按 id 扫描，当前只用 `settings.task_max_per_user` 与 `_running_users` 计数；`:237-246` 先登记内存执行体，再异步调用 `_claim_and_run`。
- `engine.py:278-304` 当前 CAS 只复核 task UUID/status、作品可见和管理员取消意图，没有用户额度复核。若仅在 `_dispatch` 开始时批量读一次用户额度，管理端可在候选登记与实际领取之间降额，旧候选仍会启动，这是必须补的竞态。
- `engine.py:410-481` 在真实执行体的 `_finalize` 内先提交 DB 终态，再 await `on_success`；所以 `COUNT(Task WHERE status='running')` 不是实际并发占用。真实并发直到 `:263-274` 的 `_on_done` 才释放，涵盖 runner finally、终态持久化与成功派生回调。
- `engine.py:12-14` 已规定 paused 不占并发；继续后重新 queued、重新准入。`engine.py:578-601` 的请求侧取消清理注册属于资源安全排空，不能随意改成配额名额，避免把“资源仍在清理”和“正在运行一个后台执行体”混为一谈。

#### 与主会话 AuthorizationState gate 配套的最小领取方案

不额外发明另一套互不协调的 semaphore；采用同一个应用级短 runtime 协调临界段与拟新增 singleton DB gate，对**短领取事务**和管理额度/默认设置变更建立一致顺序。runtime 临界段必须覆盖 commit 返回以及内存 admitted/Settings 发布完成；DB 行锁本身不足以覆盖此窗口。

1. `_dispatch` 可保持候选预登记与 serial_key/FIFO 扫描，但不能把预登记候选等同于已成功领取的运行占用。预筛选也须使用批量读取的各用户有效额度，不能仍以统一 `task_max_per_user` 阻挡显式更高值或无限用户；0 可直接跳过，无限必须显式分支，不用真假值兜底。批量快照用于减少无效候选，不替代领取处最新值复核。
2. 明确区分“候选执行体注册”和“已经领取、真实执行体尚未退出”的 admitted 集合；后者按不可复用任务身份和当前轮次绑定，不能仅凭可复用整数 id。新增状态仅表达现有两阶段调度的事实，不是第二套队列。
3. `_claim_and_run` 先取共享 runtime 临界段，再取 AuthorizationState DB gate、owner User；此后读取最新用户配额与同一共享 Settings 的最新默认值/全局上限。
4. 按 admitted 集合统计实际占用，不把其他尚未领取候选全部计成运行任务。否则 `_dispatch` 一次预登记多个候选后，每个领取者都会看见“已满”，造成全体拒绝或低龄候选反而先运行。
5. 仅当用户与全局两级准入均允许时执行原有 UUID/status/生命周期/取消意图 CAS 和 running 统计；commit 明确成功后，在**仍持有 runtime 临界段**时把当前执行身份同步登记为 admitted，再释放临界段并进入 `_run`。未获准者保持 queued，不伪造 running→queued 轮次、不提前改 started_at，也不取消其他执行体。
6. admitted 必须覆盖 DB 终态后的 on_success，并只在真实 `_on_done` 移除；失败领取的候选也通过原完成回调清理预登记并 notify。
7. 用户配额管理写和系统并发设置修改使用同一 runtime 临界段及 DB gate；持久提交后 Settings 的发布保持同步，并在发布完成后才释放 runtime 临界段。这样先提交降额的管理写会先于随后领取被观测；先完成领取的任务属于已运行，降额不打断它。已有 default 用户、用户显式 override、用户无限三种情况都必须覆盖。

**为什么“DB gate + commit 后无 await”还不足够**：数据库可能已提交并释放行锁，但第一个协程的 `await db.commit()` 尚未返回；另一个已等待的 SQL 领取可以先被唤醒，看见内存 admitted 尚未发布。这个跨驱动/事件循环窗口不能靠 Python 在 commit 返回后的几行同步代码排除。runtime 临界段阻止其他准入/发布观察到该中间态。

固定锁顺序建议为：系统设置路径先取得已有 `SystemSettingsManager._lock`，再取得共享 runtime 临界段 → DB gate → User ID 升序 → 目标资源；其他管理写/领取从共享 runtime 临界段开始，不能在持 runtime 后再调用会获取系统设置 `_lock` 的 `load/update`。所有活跃调度期间可能改变有效默认值的 `_publish` 路径都要覆盖，包括管理更新前的持久重读、不确定提交恢复、后台恢复和主动 load；不能只修成功 PATCH 的最终发布。内部 helper 必须区分“已持锁”路径，避免非重入锁二次获取。应用启动、引擎尚未启动时可顺序初始化。

提交确认未知时不启动真实执行、不盲重放原领取或额度更新；按不可复用身份/轮次重读持久事实后才能作出已确认判定，无法确认时留安全状态并明确错误/恢复路径，不把尚未确认的领取视作可释放后随意重跑的机会。该机制与原引擎 UUID/轮次恢复共同验证，不在 quota service 私自吞掉提交异常。

若不采用 admitted 集合，也可改成真正串行的“预留→领取→启动”流程，但必须同时证明内存执行体在 DB running 出现前已登记、不会因等待锁或 HTTP 取消丢失、全局/每用户名额不会因候选被重复计算。不能只把原 `Counter(_running_users)` 换成一个数据库计数就宣称完成。

#### 运行配额用量的展示口径

作品用量可直接读全生命周期 COUNT；任务用量建议显示“当前并发占用”，与引擎的真实 admitted 执行体口径一致，避免一边显示 DB running=0、一边 on_success 尚占名额导致用户不理解为何不调度。不得把包含所有待领取候选的 `_running_users` 当作精确已运行统计。若接口只展示 DB running，须明确那不是调度额度占用，不承诺两者严格相等。

### 5. 降低、提高、重启与失败

- 用户任务额度 0：保留 queued，不启动新的后台执行体；已运行继续到原有终态。用户无限：仅移除用户级约束，仍遵守全局上限、serial_key 和 resource_key，不把“不限额”编码为 0。
- 提高用户额度、从 0 改有限或无限、从覆盖改为继承，提交后 notify 调度器；失败或审计回滚不发布新值。无需新增按日重置任务、缓存或计费日志。
- `backend/app/services/task_engine/engine.py:82-91,124-155` 启动先恢复遗留 running，queued 保留；单进程约束见 `:1-14`。新配额不会使重启后的旧 queued 绕过领取检查。
- 系统设置已有 shield/自有会话、不确定提交重读、关停等待，见 `system_settings_service.py:100-116,179-208`。用户配额若引入应用缓存，也必须承担同样的提交确认与发布一致性；最小方案不另建持久额度缓存，直接在真实准入事务读取。
- 导入解析可以在锁前完成，锁内数据库写入必须保持原整体事务；不能在作品 flush 后提前 commit 以缩短锁，从而破坏章节/风格/灵感会话/解析记录删除的原子性。请求取消或业务错误应 rollback；提交结果未知时不自动重放旧创建或额度 PATCH。

### 6. 迁移、导入注册表和回滚

- 调研时迁移目录最新为 `backend/alembic/versions/0028_user_last_login.py`。新增配额表/字段与 RBAC 迁移统一规划，实施前再核对唯一 head；不能硬覆盖并行迁移。
- 作品配额/任务配额及用户角色都是用户/实例级设置，不属于小说内容；不得导入导出它们，不允许备份中的字段把目标账号改成不限额或迁入管理角色。
- `backend/app/services/project_transfer/registry.py:60-70` 与 `backend/tests/test_project_transfer.py:45-67` 只收录沿外键“引用 projects”方向关联的表；只有 User 外键的新配额表不属于其集合，**不要把它塞入 NOT_EXPORTED**，否则反射测试会失败。
- `backend/app/services/project_transfer/exporter.py:35` 的 APP_SCHEMA 仍是项目格式/相关 schema 说明，不因新增用户级设置自动修改。原备份格式、Project 表列和内容映射不因配额配置改变。
- 迁移应验证存量 User/Project/Chapter/私有配置、外键完整性及默认继承；避免为了字段改动重建 User 父表引发级联/RESTRICT。既有存量测试模式见 `backend/tests/test_migrations.py:13-73`。
- 回滚旧应用会失去新配额强制执行，不能声称“保留配额表就仍有限制”。含新角色/审计与配额配置时的 downgrade 应按整体 RBAC 设计受保护拒绝或显式离线转换；先停写/备份，优先前滚修复。

### 7. 必需验证与可复用测试落点

现有测试不是 StaticPool 内存库：`backend/tests/helpers.py:179-192` 为每个测试创建独立 SQLite 文件，`backend/tests/conftest.py:50-68` 创建 schema、load 系统设置并显式启停引擎；`backend/app/core/database.py:5-21` 开启外键并使用 expire_on_commit=False。因此可在现有夹具上安排真正独立 session 的并发屏障，但仍不能证明 PostgreSQL 行锁语义。

建议新增聚焦用户配额的测试文件，并回归这些既有套件：

| 场景 | 必需断言 / 复用位置 |
| --- | --- |
| 模式与安全字段 | inherit / limit 0 / 正整数 / unlimited，严格类型和数据库整数边界；无覆盖存量兼容；只有真实授权者可写普通账号、管理账号仅超管；审计无密码/私有配置 |
| 四入口同一用户并发争最后名额 | 2 个及以上独立 session/HTTP 请求，可混合普通创建、灵感 convert、备份导入和拆书 start；至多允许一笔增加作品，其他安全错误且无半成品；不同 owner 互不挤占 |
| 生命周期 | 正常/回收站/删除中均计数，软删与恢复不释放/再扣，purge/expire 成功才释放；额度调低后仍可维护已有作品，0 不误作无限；超管也不豁免 |
| 导入原子性 | `test_project_transfer.py:844` 的错误回滚、`:897` 的直接 multipart、`:959` 的导入后建索引；预览后改额度，真正导入按最新检查，拒绝时 token/风格/内容无错误副作用 |
| 拆书原子性与锁顺序 | `test_book_import.py:323` 的 start 校验/回滚、`:380` 的 revision 竞争；quota 拒绝保留解析记录，User→BookImport 与删号不反锁；建章+queued Task 不出现部分提交 |
| 基础调度 | `test_tasks.py:302` 用户并发与队列位置、`:611` 同 serial_key 串行与跨 key 并行、`:677` 重启串行；手动、重试、继续、自动分析、拆书派生分析全部共用领取上限 |
| 降额/提高竞态 | 扩展 `test_system_settings_runtime.py:43-104` 的 Gate/事件式测试：候选已预登记但尚未领取时降为 0/1；新领取不得用旧快照，原执行体不取消；提高后 notify；同时修改默认但显式覆盖用户不受影响；人为卡住“DB 已真实提交/释放锁、commit await 尚未返回”窗口，其他领取不得先看到未发布 admitted/Settings；load/recovery 发布同样不能绕过临界段 |
| 真实执行体占用 | 卡住 runner finally 或成功回调；即使 DB 已 terminal 也不能过早释放 admitted；暂停后执行体退出才释放，resume 必须重新准入；候选数多于额度时至少正确启动允许数量而不是全体互相判满 |
| 全局硬上限 | 多用户有限/无限/继承混合，真实活跃执行体总数不越全局；全局设置变更与领取同 gate 的顺序测试，不只测“任务已经跑起来后降额” |
| 迁移与外键 | head/base、存量回填、首次配额行竞争、User 删除 CASCADE 配额而 Project RESTRICT 不变、备份反射；真实 PostgreSQL 独立事务再验证锁等待和最后名额，不得以 SQLite 通过替代该结论 |

并发验证使用 `backend/tests/helpers.py:21` 的 Gate 或 asyncio.Event 屏障控制顺序，不用 sleep 猜竞态；直接断言实际 provider 调用数、峰值 admitted、Project 行数及受保护资源状态。

### 8. 相关规范与精确改动边界

规范：`.trellis/spec/backend/database-guidelines.md`、`auth-admin.md`、`system-settings.md`、`admin-task-monitoring.md`、`recycle-bin.md`、`project-transfer.md`、`book-import.md`、`inspiration.md`、`batch-generate.md`、`quality-guidelines.md`。

预期必改：新 quota schema/service/模型及迁移；上述四处创建服务；`task_engine/engine.py` 的候选/领取/真实完成注册；管理员用户/额度接口与安全投影；RBAC/gate 与系统设置管理写的集成；新配额测试及迁移/回归测试。`task_service` 的入队行为和自动分析直接 queued 行不应改成“超额度拒绝创建”，最多为类型/上下文集成做窄修改。

## Caveats / Not Found

- 本调研没有执行产品测试、迁移或 PostgreSQL 实验，不构成实现已满足并发保证的证据。
- AuthorizationState gate、共享 runtime 临界段与 RBAC 身份/权限投影由另一调研和主会话整体设计；本文件只给配额必须满足的取锁与准入约束，不能与其他模块各造一套互相反向的锁。实现前必须画出系统设置现有 `_lock`、runtime 临界段、DB gate、User 和资源锁的全路径顺序。
- 不新增全局可编辑作品默认额度、章节/字数/存储/Token/日调用配额，不改变禁用账号的既有后台任务处置语义，不新增多 worker/多实例调度支持。
- 大型导入持有 owner User 写锁直到整体事务结束，会推迟该账号的管理写入；若另一操作已持共享 runtime/DB gate 再等待该 User，还会形成全站授权/领取的队头阻塞，并非只影响导入账号。CPU/解析工作应放在临界事务前；可评估 PostgreSQL 领取对繁忙 User 非阻塞跳过以减少调度队头阻塞，但不能因此另造反序锁或改变保证。这是父用户串行准入与整体导入事务的代价，应在设计/验收中明确，不以提前提交牺牲一致性。
- 当前系统 Settings 发布依赖同一事件循环和单进程部署。多实例配额与全局调度租约不在此次范围，不能宣称数据库 gate 已提供完整的多进程任务调度正确性。
