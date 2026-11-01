# 管理员系统设置与运行时覆盖

## 1. 范围 / 触发

适用于六项业务运行配置、`SystemSettingsManager`、`/admin/system-settings`、公开开关同步与设置审计。修改配置读取、部署优先级、任务/MCP执行或管理表单时遵守本契约。继续沿用任务引擎已有的单进程部署前提，不提供多worker广播。

## 2. 签名与存储

```python
# app/services/system_settings_service.py
SystemSettingsManager(settings, sessionmaker, notify)
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
| GET | `{revision,updated_at,items}`；六项key/value/source/deployment_value/deployment_source/default_value/overridden/requires_restart=false |
| PATCH | `{revision,changes:{六项可选字段}}`，成功返回同一投影 |

bool键为registration_enabled/auto_analyze_enabled/mcp_enabled；正整数键为task_max_concurrency/task_max_per_user/open_api_rate_limit_per_minute。用SystemSettingBoolItem/IntItem区分，来源枚举admin/deployment/default。公开auth/config仍只有registration_enabled/mcp_enabled，no-store。

## 3. 契约

### 取值与写入

- 后台稀疏覆盖 > 启动时捕获的部署基线 > Settings代码默认；来源在发布前按原始model_fields_set捕获，显式应用注入也属于deployment。只保存六项，不序列化完整Settings。
- PATCH两层extra=forbid；省略保持、false明确关闭、null删除覆盖恢复部署值，changes不能为空。严格布尔/整数，拒绝布尔冒充整数及字符串/浮点；正整数最小1；开放API限额还须≤2_147_483_647（数据库Integer计数边界，部署基线同样fail-fast），不可存入超大值再让后续认证溢出500。不要求单用户≤全局，两级约束共同生效。
- 管理写锁序为既有_lock→共享AuthorizationRuntime→auth DB gate→User升序→设置资源，自有会话及revision条件UPDATE（首次唯一INSERT），真实管理员和成功审计同事务。先确认commit，后无await同步发布六字段，再notify调度；不先发布再存库。
- 正常失败回滚不发布。提交确认丢失重读持久状态，保留既有审计的真实结果，不盲重发旧候选。自有shield写工作不因HTTP调用者取消被拆断，关停close等待写任务；无法确认时settings_confirmed=False暂停新准入，dispatch直接返回避免热循环；即时重读仍须处于同runtime，后台恢复确认后再开放。load/reload/recovery所有发布路径共用runtime，禁止继续覆盖，不回退默认或宣称离线一致。
- 应用启动在bootstrap/任务恢复前load；持久值非法或读库失败不静默忽略。create_all测试不运行lifespan，fixture显式load/close。
- 审计action=system_settings.update、target_type=system_settings、固定目标。settings_changes最多6项，仅本动作可用；key/before_override/after_override/before_effective/after_effective/before_source/after_source均类型受限，失败不保留未提交after。禁止带入密钥、原始请求或异常。

### 真实消费

- 注册关闭不影响登录、管理员创建用户或bootstrap。
- 全站硬顶由同一ctx.settings读取；用户上限由[用户配额](./rbac-quotas.md)统一解析，inherit才取task_max_per_user，limited0禁止新准入、unlimited仍受全站硬顶；降低只停止不符合新限额的后续调度，不取消运行任务，提高notify；原FIFO/资源互斥/取消不变。
- enqueue_auto及batch后续分析阶段使用最新开关，已排队/运行/手动分析不取消；项目auto_analyze仍需同时开启。
- MCP路由require_mcp并非唯一边界：试用/工具发现连接前、连接建立后实际调用前，及智能体Worker出队实际call_tool前均读共享Settings；已发出的call_tool不强杀。不能将一次性bool传入Worker替代实时引用。
- API Key认证每请求使用最新限额，不清空rate_count/rate_window，UTC分钟/429/Retry-After保持原契约。

### 前端

`/admin/system-settings` 使用RequirePermission(settings.read)+懒路由，保存另需settings.update；AppLayout桌面/移动/账户/面包屑同步入口。表单编辑暂存，逐项恢复确认后也是待保存，点击保存才发PATCH。显示来源/部署/默认/无需重启及不打断边界。dirty刷新不覆盖编辑，409/网络结果未知保留草稿并要求先刷新核实，不自动重发；账号/权限/卸载使旧响应失效，撤下管理数据及本页拥有的确认框。

`utils/publicConfig.ts::PublicConfigController` 独立刷新公开配置，不重复init/me或改变登录态。App安装可见30秒轮询、回前台立即刷新，focus/visibility短间隔去重；慢请求single-flight、失败保持最后成功值。保存成功apply使更早响应失效，不能倒退新值。`utils/visiblePolling.ts`仅负责调度；dashboard包装保留60秒、任务监控保留5秒。StrictMode effect setup→cleanup→setup不得遗留busy/永久Skeleton。

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

## 5. 正例 / 基线 / 反例

- 正例：后台保存mcp_enabled=false，接口立即403；正在建立连接但未发工具的Worker也不再调用。已经发出的一次调用可以结束。
- 基线：无覆盖时部署task_max_concurrency=4，保存3后后台优先；恢复null后回到4。下次用部署5重建应用后仍无该覆盖则采用5。
- 反例：先setattr再commit，审计失败却已经关闭注册；或用get_settings.cache_clear后替换app.state，任务仍用旧引用。

## 6. 必需测试

- `tests/test_system_settings.py`：六项安全纯读、false/partial/reset真实注册、非法批次/权限、同revision唯一赢家、新应用重启与新部署、审计故障无发布/无泄露、commit确认丢失无相反审计、HTTP取消后写入发布完整、未知提交后台恢复及坏持久值、摘要类型/动作约束。
- `tests/test_system_settings_runtime.py`：Gate控制两级并发降/升、普通及batch自动分析、同分钟限额计数、MCP连接中关闭/Worker出队关闭/已发调用完成。
- `tests/test_system_settings_migrations.py`：0026→0027→0026，固定单行/正revision、旧业务保留；原head/base与项目备份反射仍通过。
- 前端API/controller/publicConfig/auth/结构测试：null/false、Cookie/AbortSignal、409/权限/脏编辑/未知结果、StrictMode双setup、慢请求/同步抛错清single-flight、30秒/隐藏/前台/focus/时钟回拨、保存后旧响应不反写、本页确认框清理。
- 真实浏览器补保存/逐项恢复/来源、匿名与普通用户/降权、两会话公开同步、375px/浅暗/错误恢复。node SSR不是交互验收。

## 7. 错误与正确写法

```python
# 错误：先热更新；又只改请求对象，运行中任务仍持有旧配置。
app.state.settings = new_settings
await db.commit()

# 正确：持久成功后，管理器同步发布原共享对象的白名单。
await audit.commit()
return self._publish(revision, candidate, now)  # 此处无await
```

```typescript
// 错误：后台轮询重置登录态，或迟到请求覆盖保存结果。
await useAuthStore.getState().init()

// 正确：公开配置独立刷新；applyConfig使更早请求失效。
useAuthStore.getState().applyConfig(publicConfig)
```
