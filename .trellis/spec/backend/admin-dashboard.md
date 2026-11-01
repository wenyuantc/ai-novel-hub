# 管理员首页、每日采样与独立任务统计

## 1. 范围与触发条件

适用于 `/admin/dashboard`、`DashboardTracking` / `DashboardDailySnapshot` / `TaskStatistic`、Task统计身份、所有任务创建/状态/清理入口及采样器。修改后台任务入队、恢复、领取、暂停/继续、结束、取消或业务级联删除时，必须同时检查本契约。

目标是实例级计数与运营总览，不授予管理员读取他人的任务输入/输出、模型密钥或私有资源的权限；任务安全明细与取消受理见独立[任务监控契约](./admin-task-monitoring.md)。

## 2. 签名与存储

迁移 `0025_admin_dashboard`，父版本 `0024`：

- `admin_dashboard_tracking`：单例id=1、UTC `started_at`、`timezone=Asia/Shanghai`。
- `admin_dashboard_daily`：北京时间 `day` 主键、UTC `captured_at`、`users/projects/chapters/total_words/pending_submissions` 非负大整数。
- `admin_task_statistics`：UUID `key`、`kind`、`origin=baseline|new`、`observed_at`、可空 `created_event_at`、`status`、可空 `finished_event_at/removed_at`。创建时间及状态/结束时间具有索引。
- `tasks.statistics_key`：可空唯一UUID，运行时为新任务生成，初始化时补齐存量；不进入公开 `TaskOut`。

统计表没有指向User/Project/Task的业务级联外键，不进入作品备份，不保存名称、正文、任务输入/输出、原始错误或凭据。

写入方 `backend/app/services/task_statistics_service.py`：

```python
async def initialize_tracking(sessionmaker) -> None: ...
async def record_created(db, task) -> None: ...
async def record_state_change(db, task, status, *, finished_at=None) -> None: ...
async def mark_removed(db, *conditions) -> list[str]: ...  # 返回本事务锁定的统计UUID集合
# app/services/task_service.py
async def refresh_same_task(db, task) -> Task: ...  # 原UUID不存在则404，不能刷新为同ID的新任务
# TaskEngine内部接口，None只兼容未绑定的旧内部构造，不是通配身份。
def close_hub(task_id, event, data, *, statistics_key=None) -> None: ...
async def cancel_running(task_id, *, statistics_key=None) -> None: ...
def forget_project_hubs(task_ids, *, statistics_keys=None) -> None: ...
```

除初始化拥有独立会话外，helper仅flush/执行SQL，不擅自commit。采样方为 `admin_dashboard_service.collect_snapshot(sessionmaker, *, captured_at=None)` 及 `DashboardSampler.start()/stop()`。

每个GET独立检查dashboard.read，公共前缀 `/api/v1/admin/dashboard`；[RBAC](./rbac-quotas.md)不因首页权限授予tasks.read/users.read/workshop.read：

| 路径 | 响应 |
| --- | --- |
| `/overview` | `as_of, tracking_since, timezone, users, projects, chapters, total_words, pending_submissions` |
| `/tasks` | 元信息及 `baseline_tasks,new_tasks,queued,running,paused,succeeded,failed,cancelled,interrupted,failed_24h,interrupted_24h,removed_unfinished` |
| `/history?days=7\|30\|90` | 默认30；元信息、`days,start_date,end_date,points,comparison` |

`points[] = {day,captured_at,totals|null,tasks|null,partial}`；totals对应五项规模；tasks为 `created/succeeded/failed/cancelled/interrupted`。comparison包含首尾日期及四项允许负值/空值的规模净变化。Pydantic定义在 `schemas/admin_dashboard.py`，前端传输类型逐字段镜像。

## 3. 核心契约

### 计数与时间

- 用户计全部仍存在账号，包含管理员和禁用账号。
- 作品、章节、字数包含回收站及删除协调中但尚未物理删除的作品。字数只求和当前章节 `word_count`，不叠加版本/生成记录。用独立SQL标量聚合，不能join后产生乘积。
- `pending_submissions` 是工坊pending，不是Task.paused。
- 当前queued/running/paused来自真实Task表；上线后新建与真实终态来自独立统计记录，清理不会减少累计。
- baseline只是初始化时仍存在的记录，不冒充完整上线前历史或上线后的新建/旧终态。baseline未结束任务上线后实际结束时可记结果。
- 暂停/继续同UUID不新增，retry的新UUID是新任务；尚未结束即移除只标记removed，不伪造终态。
- 时间戳UTC存储，日期按Asia/Shanghai划分；新建事件用带精度的观测时间，不能用SQLite仅秒精度的created_at判断是否早于统计起点。终态业务和统计共用同一时刻。
- 24小时失败/中断是 `(as_of-24h, as_of]` 中真实记录的终态事件。

