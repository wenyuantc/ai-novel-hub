# 管理员全站任务监控技术方案

日期：2026-10-31。状态：已按用户批准方案实施并通过独立检查/最终门禁，任务completed、两个工作提交已完成。

产品契约以 `prd.md` 为准；证据见 `research/monitoring-contracts.md` 与 `research/cancellation-audit.md`。以下方案不替换任务引擎，不新增外部依赖或长期明细表。

## 1. 边界与模块

```text
RequireAdmin → AdminTasksPage → api/adminTasks.ts
                                ↓ Cookie / 当前 AdminUser
api/v1/admin_tasks.py → services/admin_task_service.py
                       ├─ Task + User + Project 安全投影、过滤、分页
                       ├─ 取消意图 + AuditOperation 同事务受理
                       └─ 现有 TaskEngine 的取消 drain / handler 收尾
```

- 普通 `/tasks`、owner helper、个人任务 store/输出组件不变成跨用户入口。
- 管理路由只编排参数/依赖，ORM 查询与授权复核属于 service；每个路由使用 AdminUser。
- 新取消协调逻辑可置于 `task_engine/cancellation.py`，由现有引擎持有、启动/停止和登记工作；不创建新调度系统、通用 outbox 或独立服务层框架。
- 复用 TaskKind/TaskStatus、真实 actor、AuditOperation、安全错误处理、现有标签与 StudioPage；只为实际不同的数据面新增 DTO。

## 2. 数据库与时间

在当前 head `0025` 后新增迁移 `0026_admin_task_monitoring`，Task 增加三个可空字段：

| 字段 | 用途 |
| --- | --- |
| `queued_at`（UTC datetime） | 新任务/本轮恢复入队的可信起点；新建 ORM 默认当前 UTC，resume 成功时同事务刷新 |
| `admin_cancel_operation_id`（UUID 字符串） | 待完成取消请求的持久化标记，与受理审计 operation_id 相同 |
| `admin_cancel_requested_at`（UTC datetime） | 受理时刻，不使用 updated_at 推断 |

取消字段成组写入/清空，`operation_id != null` 表示尚未安全完成收尾；原 Task.status 不新增第八种状态。原因/actor/原状态属于受理审计快照，不在 Task 重复存储。只要所有 finalize/recover 边界在真实状态提交中原子清标记，就不需要再增加 requested_status：不会留下“late paused 赢家已提交但仍需取消”的歧义。该不变量不可通过外层事后清理代替。

新建 Task 的 ORM 默认覆盖通用创建、自动/批量分析及 KB 直接插入；所有 paused→queued 入口均须核实后刷新 queued_at。已有记录不能可靠重建本轮入队起点，保留 null 并显示“未记录”，不使用 created_at/updated_at 猜测或填零。

- `queued_seconds` 只在 queued 且 queued_at 有效时计算；`running_seconds` 只在 running 且 started_at 有效时计算，均使用同一响应 as_of、UTC、下限 0。
- started_at 仍为最近一次实际领取，不把暂停等待或历轮耗时说成累计运行时长。创建/开始/结束原始时间另外展示。
- 需要时给待处理取消标记加窄索引；不改变 `uq_tasks_active_resource` 的范围，前提是第 5 节的资源屏障完整实现。
- 新增可空列/索引，**不因本任务重建 SQLite tasks 父表**，不损坏生成日志与智能体引用；不把实例控制数据加入作品备份。
- 安全降级：有 pending 标记时拒绝 downgrade。部署应先停旧进程、升级再启动，不支持新旧调度器共跑；回退必须先完成/明确处理取消意图、保留审计，再移除新增字段。

## 3. 读取 API 与安全投影

前缀 `/api/v1/admin/tasks`，时间参数带时区、成对出现、满足起点 < 终点，规范为 UTC 半开区间 `[from_at,to_at)`。

| 方法/路径 | 输入 | 输出 |
| --- | --- | --- |
| GET 空路径 | `q?≤200`（用户名/作品标题字面量）、`owner_user_id?`、`project_id?`、`task_id?`（正整数）、`kind?`、`status?`（重复参数多选）、`from_at?/to_at?`（创建时间）、`sort=created_desc\|queue_longest\|running_longest`、`page=1`、`page_size=20`（1–100） | `{items,total,as_of,counts}` |
| GET `/{task_key}` | UUID | 安全 `AdminTaskDetail`，含 as_of，不含生成内容 |
| POST `/{task_key}/cancel` | `{confirm:true, reason}`，reason trim 后 1–200 字符，拒绝纯空白/控制字符 | 202 `{operation_id,audit_log_id:null\|id}`，仅表示请求受理/已有同一 pending 请求 |

