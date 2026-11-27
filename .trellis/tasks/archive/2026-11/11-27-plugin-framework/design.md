# 插件框架：技术设计

> 需求见同目录 `prd.md`（F1–F15、AC1–AC13）；决定编号 D1–D15 见父任务 `prd.md`。

## 1. 设计原则

1. **服务端持久值说了算。** 每个插件入口在自己的事务里读数据库里的启用状态，不读进程缓存。
   这是本仓库已有的做法：任务受理读持久的排空开关（`backend/app/services/task_service.py:547`），
   MCP 在入口、写入、出站三处各自核对（`backend/app/services/mcp/settings_gate.py`）。好处是双实例无需额外同步即可一致（F13）。
2. **登记在启动时完成，判定在请求时完成。** 插件有哪些是代码事实（进程内只读注册表）；插件开没开是数据事实（数据库）。
3. **核心不认识具体插件。** 核心只依赖注册表接口；新增插件 = 新增目录 + 在登记处加一行（F3）。
4. **只做第一个真实插件用得到的接入点。** 本子任务：接口、后台任务、前端页面与导航。备份规则、智能体工具等在短剧子任务里按需补。

## 2. 总体结构

```
backend/app/plugins/
  __init__.py     # 对外入口：REGISTERED（内置插件清单的唯一登记处）、load()
  manifest.py     # PluginManifest 数据类与校验
  registry.py     # 进程内只读注册表：登记、查询、kind → 插件 的反查
  state.py        # 启用状态：读取、带修订号的更新、门禁函数
  deps.py         # FastAPI 依赖 require_plugin(plugin_id)
backend/app/models/feature_plugins.py      # FeaturePluginState
backend/app/schemas/feature_plugins.py     # 管理接口与公开投影的 schema
backend/app/api/v1/admin_plugins.py        # /admin/plugins
backend/alembic/versions/0080_feature_plugins.py

frontend/src/plugins/
  types.ts        # FeaturePlugin 类型
  registry.ts     # PLUGINS（前端唯一登记处）与纯函数：路由、导航的合成
  RequirePlugin.tsx   # 路由守卫：未启用时显示提示
frontend/src/api/adminPlugins.ts
frontend/src/pages/AdminPluginsPage.tsx
```

真实插件以后放在 `backend/app/plugins/<id>/` 与 `frontend/src/plugins/<id>/`，各自内部再分 api / services / models / 页面。
本子任务不创建任何真实插件目录。

## 3. 后端

### 3.1 清单与注册表

```python
# app/plugins/manifest.py
@dataclass(frozen=True)
class PluginManifest:
    id: str                 # ^[a-z][a-z0-9_]{1,31}$，全站唯一；同时是任务类型前缀
    name: str               # 界面显示名，≤ 50
    description: str        # ≤ 300
    version: str            # ≤ 32，仅展示
    routers: tuple[APIRouter, ...] = ()
    task_handlers: tuple[TaskHandler, ...] = ()
```

`PluginRegistry(manifests)` 在登记时校验并在失败时抛异常（应用启动失败，F2 / AC10）：

- `id` 格式合法且未被登记过。
- 每个 `task_handler.kinds` 中的类型都以 `f"{id}."` 开头，长度至多 32，与其他类型不重复，并避开核心任务的命名空间（如 `wizard.*`）。
- 每个 router 的前缀与实际接口路径均位于 `/plugins/{id}` 的完整路径分段下；只接受可附加 FastAPI 依赖的 HTTP 接口，以保证每条路由都经过启用检查。

注册表提供：`all()`、`get(plugin_id)`、`plugin_of_kind(kind) -> str | None`。登记完成后只读。

`app/plugins/__init__.py` 的 `REGISTERED: tuple[PluginManifest, ...]` 是内置插件的唯一登记处，本子任务交付时为空元组。
`load(extra=())` 把 `REGISTERED + extra` 逐个登记，`extra` 只给测试用来注入示例插件。`create_app()` 增加一个可选参数把 `extra` 传进去，
生产路径不传。

插件的 ORM 模型由插件包自己定义，`load()` 导入插件包时即进入 `Base.metadata`；迁移仍写在唯一的线性链 `backend/alembic/versions/` 里
（表始终存在，与启用状态无关，见父任务证据）。

### 3.2 启用状态

