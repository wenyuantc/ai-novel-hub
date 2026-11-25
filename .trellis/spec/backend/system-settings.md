# 管理员系统设置与运行时覆盖

## 1. 范围 / 触发

适用于七项业务运行配置、`SystemSettingsManager`、`/admin/system-settings`、公开开关同步与设置审计。修改配置读取、部署优先级、任务/MCP执行或管理表单时遵守本契约。各应用分别维护已确认设置的缓存，同库实例使用相同的部署基线。部署条件见[部署与迁移](./deployment-migrations.md)。

## 2. 签名与存储

```python
# app/services/system_settings_service.py
SystemSettingsManager(settings, sessionmaker, notify, *, poll_interval)
async def load(self) -> SystemSettingsOut
async def update(self, actor_id: int, request: SystemSettingsUpdate) -> SystemSettingsOut
async def close(self) -> None

# app/schemas/system_settings.py
SystemSettingsUpdate(revision: int, changes: SystemSettingsChanges)
```

`app.state.system_settings` 管理应用注入的**同一Settings对象**。`0027_system_settings`（父0026）新增实例级 `system_settings`：固定id=1、revision≥1、overrides JSON、last_operation_id、UTC updated_at；无行表示revision=0/无覆盖。无业务FK，不随小说导入导出，也不加入仅容纳项目相关表的NOT_EXPORTED。

GET需settings.read、PATCH需settings.update，响应no-store，前缀 `/api/v1/admin/system-settings`：

| 方法 | 请求 / 响应 |
| --- | --- |
| GET | `{revision,updated_at,items}`，每项含 key/value/source/deployment_value/deployment_source/default_value/overridden/requires_restart=false |
| PATCH | `{revision,changes:{七项可选字段}}`，成功返回同一投影 |
| GET `/drain-status` | `{enabled,sampled_at,queued,running,paused,finalizing,active_backups,workers}`，需要 `settings.read`。每个 worker 含 `hostname,pid,started_at,heartbeat_at`，时间为 UTC。 |

布尔键为 `registration_enabled`、`auto_analyze_enabled`、`mcp_enabled`、`task_admission_closed`。正整数键为 `task_max_concurrency`、`task_max_per_user`、`open_api_rate_limit_per_minute`。用 SystemSettingBoolItem/IntItem 区分，来源枚举 admin/deployment/default。公开 auth/config 两项为 registration_enabled/mcp_enabled，no-store。

## 3. 契约

### 取值与写入

- 后台稀疏覆盖 > 启动时捕获的部署基线 > Settings 代码默认。来源在发布前按原始 model_fields_set 捕获，显式应用注入也属于 deployment。存储使用七项白名单。
- PATCH两层extra=forbid；省略保持、false明确关闭、null删除覆盖恢复部署值，changes不能为空。严格布尔/整数，拒绝布尔冒充整数及字符串/浮点；正整数最小1；开放API限额还须≤2_147_483_647（数据库Integer计数边界，部署基线同样fail-fast），不可存入超大值再让后续认证溢出500。不要求单用户≤全局，两级约束共同生效。
- 管理写锁序为既有 _lock → 共享 AuthorizationRuntime → auth DB gate → User 升序 → 设置资源，自有会话及 revision 条件 UPDATE（首次唯一 INSERT），真实管理员和成功审计同事务。先确认 commit，后无 await 同步发布白名单字段，再 notify 调度。
- 正常失败回滚不发布。提交确认丢失重读持久状态，保留既有审计的真实结果，不盲重发旧候选。自有shield写工作不因HTTP调用者取消被拆断，关停close等待写任务；无法确认时settings_confirmed=False暂停新准入，dispatch直接返回避免热循环；即时重读仍须处于同runtime，后台恢复确认后再开放。load/reload/recovery所有发布路径共用runtime，禁止继续覆盖，不回退默认或宣称离线一致。
- 启动时在 bootstrap 和任务恢复前 `load()`。管理器自有的周期读取独立于任务引擎，空闲应用也会刷新。普通消费者最多使用一个调度周期内确认的设置，过期时在自己的事务中读取有效值。保存后可经内部通道请求其他进程重读库内 revision；收包、定时重载和本地发布不会再次广播。相同 revision 和覆盖内容无需再次发布。回执迟到不得重新延长旧设置的有效期。显式 load/close 的夹具与应用 lifespan 都要收回轮询和在途写入。
- 审计 action=system_settings.update、target_type=system_settings、固定目标。settings_changes 最多七项，仅本动作可用。key/before_override/after_override/before_effective/after_effective/before_source/after_source 均类型受限，失败不保留未提交 after。禁止带入密钥、原始请求或异常。
- `0070` 兼容已有稀疏设置。无 `task_admission_closed` 覆盖时使用部署基线 `TASK_ADMISSION_CLOSED`，代码默认 `false`。降级检查当前覆盖与系统设置审计，存在该键时保留数据并拒绝降级。