`AdminTaskBrief` 明确白名单：

- `id`、`task_key`（statistics_key 的管理端专用名称）、`kind`、固定展示标题/类型、`status`、`progress`。
- `owner_user_id/owner_username`、`project_id/project_title`、作品生命周期 `active|deleting|recycled|none`、可空 chapter_id。
- `stage`（安全模板）、`error_code/error_message`（固定分类与说明）、`created_at/queued_at/started_at/finished_at`。
- `queued_seconds/running_seconds`、可选前方排队记录数（注明非实际开始顺序）、`cancellation_pending/cancel_requested_at`、`can_cancel`。

详情不额外开放私有数据。禁止用 `TaskOut.model_dump()` 再删几个键；输入、结果、原始标题、错误、输出、checkpoint、resource_key、serial_key、模型/Skill/MCP 配置都不进入 DTO。

### 安全规则

- 管理端标题用“类型标签 + #编号”，不直传可能含智能体指令的 Task.title；搜索也不能借原始 title/input/error 形成信息侧漏。
- 阶段采用有限固定文案及严格“固定文字 + 数字”模板；私有插件调用仅显示“调用工具”，未知阶段回退为状态文案。不得把宽松截断当安全白名单。
- 错误只对可靠的内部固定值/有语义来源的错误分类，无法可靠判断则 `execution_failed / 执行失败`。不从任意上游原文猜测并透传诊断细节；不在管理 API 中修改普通所有者看到的原始错误。
- 活动进度从匹配 task_key 的 Hub 读取，阶段仍经安全投影；错误/状态以 Task 为事实来源，不能把 closed Hub 的旧数据覆盖新 UUID。
- 查询现存 Task，包括仍有 Task 行的回收站/删除中作品；普通用户生命周期 helper 不改。只读不写标记、初始化统计或创建 Hub/模型请求。极旧缺 UUID 数据不可按整数 ID 冒险处置。

### 查询一致性

同一过滤 CTE 生成当前候选；状态聚合子查询与分页子查询在**同一条 SQL 语句**中组合，空页也通过聚合侧保留 total/counts，避免 PostgreSQL READ COMMITTED 下多条查询得到互相矛盾的响应。只选择必要元数据列、原始 stage/error 的服务端投影来源，不读取 input/output/checkpoint/原始标题。counts 覆盖过滤后所有页，明确“当前筛选下的现存任务”；不宣称跨 HTTP 请求冻结快照。分页排序稳定，以 ID 兜底；时长排序先当前相应状态、有效起点、最早时间，再 ID，未知时间置后。

仅整数 ID 不是定位凭据；详情、取消和后续异步写都绑定 task_key。UUID 消失为 404，不复用旧 Task 对象去加载同整数 ID 新对象。

## 4. 取消受理与审计

### 4.1 接受事务

1. `AuditOperation(... action=task.cancel_request, target_type=task, include_own_project=True)`；当前启用管理员必须显式复核，不能让 AuditOperation 的“关闭采集”退化成放行业务。
2. 按 UUID 加载并核实真实 Task、owner 与作品元数据，捕获安全 primitive 快照；使用固定任务标签，不调用会拷贝原始 Task.title 的通用 target 默认行为。
3. 已有 pending 返回同一 operation_id，不再次 signal/abort/记录一条新受理；若对应审计被显式清理，audit_log_id 可空。结束任务拒绝 409；不存在 404。
4. 条件 UPDATE 绑定 `(id, statistics_key, 当前 OPEN 状态, marker is null)`，写 operation_id 和 requested_at；命中后与受理成功审计通过 audit.commit 同事务提交。竞态失败重读原 UUID 决定明确反馈，不跨目标重试。
5. **提交确认前不产生取消信号或 handler 副作用**；提交后唤醒后台 drain。响应只说“取消请求已受理”，通过重新读取 Task 获知实际状态。

同事务保证的是“持久化受理意图 + 受理审计”，不是跨请求、执行体、独立清理事务的一次性终态原子取消。提交确认丢失/响应失败时不能追加相反的受理失败；客户端先刷新，后台按已持久化标记处理。