新表（迁移 `0080_feature_plugins`，父 `0079`）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `plugin_id` | `String(32)` 主键 | 与清单 `id` 对应 |
| `enabled` | `Boolean` 非空 | |
| `revision` | `Integer` 非空，`CHECK revision >= 1` | 每次变更 +1 |
| `updated_at` | `DateTime(timezone=True)` 非空 | |
| `last_operation_id` | `String(36)` 可空 | 关联审计记录，与 `system_settings` 做法一致 |

- 没有行 = 从未被操作过 = 停用，`revision = 0`（F6）。
- 代码里已不存在的 `plugin_id` 的行被忽略，不报错（插件将来被移除时不需要数据迁移）。
- 与作品无关，不进作品备份；按项目规范登记到作品备份注册表的 `NOT_EXPORTED` 不适用（那只容纳作品相关表），
  做法与 `system_settings` 相同，并补上对应的反射测试断言。整库原生快照自动包含这张表，SQLite 恢复时按版本校验表和列。

```python
# app/plugins/state.py
async def is_enabled(db, plugin_id: str) -> bool            # 主键查询；无行 → False
async def enabled_ids(db) -> list[str]                      # 已登记且启用的插件，按登记顺序
async def require_enabled(db, plugin_id: str) -> None       # 未启用 → PluginDisabled
async def set_enabled(db, actor, plugin_id, *, revision: int, enabled: bool) -> PluginStateOut
```

`PluginDisabled` 是 `AppError` 子类：HTTP 403，`detail = "功能插件未启用"`，`extra = {"code": "plugin_disabled", "plugin": plugin_id}`（F8）。
选 403 而不是 404，是为了与 MCP 关闭时的行为一致，并让前端能据此刷新开关状态。

`set_enabled` 的事务顺序沿用系统设置的管理写约定（`.trellis/spec/backend/system-settings.md` 第 3 节）：

1. `authz.require(db, actor, "plugins.update", write=True)` 取得授权写屏障。
2. 未登记的 `plugin_id` → 404。
3. 在各实例共用的授权写门内锁定并重读状态行，核对修订号后写入（首次为 INSERT，后续为 UPDATE）。修订号不符 → 409 `{"code": "stale_revision"}`（F5 / AC5）。状态未变化（已是目标值）同样走修订号校验后返回当前值，不写审计。
4. 同一事务内写审计：动作 `feature_plugin.enable` / `feature_plugin.disable`，目标类型 `feature_plugin`，目标标签为插件名。
5. 停用时，同一事务内把该插件尚未结束的任务标记为待取消（见 3.4），然后提交。
6. 提交确认后，再通知本进程的任务引擎处理这些任务；其他进程靠各自的调度周期与领取时门禁发现。

### 3.3 接口门禁

`app/plugins/deps.py`：

```python
def require_plugin(plugin_id: str):
    async def dependency(db: DbDep) -> None:
        await state.require_enabled(db, plugin_id)
    return dependency
```

`app/api/v1/__init__.py` 在现有的 `include_router` 之后追加一段：遍历注册表，把每个插件的每个 router 以
`dependencies=[Depends(require_plugin(manifest.id))]` 挂到 `api_router` 上。插件 router 的前缀统一为 `/plugins/{id}/...`，
对外即 `/api/v1/plugins/{id}/...`。这样：

- 门禁加在挂载处，插件作者不可能漏加（R1）。
- 依赖的执行先于插件自己的鉴权依赖是否成立，取决于 FastAPI 的解析顺序；因此门禁**不**依赖登录态，未登录访问未启用的插件接口同样得到 403，
  不泄露比「有这个插件但没开」更多的信息。这一点写进测试。
- 插件的上传体积限制等中间件规则（`backend/app/main.py:225` 一带的列表）本子任务不扩展；短剧子任务需要时再让清单贡献。

### 3.4 后台任务门禁

每个应用创建自己的插件注册表。请求、调度器和请求执行者各自在该应用的上下文内运行。核心任务沿用现有注册表，插件任务由同一个 `get_handler(kind)` 从当前应用查找，两个应用的处理器互不覆盖。本人任务列表、详情以及任务类型筛选都识别已登记的插件任务类型。

