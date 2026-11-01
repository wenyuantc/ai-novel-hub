# 管理员全站现存任务监控与取消请求

## 1. 范围与触发条件

适用于 `/api/v1/admin/tasks`、Task 入队时间/管理员取消标记、任务状态条件写、引擎取消 drain/finalize/recover、管理审计 task 动作、项目排空与前端 `/admin/tasks`。修改任一 Task 状态入口、恢复入队、资源清理或管理元数据投影时，必须检查本契约。

仍为单进程数据库队列，不提供长期明细归档、阈值告警、自动取消或管理员重试/继续/成功提前结束/接管他人任务。普通 owner API 和私有模型/Skill/MCP/输出权限不放宽。已提交正文、已执行智能体修改及外部工具效果不会因取消自动撤销。

## 2. 签名、传输与存储

迁移 `0026_admin_task_monitoring`，父 `0025`，在 tasks 原位增加可空列：

- `queued_at: datetime | null`：本轮入队的可信 UTC 起点，新 Task ORM 默认当前时刻，成功恢复入队时刷新。
- `admin_cancel_operation_id: str(36) | null`、`admin_cancel_requested_at: datetime | null`：成组写入/清空的持久取消意图，operation_id 绑定受理审计。
- `ix_tasks_admin_cancel_operation_id` 支持 pending 扫描。没有新增任务历史表，不复制原因/actor 到 Task，不把这些运行控制数据加入作品备份。

升级不重建 SQLite tasks 父表，不为旧记录伪造入队起点；有 pending 意图时拒绝 downgrade。仍不支持新旧调度器并行运行。

GET需tasks.read，取消POST需tasks.cancel，均使用真实当前actor；共同授权屏障见[RBAC](./rbac-quotas.md)，前缀 `/api/v1/admin/tasks`：

| 方法/路径 | 参数 | 成功响应 |
| --- | --- | --- |
| GET 空路径 | q≤200；owner_user_id/project_id/task_id正整数；kind；重复status多选；成对带时区from_at/to_at；sort=created_desc/queue_longest/running_longest；page≥1；page_size=20（1–100） | `{items,total,as_of,counts}` |
| GET `/{task_key}` | UUID，不接受整数任务 ID 作为执行身份 | `AdminTaskDetail`（brief + as_of） |
| POST `/{task_key}/cancel` | `{confirm:true,reason}`；trim后1–200字，不含Unicode C类控制/格式字符 | 202 `{operation_id,audit_log_id:number|null}` |

`AdminTaskBrief` 字段：

- id、task_key（旧数据可空）、kind/status（未知值投影为unknown）、安全title、progress（0–100）。
- owner_user_id/owner_username、project_id/project_title、project_lifecycle=active/deleting/recycled/none、chapter_id。
- stage、error_code/error_message；error_code仅execution_failed/cancelled/interrupted/cleanup_pending/null。
- created_at、queued_at、started_at、finished_at、queued_seconds、running_seconds。
- cancellation_pending、cancel_requested_at、can_cancel。

counts为七种状态同名计数加unknown，口径是**所有当前筛选条件下、所有页的现存任务**，不包括已裁剪/清理明细，不是首页独立历史累计。不返回任务输出或队列开始时间承诺。

后端核心：

```python
# app/services/admin_task_service.py
async def list_tasks(db, engine, filters: AdminTaskFilters) -> AdminTaskList: ...
async def get_task(db, engine, task_key: str) -> AdminTaskDetail: ...
async def cancel_task(db, engine, actor: User, task_key: str,
                      data: AdminTaskCancelRequest) -> AdminTaskCancelOut: ...

# app/services/task_engine/engine.py
async def complete_state(self, task, *, expected_status, status, result, error,
                         text, progress, abort_ok, operation_id=None): ...
def request_admin_cancel(self, task_key: str, operation_id: str) -> None: ...
async def scan_admin_cancellations(self) -> None: ...
async def wait_admin_cancellations(self) -> None: ...
```

前端传输镜像在 `api/types.ts`；`api/adminTasks.ts` 经过现有 request/Cookie/AbortSignal。页级状态所有权属于 `components/adminTasks/adminTasksController.ts::AdminTasksController`，不属于个人任务 store。