### 事务与执行身份

- 必须覆盖通用create/retry、自动/批量分析及记忆索引的直接Task插入，和所有取消/提前结束/恢复/finalize分支。
- 原业务条件UPDATE命中才记录迁移；Task与统计同事务提交/回滚，终态最多一次。保留原父行锁、资源键、原事务所有权与管理员审计。
- 单删、清理、裁剪、作品purge/expire及删号在原事务内标记移除，不能删统计行。批量删除仅使用mark_removed返回的已捕获UUID集合，不能在标记后以宽条件重新选择并误删新任务。锁顺序保持Task→统计，避免与终态写入死锁。
- **整数Task.id不是执行身份。** SQLite删除后可复用ID；持有旧Task对象/旧执行体的更新、刷新、移除以及Hub/输出路径，必须同时核对原statistics_key，不能作用到同ID的新任务。旧对象不能通过按id的refresh换成新对象后继续处理。
- TaskHub携带statistics_key，`_running_statistics_keys`保存调度执行体绑定；close_hub/cancel_running/恢复收尾/项目forget必须传原UUID或捕获映射。省略key的旧内部调用只能操作未绑定构造，不能关闭或取消已有真实UUID的Hub/执行体；不能在调用前一次核对后跨await再只按整数id操作。
- 不能在HTTP成功后、任务裁剪后或定时扫描时补写累计，也不能把统计失败误译为普通“资源已有任务”。

### 取消与原子保存

运行中取消在等待执行体收尾前先结束请求侧事务，不能持有独立清理事务需要的锁。章节结果保存由 `generation_service._save_chapter_result` 保护有界的正文/版本/生成日志原子事务，不屏蔽LLM等待。取消不直接打断执行中的SQLite `INSERT ... RETURNING`：本项目实际驱动组合中，未收尽的活游标即使在连接失效/关闭后仍可能持写锁，导致on_abort和终态统计均无法写入。

提交前收到取消，保存协程先完整回滚/退出会话，再传播取消；提交已经开始的极晚取消，等待确定保存并产出done，保存获胜、记录唯一成功终态，不声称撤销已提交正文。不得以提高锁超时、WAL或无限重试掩盖未释放游标，也不得把整个模型调用包在shield内。

### 采样与历史

- 初始化在任务恢复前幂等执行，重启不重置起点；引擎start覆盖不走lifespan的测试。主程序启动采样器，立即及每60秒独立会话采样，先停止采样器再dispose数据库。
- 每天只保留最后一次成功规模采样；较旧采样不能覆盖较新值。失败不写零，不修改已有成功值，重启不回填漏采日期。
- 历史完整返回所选日期序列。上线前两类数据为空；没有规模采样时totals为空，但当天已提交任务事件仍返回并标partial；既无采样也无事件时不伪造零活动日。
- 首日/当日及日终最后采样不完整的日期标partial。日值不是停机期间准确的午夜终值，必须展示真实采样时刻。
- 规模净变化只比较所选首尾有效采样，任一缺失则null，不换成更短区间或补零；净减少不是错误，不能把净变化称为新增或写作产出。

### 前端

- RequirePermission(dashboard.read)下懒加载 `/admin/dashboard`，同步侧栏/账户菜单/面包屑；跨域深链接分别检查对应读取权限，无首页权限的管理账号不强制跳本页，普通用户仍进入个人作品。
- 三块请求独立加载/重试，失败保留旧值时标过期及上次成功时间；无数据不可用`?? 0`假装成功。
- 7/30/90默认30；手动及60秒自动刷新，可关闭，hidden暂停、visible恢复。切换范围、卸载、登出、403等令旧请求失效并清理轮询。
- 图表缺口断线、轴文字在375px保持可读；SVG viewBox跟随实测容器宽度，不能把固定大画布等比缩到不可读。提供分页HTML历史表，不只靠颜色/鼠标悬浮。
- 作品总量明确“含回收站”，正常作品与回收站分开入口；工坊`?tab=review`只允许管理员，非法/普通用户tab回browse。已交付的任务监控入口跳/admin/tasks及失败/中断状态列表，并明确“现存任务”；累计含已移除任务，不能承诺与明细条数相同，也不能把24h终态累计误接成创建时间明细。