### 4.2 审计契约的局部扩展

`schemas/audit.py` 与 TS 镜像新增 `task.cancel_request` / `task`，标签为“请求取消任务 / 受理成功”，不能让全局 result=success 被误解为任务已取消。AuditSummary 仅增加明确有界的 task_key、task_kind、accepted_from_status 和 cancel_reason；最后一项是本任务专用、管理员主动填写的自由文本例外，不开放任意 JSON/其他自由文本键。

原因按纯文本显示，提醒不填写密钥或正文；不能承诺对任意自由文本可靠自动脱敏。失败审计如保留取消信息，只保留已校验请求与安全目标的白名单快照，不能保存未提交 after 值。记录真实 actor、owner/作品；本人作品同样采集，不因 Task 清理级联删除，仍遵守原显式审计日期清理机制。

新增边界的未知异常固定安全 500；日志只含操作 UUID/任务 UUID/是否提交等安全标识，不能把含 Task 输入或 SQL 参数的异常送入原始 traceback 日志。

## 5. 取消执行、恢复与资源屏障（核心不变量）

取消 drain 由引擎持有，按 `(task_key, operation_id)` single-flight，独立于 HTTP 请求生存。接受后立即唤醒；启动及调度兜底检查 pending，但不在 scheduler 主循环串行等待每条最多 10 秒的收尾。每次独立 session，等待前释放请求/项目锁；实际清理全过程纳入 cleanup_scope/project_work。

### 5.1 queued 与已暂停任务

- 扫描和 `_claim_and_run` 的条件 UPDATE 都拒绝 pending queued；不能仅过滤扫描或依赖内存集合。
- 管理取消路径保留原 queued/paused 状态，先完成幂等必要清理，再在同一最终事务写 cancelled、唯一结束时刻、独立统计并清 pending 字段。**不能继续先写终态、释放资源索引，再重试旧 abort。**
- queued 保持活动资源键/范围锁直到安全完成。真实 paused 不占并发，不能为了取消抢新任务的资源；当前 agent/book.import 暂停 abort 无额外资源回滚，batch 暂停前 current 已清空，必须以测试固化。
- abort 或状态写失败则保持原状态及 marker，固定提示“取消收尾尚未完成”，由同一可信意图重试；不伪造已取消或任意复原后来任务状态。

### 5.2 running

- 复用 `cancel_running(task_id, statistics_key=...)`，不对同执行体二次 `.cancel()`，不打断实际 SQLite INSERT/RETURNING 保存游标。
- 10 秒只是等待窗口，未结束仍显示取消中。原 finalize 在真实状态 + 统计的**原提交**中清 marker：取消前保存成功则保留 succeeded；late paused 获胜则保留 paused，不再追加旧 abort。
- 存在管理员 pending 时，abort 失败不允许写会释放资源屏障的假 cancelled；保留 running + marker，退出后的孤立执行记录可安全收尾，不重放模型任务。状态保存失败也保留该意图。
- 提交确认丢失按同 UUID 重读事实；不能重复统计、将成功当成待 abort 或由外部 drain 事后猜测 late paused。
- finalize/recover 若先读 marker 或依据 abort 成败选择分支，最终 UPDATE 必须绑定所观察的 marker（含 null）与原 UUID/状态，或先取得真实行写锁再重读；不得让管理员受理在“读标记→写终态”之间插入后被旧分支忽略。条件未命中须重读并按新意图处理，不跨目标重试。

### 5.3 其他入口与生命周期

- 普通 owner cancel 遇 pending 委托同一 drain；resume/finish 不得覆盖 pending。所有普通 queued/paused/孤立 running 的终态 UPDATE、resume/finish 的状态 UPDATE 必须 CAS `admin_cancel_operation_id IS NULL`；命中 0 时重读原 UUID 并委托/拒绝，不能只在 UPDATE 前做一次检查。其他 Task 状态写入/跳过/自动继续入口同样检查是否会绕过该屏障。普通权限、未涉及管理意图的取消语义不变。
- 清理、裁剪、单删和删号不能移除 pending 记录，包含无作品任务的删号边界；完成后恢复原正常保留机制，不新增历史归档。
- 项目 stop/purge/expire 必须等待 pending 与已登记 drain 真正排空；不能只因 OPEN=0 或某状态已 cancelled 就删除。保留已有父行屏障与超时反馈。
- 重启原 running 按既有规则 interrupted，在幂等清理、真实状态及统计提交中清 marker；queued/paused 的已受理意图继续收尾，不续跑 LLM/MCP。
- 正常不产生“真实终态 + 未清 marker”。启动扫描仍覆盖这些异常组合以安全暴露；不得对 succeeded 盲目 abort 或直接删掉恢复依据。
- shutdown 停接新工作后等待真实执行链和已登记清理再释放数据库，不把请求取消传播到独立收尾协程，不以调大锁超时/WAL/无限重试替代资源顺序。