| 时机 | 事务和行为 |
| --- | --- |
| 新建及重试 | `task_service.create_task` 在持有授权写门禁的受理事务中检查插件状态，随后校验请求、创建并提交任务。重试复用这条路径。 |
| 继续 | `task_service.resume_task` 在同一写门禁内检查插件状态，再将原暂停任务重新入队。 |
| 领取 | 执行者在授权写门禁和队列行锁内读取开关。未启用时写入取消意图并提交，然后唤醒取消处理。已有取消意图的任务无法通过领取条件更新。 |
| 运行中的事件 | 插件输出在事件核验事务中检查原 UUID、入队轮次和执行者对应的取消意图。即使插件随后重新启用，持有者仍会在下一事件或调度扫描时停止这一轮。 |

停用事务从清单收集准确的任务类型，通过 `Task.kind.in_(kinds)` 查找尚未结束的任务。含下划线的插件标识也只匹配自己的任务。按任务行锁顺序留下独立的 `admin_cancel_operation_id` 和受理时间，保存已有意图，一并提交开关与停用审计。提交后通知本机取消处理和对应的远端执行者，后台扫描可以补上丢失的通知。

复用 `AdminCancellationDrain`，由它验证任务原轮次和执行者、协调已启动的执行体、运行处理器的幂等 `on_abort` 并提交终态和统计。排队和暂停中的插件任务也走同一条收尾路径。处理器的回退只释放原任务持有的资源；停用不会清除插件业务数据。若原任务已经提交结果，则沿用引擎原有的终态核验规则。

其余受理检查分布在分析、知识库、导入、富文档、模型和智能体模块，处理各自的核心类型或在准备阶段检查通用维护开关。具体核对结果和真实任务验收见 `backend/tests/test_feature_plugins_tasks.py`。

### 3.5 管理接口

前缀 `/api/v1/admin/plugins`，响应一律 `Cache-Control: no-store`。

| 方法 | 路径 | 权限 | 请求 / 响应 |
| --- | --- | --- | --- |
| GET | `` | `plugins.read` | `{items: [{id, name, description, version, enabled, revision, updated_at}]}`，按登记顺序 |
| PATCH | `/{plugin_id}` | `plugins.update` | 请求 `{revision: int, enabled: bool}`（`extra=forbid`，严格布尔）；成功返回该项；404 未登记；409 修订过期 |

权限目录（`backend/app/core/permissions.py`）新增两项，分组「功能插件」：

- `plugins.read`「查看功能插件」
- `plugins.update`「启用/停用功能插件」，`dangerous=True`，`requires=("plugins.read",)`

超级管理员自动获得（`BUILTIN_ROLES["super_admin"]` 取全部）。其余内置角色不变。权限总数由 40 变为 42，
`.trellis/spec/backend/rbac-quotas.md` 与相关测试里的数量断言同步修改。

### 3.6 对前端公开的状态

扩展现有公开配置 `GET /api/v1/auth/config`（`backend/app/api/v1/auth.py:27`）：`AuthConfigOut` 增加 `enabled_plugins: list[str]`，
由 `state.enabled_ids(db)` 给出。

- 复用前端已有的 30 秒轮询与切回前台刷新（F12），不新增轮询器。
- 该接口无需登录，因此「哪些插件已启用」对匿名可见。信息量仅为插件标识，判定为可接受；
  备选方案是新开一个需登录的接口并复制一套轮询，代价是多一份同步逻辑，本期不选。
- `.trellis/spec/backend/system-settings.md` 里「公开 auth/config 两项」的描述同步更新。

## 4. 前端

### 4.1 插件类型与登记

`src/plugins/types.ts` 的 `FeaturePlugin` 声明 `id` 和两个可选页面列表 `project`、`global`。每个页面有相对路径 `path`、异步组件加载函数 `load`，以及可选的导航配置 `nav: { group, label, icon }`。

清单内的 `path` 从插件标识开始，首个路径段必须完整等于 `id`。注册表统一添加 `features/` 前缀，全局页面挂在 `/features/<id>`，作品内页面挂在 `/projects/:pid/features/<id>`。例如 `id = "sample"`、`path = "sample/editor"` 的页面，登记在 `project` 下时，作品 42 中的链接是 `/projects/42/features/sample/editor`。

`src/plugins/registry.tsx` 的 `PLUGINS` 是前端登记入口。路由和导航使用同一份清单：