## 4. 校验与错误矩阵

| 条件 | 行为 |
| --- | --- |
| 未登录/禁用/会话过期 | 401，沿用全局认证清理 |
| 当前非管理员 | 每个API均403；页面停止自动请求 |
| days不是7/30/90 | 422，不悄悄改成默认值 |
| 跟踪起点尚未初始化 | 503 `管理统计尚未初始化，请稍后重试`，不返回假零 |
| 单区块失败 | 独立错误/重试，不清空其他成功区块 |
| 采样早于起点/不支持方言 | 拒绝写入；采样循环记录错误并在下周期重试 |
| 业务事务回滚/条件迁移未命中 | 不产生新建或终态统计 |
| 旧Task身份已消失/同ID已是另一个UUID | 不更新、不刷新成新任务，不泄露新任务输出 |
| 存在日快照或任务统计时直接downgrade | 拒绝破坏性降级，要求先显式备份/处理 |

无需新增环境变量或外部服务。升级增加可空列/索引，不为此重建SQLite tasks父表，以免触发已有生成日志/智能体外键副作用。部署仍要求单进程，不支持新旧版本同时调度；回退期间旧代码可能造成未跟踪区间，不能伪称连续历史。

## 5. 正例、基准与反例

- 正例：用户删除已完成任务、后台自动裁剪200条历史，再彻删作品；此前已记录的累计成功/失败与每日结果保持。
- 基准：作品移动到回收站又恢复；作品/章节/字数总量不变。真正彻删后当前总量减少，但历史快照不重写。
- 反例：把现存Task按created_at分组当历史新增；或新旧任务整数ID相同就让旧取消/输出回调操作新行。

## 6. 必需测试

- `test_admin_dashboard.py`：全部账号/回收站口径、无join乘积、401/403/422/503、7/30/90与北京时间跨日、缺失/零/负净变化、baseline及24h边界。
- `test_admin_dashboard_sampler.py`：立即/周期采样、重复启停、异常不覆零、倒序样本不覆盖、停机漏采与协程清理。
- `test_task_statistics.py`、`test_task_statistics_integration.py`：所有入队/迁移入口、回滚、暂停继续/重试、并发结束/取消与重复回调、清理/裁剪/级联保留、UUID隔离ID复用，含旧执行体/旧输出不影响新任务。
- `test_task_statistics_integration.py::test_running_cancel_drains_atomic_save_before_cleanup`：真实RETURNING游标/已commit两种可控屏障，断言提交前全回滚、已提交保存获胜、唯一终态、请求事务释放且随后独立SQLite写入成功；单例偶然通过不能替代该机制回归。
- `test_task_statistics_identity.py`：真实双session复用SQLite整数ID，证明旧取消/暂停操作/删除/裁剪/恢复/调度、旧Hub/flush/output/stream均不能作用于新UUID；只测主键不同的任务不足以覆盖此问题。
- `test_admin_dashboard_migrations.py`：0024存量升级不损坏Task子行/外键、空库降级、非空统计拒绝降级。
- 前端API/纯函数/控制器/图表测试：契约、null和负值、范围竞态、401/403/卸载旧响应、轮询暂停/恢复、窄宽容器坐标及深链接；真实浏览器核对375px/桌面/浅暗/重试/精确表格。

## 7. 错误写法与正确写法

```python
# 错：清理后计数丢失；旧整数ID可能已经指向新任务。
await db.execute(update(Task).where(Task.id == old_task.id).values(status="cancelled"))
await db.commit()
# 再异步扫描Task表补统计不能恢复已经被删掉的结果。

# 正：原状态与不可复用身份均命中，才同事务写独立统计。
result = await db.execute(
    update(Task).where(
        Task.id == old_task.id,
        Task.statistics_key == old_task.statistics_key,
        Task.status == "queued",
    ).values(status="cancelled", finished_at=stamp)
)
if result.rowcount == 1:
    await record_state_change(db, old_task, "cancelled", finished_at=stamp)
await db.commit()
```

同样的身份规则适用于加载/刷新、内存Hub和输出刷新；只保护SQL终态却仍按整数id重用Hub，不算完成隔离。