## 3. 核心契约

### 安全读取与查询

- 只读现存 Task，包含仍有任务行的回收站/删除中作品并显示生命周期。普通 get_owned_task、项目生命周期与 owner 路由不修改为管理员通配。
- 元数据查询只选择必要列，**不读取原 Task.title/error/input/result/output_text/checkpoint**。智能体 title 可能包含请求前20字，不能当作天然安全标题；管理标题固定为类型标签加编号。
- q仅对用户名/作品标题做转义后的字面量子串查询，不能搜索任务原始标题/错误/输入产生有无结果侧漏。
- 原始stage仅允许完全匹配的内部固定值或有界数字模板；私有工具/插件调用退化为“调用工具”，其余回退到状态标签。不能截断或宽松替换后透传。
- 错误摘要只依赖真实任务状态与pending标记，不从原错误猜测具体供应商原因；failed为固定“执行失败”，成功/正常进行状态不因残留旧error被显示失败。普通所有者原错误展示不因此改变。
- 活动进度来自相同task_key的未关闭Hub；旧整数ID对应的Hub不能覆盖新UUID数据。阶段仍经安全投影。
- 候选过滤CTE、状态聚合与分页合并为一条SQL，空页仍有total/counts；不能在PG READ COMMITTED下用分离查询拼出互相矛盾的响应。各次HTTP刷新不是冻结快照。
- 创建时间筛选为UTC半开`[from_at,to_at)`；前端本地日历含结束日，转次日零点作为排他终点。
- queued_seconds只计当前queued轮、running_seconds只计当前running轮；未知起点为null，不从原created_at或可变updated_at推测。时长排序先对应状态、有效最早起点、稳定ID，未知时间置后。
- resume在同事务更新queued_at，遇同一时刻/时钟回拨仍保证新轮次严格递增；同UUID的新轮不能复用旧winner/completed知识。

### 可靠受理，不是假“原子取消”

1. 写前按runtime→auth DB gate→User ID升序取得真实actor/owner屏障，复核启用、tasks.cancel及原已认证token版本，防降权/无作品删号竞态。不能让AuditOperation的enabled=False放行未审计取消。
2. 按UUID核实Task与真实OPEN状态；目标标签固定，不复制Task.title。相同任务已有pending时回同一operation_id，不重复受理/信号/abort；审计被显式清理时audit_log_id可空。
3. 条件写绑定`id + statistics_key + 真实原状态 + marker IS NULL`，写入两列意图；与`task.cancel_request`成功受理审计共用audit.commit事务。
4. 只有确认受理提交后才唤醒独立drain。202仅是已受理，客户端重新读取真实状态，不固定提示“任务已取消”。提交确认丢失时若已提交成功，不能补相反失败审计；持久marker由周期/启动扫描处理。
5. 管理员本人作品从该入口处置也审计，使用真实actor且include_own_project=True；普通用户取消不伪装成管理操作。

同事务保证只涵盖**受理意图与受理成功审计**。执行信号、独立资源清理与最终状态是后续工作，不能声称与接受请求跨事务一次性原子完成。

### 审计局部扩展

AuditAction=`task.cancel_request`，AuditTargetType=`task`。AuditSummary只增加task_key、task_kind、accepted_from_status（queued/running/paused）、cancel_reason；原因是唯一针对该动作允许的管理员主动自由文本，仍受1–200字/控制字符校验。writer拒绝其他动作夹带这些字段，不开放任意JSON或更多自由文本。

不自动附带原错误、输入/输出/断点。UI提醒不要粘贴密钥或正文；不能承诺对任意主动自由文本可靠自动脱敏。失败只保留已校验的任务标识/种类/原因白名单，不保存未提交after值。成功文案针对该动作显示“已受理”，不能全局改写其他管理成功的语义。

### 真实执行、CAS与资源代次