### 真实消费

- 注册先检查开关，再计算密码哈希，最后在建账号事务取得授权门并重查持久值。关闭先提交则返回 403，无新用户、登录记录或 Cookie。登录、管理端建号和 bootstrap 沿用各自入口。
- 全站硬顶与用户默认值由领取事务读库，用户显式限额优先于默认值。用户限额为零时继续排队，显式不限仍受全站硬顶。降额保留已经开始的执行。见[用户配额](./rbac-quotas.md)。
- enqueue_auto及batch后续分析阶段使用最新开关，已排队/运行/手动分析不取消；项目auto_analyze仍需同时开启。
- MCP 插件入口、实际写入和出站业务请求各自核对持久开关。公开配置从数据库返回两个开关，继续发送 `Cache-Control: no-store`。传输与智能体调用的锁边界见 [MCP 插件规范](./skills-mcp.md)。
- API Key认证每请求使用最新限额，不清空rate_count/rate_window，UTC分钟/429/Retry-After保持原契约。

### 后台任务排空

- `task_engine/drain.py::require_task_admission_open(db)` 读取共享持久开关。服务端从既有授权写屏障内核验并提交同一根事务。新建、重试、继续及直接写入排队状态的业务共用检查。锁外验证真实身份后的短读可提前返回，锁内判定覆盖翻转。一键分析整批共用一个根事务及一次提交。
- 暂停受理期间，已领取、已排队的任务照常执行。自动分析、分析到向量索引及拆书内部派生继续按原执行轮次、收据、授权和业务配置校验。人工记忆等已提交操作的可选后台索引跳过。富文档批量采纳会继续任务，所以在正文写入前检查。单章采纳可完成，自动分析未排队时返回提醒。
- 提案启动任务遇到排空时保持待确认。智能体纯决定提交后若自动续跑被暂停，轮次保留待审核。整轮接口可返回 `notice`，会话在无未确认提案且可写时提供继续入口。继续仍需通过原轮次检查，取消及适用的提前结束照常可用。
- 设置读写和新的计划备份受理共用授权写屏障。暂停时保留计划到期时间，恢复后按原去重规则处理。手动备份和已受理备份继续。
- 排空状态的开关、三类 Task 计数、未被活动 Task 原轮次覆盖的执行槽、活动备份由单条主库语句读取。终态回调仍持执行槽，避免在派生前提前显示零。存活状态用 PostgreSQL 主库心跳或 SQLite 配对侧库心跳。管理员可见主机、PID 和时间信息，内部监听地址用于服务端归并。

### 前端

`/admin/system-settings` 使用RequirePermission(settings.read)+懒路由，保存另需settings.update；AppLayout桌面/移动/账户/面包屑同步入口。表单编辑暂存，逐项恢复确认后也是待保存，点击保存才发PATCH。显示来源/部署/默认/无需重启及不打断边界。dirty刷新不覆盖编辑，409/网络结果未知保留草稿并要求先刷新核实，不自动重发；账号/权限/卸载使旧响应失效，撤下管理数据及本页拥有的确认框。

`utils/publicConfig.ts::PublicConfigController` 独立刷新公开配置，不重复init/me或改变登录态。App安装可见30秒轮询、回前台立即刷新，focus/visibility短间隔去重；慢请求single-flight、失败保持最后成功值。保存成功apply使更早响应失效，不能倒退新值。`utils/visiblePolling.ts`仅负责调度；dashboard包装保留60秒、任务监控保留5秒。StrictMode effect setup→cleanup→setup不得遗留busy/永久Skeleton。

排空状态使用独立请求，页面可见时每 5 秒更新，保存设置后立即重读。迟到响应、切换会话及撤权受请求代次保护。只在排空开启、五项活动计数均为零且状态有效时显示「已排空」。发现另一管理员修改开关后，复用原设置读取与 revision 校验同步有效值，保留草稿。公开配置同步不会撤销只读管理员的设置请求。受理错误沿用 `ApiError.detail`。编辑要求和智能体输入在提交成功后才清空，批量弹窗同样在成功后才关闭。