## 6. 前端交互与数据所有权

- RequireAdmin 下懒加载 `/admin/tasks`，补 AppLayout 桌面/移动导航、账户菜单与面包屑；首页增加“查看现存任务/失败/中断”入口，不改累计统计含已移除任务的口径。
- 桌面紧凑列表 + 筛选 + 当前筛选计数；移动 375px 用卡片、可换行的操作与可折叠筛选，不撑宽整个页面。详情 Drawer 只安全元数据，不复用 TaskOutput/TaskResult/TaskPanel。
- 创建时间范围按浏览器本地日历选择、转 UTC 半开区间，展示时区；列表状态多选、用户选择/精确作品 ID 与创建时间可组合。默认创建倒序，时长排序有明确名称与 null 说明。
- 页面专属 controller 按当前 actor ID 管理请求所有权；可复用 startDashboardPolling 的 interval 参数设 5_000，不修改其 60 秒默认。自动/手动刷新不重叠，慢请求不被每个 tick 无限取消；hidden 暂停定时，visible 恢复更新。
- 筛选/分页/详情目标变化取消旧请求并递增代次；切换账号、401/403、卸载清空敏感页面数据、取消计时器及监听。一般读取失败保留旧数据但显式过期和上次成功时刻，不用零填补未加载数据。
- 点击取消先显示目标用户/作品/类型/当前状态，说明已保存内容和已执行效果保留、最终状态以安全收尾为准；必填 1–200 字原因 + 明确确认后提交。pending/提交中禁止重复提交；详情目标更换/关闭不允许旧响应恢复弹窗。
- POST 返回后刷新列表和目标；202 提示已受理，pending 展示“取消中”，最终按实际 cancelled/succeeded/paused/interrupted 展示。网络结果不确定先刷新，不自动重复提交；404 说明任务已移除，409 提示已结束/状态变化并刷新。
- 不向个人 useTasksStore 注入他人任务，也不因为管理员查看而订阅普通输出/SSE；类型/状态标签可复用，未知值有安全回退。

## 7. 错误矩阵

| 场景 | 行为 |
| --- | --- |
| 未登录/禁用/会话失效 | 401，页面清空，停止轮询 |
| 当前非管理员/已降权 | 每个接口 403，清空并停止，不仅隐藏按钮 |
| 参数越界/非法枚举/无时区或错误时间对/confirm 非 true/空原因 | 422，无取消副作用 |
| Task UUID 不存在或已移除 | 404，不改同整数 ID 新任务 |
| 结束状态取消 | 409，固定安全说明并刷新 |
| 同 UUID 已 pending | 202 回同一受理信息，不重复操作 |
| 审计受理失败 | marker 回滚，无信号/abort，安全失败记录按既有规则尽力保存 |
| 受理成功但等待未完成/清理失败 | 202 事实不反转，Task 保留 pending，展示未收尾，不造终态 |
| 读取临时失败 | 旧数据标过期、可重试，不显示假零 |

## 8. 风险、验证与延期

必须以真实双 session、事件/数据库屏障验证领取与受理竞争、原子保存、重启、UUID 复用和清理顺序；不能只测 HTTP 202 或用 sleep 凑时序。具体执行与阻断条件见 implement.md。

本任务不承诺任意进程强杀后精确重建“正文原子保存已提交、Task finalize 尚未提交”的赢家；那是现有引擎更大的跨服务事务边界。仍按原启动中断语义、不重放模型、不伪造撤销。SQLite 验证不能冒充真实 PostgreSQL 并发验收，未验证的环境必须如实记录。

技术层的核心风险是旧 abort 误伤新资源、后台清理丢失与假同事务审计。任何实现无法证明第 5 节不变量时必须回到设计，不能通过外套日志或终态后盲重试交付。