- AdminCancellationDrain按`(task_key,operation_id)`single-flight，独立HTTP生存、独立session；清理全过程登记cleanup_scope/project_work。周期扫描不能在scheduler主循环串行等待全部10秒收尾。
- queued同时在扫描与实际claim条件UPDATE拒绝pending。管理取消保留原queued及其资源索引，**先成功abort，再终态+统计+清marker提交**；失败继续保留屏障。paused不重新占并发，也不能回滚后来任务资源；不安全/未知暂停断点fail-closed。
- running使用原UUID的一次cancel_running；等待10秒未完成仍pending，不二次取消执行中的SQLite保存游标。正文提交前取消回滚，提交已开始则保存获胜；late succeeded/paused保留真实结果。
- complete_state在同一条件事务写真实状态、统计、清两列marker；条件含原UUID/原状态与观察到的marker（含null）。存在pending且abort失败时保持running/资源屏障，不写假cancelled。
- 普通owner cancel的queued/paused/孤立running终态、resume/finish及其他控制状态写也必须CAS marker IS NULL；0命中后重读原UUID并委托/拒绝。先读一次marker再无条件写不构成屏障。
- 本进程已产出done/paused但终态事务暂时失败时，winners保存按UUID+queued_at本轮绑定的已知赢家，drain只重试其真实状态，不改成旧abort。completed也绑定本轮，防项目级旧失败标记重放已释放资源的abort；扫描随移除/新轮清理缓存。
- pending核心扫描与缓存生命周期核实分离；缓存键的IN查询按每批最多500个执行，不能随现存completed/winner数量无限扩展绑定参数并使整个pending扫描失败。
- 所有原finalize/recover/drain提交确认丢失均须重读同UUID事实：已提交则回放真实状态、维护对应完成代次，不再次统计/abort；未确认真实提交则保留pending，不把成功误报为失败。不能只修_finalize调用者而漏共用状态提交边界。
- 不认识handler的pending恢复/收尾fail-closed，保留意图和资源，不因找不到清理方法就释放为终态。
- 重启遗留running仍按既有interrupted语义，安全清理与真实状态/统计/清marker同事务；queued/paused已受理意图继续收尾，不重放模型或外部工具。正常不留下“终态+pending”；异常组合不得盲abort或抹恢复依据。
- 单删/清理/裁剪/无作品删号必须保护pending；项目purge/expire等待实际drain与执行链排空，不能只看OPEN计数。正常关闭先等待已登记清理再dispose数据库，不取消请求之外的收尾协程。

### 前端

- RequirePermission(tasks.read)下懒加载/admin/tasks，取消另需tasks.cancel，桌面紧凑表格固定右侧操作，375px卡片/可折叠筛选、页头与操作上下排；同步导航、账户菜单、面包屑及首页“现存任务”链接。
- 5秒可关闭轮询，hidden暂停/visible更新；慢请求不重叠、不每tick无限abort。失败保留旧值并显式过期/上次成功时间，不补假零。
- actor/过滤/页码/目标代次及AbortSignal保障旧响应失效；401/登出/卸载或失去tasks.read时清空安全详情/取消弹窗并停请求；只失去tasks.cancel时关失效取消弹窗，保留仍合法的只读列表，不登出。
- UUID深链可接受合法大写，比较/请求规范化为同一UUID，不能因服务端标准小写就误判目标变更。
- 原因输入→再次核对用户/作品/任务/原因→勾选明确确认才提交；busy禁重复、焦点可用，网络不确定先刷新不自动重发。受理、取消中、真实终态分开反馈。
- 不复用个人TaskPanel/TaskOutput/SSE接口，不向个人useTasksStore注入他人任务。首页累计含已删除统计，入口注明现存列表不保证与累计条数一致。

## 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 未登录/会话失效/禁用 | 401，沿用全局认证清理 |
| 缺对应read/cancel或受理写前已失权 | 403，无取消副作用；按实际失去读/写权分别清理 |
| 非法UUID/分页/枚举/时间对/确认/原因 | 422，不回显请求秘密，不受理 |
| UUID不存在/已移除 | 404，不改同整数ID新Task |
| 任务已结束 | 409，安全拒绝，不伪造取消 |
| 同Task已有pending | 202同operation_id，不新增重复受理 |
| 审计flush/真正提交前失败 | 意图整体回滚，无signal/abort，安全500/失败留痕 |
| 实际受理提交后确认丢失/HTTP断开 | 已受理事实不反转，持久扫描继续，独立drain不被连带取消 |
| 收尾失败/等待未完成 | 保留pending及原资源屏障，固定说明，不宣称终态 |
| 未知handler/异常终态+marker | fail-closed、安全日志，不盲abort或删除意图 |
| 有pending时降级/销毁恢复依据 | 拒绝，先明确安全处理 |