- `pluginRoutes(plugins, scope)` 按作用域生成路由，将用 `React.lazy(load)` 包装的页面置于 `RequirePlugin` 内，只有守卫放行后才下载页面模块。
- `withPluginNav(groups, plugins, enabled, projectId)` 根据作品 ID 或全局作用域构造完整地址，仅把已启用插件的入口加入对应导航分组。

### 4.2 状态与守卫

- `stores/auth.ts` 的 `enabledPlugins` 初始为空数组，从公开配置的 `enabled_plugins` 更新。旧服务缺失该字段时按空数组处理。
- `RequirePlugin` 通过 `pluginConfigReady` 区分首次加载与已停用。取得配置后，已启用则渲染页面，已停用则给出「功能插件未启用」提示。
- API 客户端收到带 `plugin_disabled` 的 403 时触发配置刷新。导航复用可见页面的公开配置轮询和切回前台刷新。
- `router.tsx` 分别在全局和作品路由下展开相应作用域的 `pluginRoutes`；两者均使用上述固定前缀。

### 4.3 管理页

- 路由 `admin/plugins`，`RequirePermission permission="plugins.read"` + 懒加载，写法与 `admin/system-settings`（`router.tsx:75`）一致。
- `AppLayout.tsx` 的管理导航增加「功能插件」入口（`can(user, 'plugins.read')`），桌面、移动、面包屑同步。
- 页面：表格或卡片列出插件名称、简介、版本、状态开关、最近变更时间。无 `plugins.update` 时开关只读。
- 启用直接提交；停用弹确认，文案写明「入口将对所有作者关闭，运行中的任务会被取消，已有数据保留」。
- 409：提示「状态已被其他管理员修改」，自动重新读取列表；网络结果未知时不自动重发，提示先刷新核实。
- 成功后对自己立即 `applyConfig`，不等 30 秒轮询。
- 页面离开、账号切换、权限变化时让在途请求失效（沿用管理页现有的请求代次做法）。
- 没有任何已登记插件时显示空状态说明。

## 5. 兼容与迁移

- `0080` 新增状态表，并更新内置超级管理员权限及授权修订号。降级会移除该表并恢复旧权限；如存在插件状态修订、旧版本无法读取的任务或取消审计、插件启停审计、涉及插件权限的角色审计或仍持有这些权限的角色，降级会拒绝，以保留现存数据及审计。
- 不登记任何插件时：`api_router` 不多挂任何路由，任务注册表不变，前端路由与导航不变（F14 / AC11）。
- `AuthConfigOut` 新增字段，旧前端忽略未知字段；新前端对缺失字段按空数组处理。

## 6. 权衡记录

| 问题 | 选择 | 放弃的方案与原因 |
| --- | --- | --- |
| 状态存哪 | 独立表 | 塞进 `system_settings`：它是七项固定白名单，改动面波及其审计摘要与前端表单 |
| 门禁读什么 | 每次读库 | 进程缓存 + 跨进程通知：快一个主键查询，但要自己保证双实例一致，与现有做法相悖 |
| 接口未启用返回 | 403 + 原因码 | 404：更隐蔽，但与 MCP 不一致，前端无法据此刷新状态 |
| 前端如何得知 | 扩展公开配置 | 新开登录后接口：不暴露给匿名，但多一套轮询 |
| 停用时的任务 | 复用现有取消路径 | 让任务自然跑完：实现简单，但违背 D5「正在运行的任务被取消」 |
| 接入点范围 | 接口、任务、页面、导航 | 一次设计齐全：会包含没有真实使用者的接入点，等短剧时很可能返工 |

## 7. 规范与文档

- 新增 `.trellis/spec/backend/feature-plugins.md`（清单、注册表、状态表、三道门、管理接口、错误矩阵、必需测试），登记到 `backend/index.md`。
- 新增 `.trellis/spec/frontend/feature-plugins.md` 或在 `component-guidelines.md` 增补（登记处、守卫、导航合成、管理页），登记到 `frontend/index.md`。
- 更新 `system-settings.md`（公开配置字段）、`rbac-quotas.md`（权限数量）、`admin-backups.md`（表目录）。
- README：功能概览表增加「功能插件」一行；管理与运维部分说明启停行为；目录结构说明增加 `plugins/`。