## 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 未登录/会话过期/禁用 | 管理401，不读取配置 |
| 非管理员/等待写锁后已降权 | 403，不持久修改 |
| 未知字段、空changes、非严格类型、整数<1 | 422，整批无副作用 |
| revision过期/首次行竞争 | 409，不静默覆盖 |
| 审计或业务提交失败 | 安全500/原业务错误，回滚且不发布 |
| 提交确认丢失 | 安全错误但重读真实持久结果，不追加相反失败审计 |
| 持久值坏/暂不可读 | 安全固定说明，不输出Settings/SQL参数或静默启用默认 |
| 开关开启后新建、重试、继续 | 503 `task_admission_closed`，`detail` 为「系统正在维护，暂不受理新任务，请稍后再试」。操作关联的资源、任务及断点保持原状。 |
| 排空状态或租约无法核验 | 503 或安全读取错误，页面标记上次状态已过期。 |

## 5. 正例 / 基线 / 反例

- 正例：后台保存mcp_enabled=false，接口立即403；正在建立连接但未发工具的Worker也不再调用。已经发出的一次调用可以结束。
- 基线：无覆盖时部署task_max_concurrency=4，保存3后后台优先；恢复null后回到4。下次用部署5重建应用后仍无该覆盖则采用5。
- 反例：先setattr再commit，审计失败却已经关闭注册；或用get_settings.cache_clear后替换app.state，任务仍用旧引用。
- 排空正例：A 保存开关为开启，B 的下一次受理读取持久值。原回调未完成时管理端仍有收尾计数。
- 排空基线：默认关闭，待审核任务按已有规则续跑，计划备份到期照常受理。
- 排空反例：只检查发布缓存导致另一个进程在重载前继续受理，或者任务刚写成功时忽略仍占用的执行槽。

## 6. 必需测试

- `tests/test_system_settings.py`：七项安全纯读、false/partial/reset 真实注册、非法批次、权限、同 revision 唯一赢家、新应用重启与新部署、审计故障及未知提交、摘要类型和动作约束。
- `tests/test_system_settings_runtime.py`：Gate控制两级并发降/升、普通及batch自动分析、同分钟限额计数、MCP连接中关闭/Worker出队关闭/已发调用完成。
- `tests/test_system_settings_cross_process.py` 与 `tests/test_system_settings_publication_races.py` 在独立应用之间交错设置提交、注册写入、迟到回执和限流请求，验证每种先后顺序下的 HTTP 回包和实际副作用。
- `tests/test_system_settings_migrations.py`：0026→0027→0026，固定单行/正revision、旧业务保留；原head/base与项目备份反射仍通过。
- 前端API/controller/publicConfig/auth/结构测试：null/false、Cookie/AbortSignal、409/权限/脏编辑/未知结果、StrictMode双setup、慢请求/同步抛错清single-flight、30秒/隐藏/前台/focus/时钟回拨、保存后旧响应不反写、本页确认框清理。
- 真实浏览器补保存/逐项恢复/来源、匿名与普通用户/降权、两会话公开同步、375px/浅暗/错误恢复。node SSR不是交互验收。
- `tests/test_task_drain*.py` 覆盖开关与受理两种竞争顺序、受理及派生无部分变更、回调窗口与备份占用、进程心跳投影、SQLite 和 PostgreSQL 迁移。真实双进程和 Chrome 验证跨进程读取、收尾与输入保留。

## 7. 错误与正确写法

```python
# 错误：先热更新；又只改请求对象，运行中任务仍持有旧配置。
app.state.settings = new_settings
await db.commit()

# 正确：持久成功后，管理器同步发布原共享对象的白名单。
await audit.commit()
return self._publish(revision, candidate, now)  # 此处无await
```

```python
# 读取进程缓存无法判定同库另一进程已保存的值。
if engine.ctx.settings.task_admission_closed:
    raise TaskAdmissionClosed()

# 受理方使用现有根事务和共享持久值。
await authz.require_authenticated(db, actor, write=True)
await require_task_admission_open(db)
# 后续模型受理、资源写入和入队仍使用 db，提交时一同释放屏障。
```

```typescript
// 错误：后台轮询重置登录态，或迟到请求覆盖保存结果。
await useAuthStore.getState().init()

// 正确：公开配置独立刷新；applyConfig使更早请求失效。
useAuthStore.getState().applyConfig(publicConfig)
```