## 5. 正例、基准与反例

- 正例：管理员取消Alice排队生成任务，审计受理先成功；清理阻塞期间Task仍queued+pending，不能被claim或被新同资源任务挤占。成功收尾后唯一cancelled事件，普通历史机制继续生效。
- 基准：运行任务原子保存已经提交，取消只得到受理记录，最终succeeded胜出；UI显示真实完成，不宣称撤销正文。
- 新轮：同UUID late paused胜出后由owner继续，queued_at严格变化；旧completed/winner不能影响新轮失败修复。
- 反例：外套AuditOperation调用旧取消helper后再写日志；或终态释放资源后无限重放旧abort；或用Task.title/error截断作安全管理摘要。

不承诺任意强杀后精确重建“正文已提交、Task finalize尚未提交”的赢家；这是既有跨服务事务边界。重启仍按原中断规则，不重放模型、不虚构撤销。SQLite通过不能冒充真实PostgreSQL并发已验收。

## 6. 必需测试

- `test_admin_tasks.py`：单SQL筛选/总数/空页、字面搜索、时区/枚举/分页、当前轮排序/null、Hub UUID、安全阶段模板、成功残留error不显示失败、401/403及owner隔离。
- `test_admin_task_cancellation.py`：原子受理/重复请求、queued资源/claim屏障、late成功/暂停、重启、一次cancel/10秒、真实RETURNING和已commit保存、abort/终态保存失败、shutdown、commit确认丢失、completed/winner轮次与缓存回收、未知handler fail-closed。
- `test_admin_task_adversarial.py`：真实UPDATE前owner/finalize CAS受理插入、审计flush/commit失败零副作用且无秘密日志、真SQLite ID复用、标题/Hub插件stage/error不投影/不搜索/不审计、无作品删号、裁剪/单删、项目软删/彻删真实排空及异常pending保护。
- `test_admin_task_check_regressions.py`：实际受理commit后确认丢失仍只有成功受理、扫描只一次收尾；HTTP取消后独立工作仍登记且存活；共享终态提交确认丢失不能重放旧资源abort；601个缓存键验证IN分批≤500且pending扫描不被参数上限阻断。
- `test_admin_task_monitoring_migrations.py`：0025存量带子行/外键升级不损坏，旧时钟null，pending拒绝降级。
- 前端 `api/adminTasks.test.ts`、`utils/adminTasks.test.ts`、`components/adminTasks/*test.ts`：传输/数组/凭据、创建日期/null/固定分类、所有权与401/403/卸载/慢响应、5秒可见性开关、二次确认、真实终态、UUID大小写、两种布局与审计局部成功语义。
- 必须保留tasks/statistics/identity/recycle/audit/admin/batch/book-import全量回归，真实浏览器核对桌面/375px/浅暗/筛选/详情/取消/受控失败恢复，不能以静态渲染代替交互。

## 7. 错误写法与正确写法

```python
# 错：旧取消helper有内部提交/独立执行体；末尾日志无法回滚先发生的取消。
await task_service._cancel_loaded_task(db, engine, task)
await audit.commit()

# 对：这里只提交“受理意图 + 受理审计”；提交后才唤醒独立收尾。
changed = await db.execute(
    update(Task).where(
        Task.id == task.id, Task.statistics_key == key,
        Task.status == previous, Task.admin_cancel_operation_id.is_(None),
    ).values(admin_cancel_operation_id=audit.operation_id,
             admin_cancel_requested_at=stamp)
)
if changed.rowcount == 1:
    await audit.commit()
    engine.request_admin_cancel(key, audit.operation_id)
```

同样，普通状态写的marker检查必须进入UPDATE条件；admin后台最终写必须绑定原operation_id。只在await前检查身份/标记，然后按整数ID或旧状态继续更新，不满足本契约。
