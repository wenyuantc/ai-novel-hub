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

`registry.register(manifest)` 在登记时校验并在失败时抛异常（应用启动失败，F2 / AC10）：

- `id` 格式合法且未被登记过。
- 每个 `task_handler.kinds` 中的类型都以 `f"{id}."` 开头，且未被核心或其他插件占用。
- 每个 router 的 `prefix` 以 `/plugins/{id}` 开头（见 3.3），避免与核心路径冲突。

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
  做法与 `system_settings` 相同，并补上对应的反射测试断言。整库备份的表目录需要加上这张表。

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
3. 按 `revision` 条件写：无行且 `revision == 0` 时 INSERT（唯一约束兜住并发首写），否则 `UPDATE ... WHERE revision = :revision`；
   影响 0 行 → 409 `{"code": "stale_revision"}`（F5 / AC5）。状态未变化（已是目标值）同样走修订号校验后返回当前值，不写审计。
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

登记：`load()` 对每个插件的 `task_handlers` 调用现有的 `task_engine.registry.register`（`backend/app/services/task_engine/registry.py:93`），
并记录 kind → 插件。核心的 `handlers/__init__.py` 不改。

三道门，全部读持久值：

| 时机 | 位置 | 行为 |
| --- | --- | --- |
| 受理（新建） | `task_service` 中紧随 `require_task_admission_open(db)` 之后（`backend/app/services/task_service.py:547`） | `plugin_of_kind(kind)` 非空时 `await state.require_enabled(db, plugin_id)`；与受理在同一根事务，提交时一并生效 |
| 重试 / 继续 | 同文件 `:758` 处的同一检查之后 | 同上 |
| 领取（执行前） | 引擎领取事务内取处理器处（`backend/app/services/task_engine/engine.py:1710`） | 插件已停用 → 不执行，按「已取消」收尾 |

把受理检查做成一个函数 `plugins.state.require_kind_enabled(db, kind)`，其余直接写入排队状态的业务入口
（仓库里凡是调用 `require_task_admission_open` 的地方，共 5 个模块）在引入插件任务时沿用同一函数；
本子任务只改 `task_service` 的两处，因为示例插件只经由标准受理入口建任务，其余入口都不可能产生插件类型的任务。这一点在实现时用 grep 复核并写进测试说明。

停用时取消未结束的任务（F9 / AC6）：

- 不新造终态路径。复用引擎已有的取消机制（所有者取消与管理端取消最终汇入的同一条路径，见
  `backend/app/services/task_engine/cancellation.py` 与 `owner_cancellation.py`）。
- `set_enabled(..., enabled=False)` 在事务内查出 `kind LIKE '{id}.%' AND status IN OPEN_TASK_STATUSES` 的任务，逐个登记取消意图；
  提交后由引擎完成取消。跨实例运行中的任务由持有它的那个实例在下一个调度周期、或处理器下一次产出事件时发现并停止。
- 领取时门禁兜住「停用提交与排队任务被领取」之间的竞争：即使取消登记漏掉了刚入队的任务，它也不会被执行。
- 取消的审计：整个停用操作只写一条 `feature_plugin.disable`，摘要里带被取消任务的数量；不为每个任务单独写管理端取消审计。

**这是本子任务风险最高的部分。** 引擎的取消路径有章节占用、执行槽、收尾回调等既有约束（`engine.py` 四千余行）。
实施计划把它单列为一步，先读代码确认可复用的最小入口，再写实现；若发现现有取消入口都绑定了具体任务类型的前置条件，
回到本设计文档修订后再继续，不绕开。

### 3.5 管理接口

前缀 `/api/v1/admin/plugins`，响应一律 `Cache-Control: no-store`。

| 方法 | 路径 | 权限 | 请求 / 响应 |
| --- | --- | --- | --- |
| GET | `` | `plugins.read` | `{items: [{id, name, description, version, enabled, revision, updated_at}]}`，按登记顺序 |
| PATCH | `/{plugin_id}` | `plugins.update` | 请求 `{revision: int, enabled: bool}`（`extra=forbid`，严格布尔）；成功返回该项；404 未登记；409 修订过期 |

权限目录（`backend/app/core/permissions.py`）新增两项，分组「功能插件」：

- `plugins.read`「查看功能插件」
- `plugins.update`「启用/停用功能插件」，`dangerous=True`，`requires=("plugins.read",)`

超级管理员自动获得（`BUILTIN_ROLES["super_admin"]` 取全部）。其余内置角色不变。权限总数由 38 变为 40，
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

```ts
// src/plugins/types.ts
export interface FeaturePlugin {
  id: string
  /** 挂在 projects/:pid 之下的路由，path 必须以 `${id}` 开头 */
  projectRoutes?: RouteObject[]
  /** 作品内导航：归入已有分组或新分组 */
  projectNav?: { group: string; items: { key: string; label: string; icon: ReactNode }[] }
  /** 作品之外的全局路由与导航（本期示例插件不使用，类型先留） */
  globalRoutes?: RouteObject[]
  globalNav?: { group: string; items: { to: string; label: string; icon: ReactNode }[] }
}
```

`src/plugins/registry.ts`：`export const PLUGINS: FeaturePlugin[] = []`（唯一登记处），以及纯函数：

- `pluginProjectRoutes(plugins)`：把每个插件的 `projectRoutes` 包进 `<RequirePlugin id>` 后返回，供 `router.tsx` 展开到 `projects/:pid` 的 children。
- `pluginGlobalRoutes(plugins)`：同理。
- `withPluginNav(groups, plugins, enabledIds, scope)`：在现有导航分组上追加已启用插件的入口。

插件页面继续用 `lazyPage(() => import(...))` 按需加载，未启用的插件不会下载其页面代码。

### 4.2 状态与守卫

- `stores/auth.ts` 增加 `enabledPlugins: string[]`，由公开配置的 `enabled_plugins` 写入。**初始值为空数组**（与 `mcpEnabled` 初始为 `true` 不同）：
  配置尚未取到时宁可不显示入口。
- `RequirePlugin`：`enabledPlugins` 含该 id → 渲染子路由；否则显示「该功能插件未启用，请联系管理员」的提示（F11）。
  公开配置尚未首次返回时显示加载态，避免先闪一下「未启用」。
- API 客户端收到 `code === "plugin_disabled"` 的 403 时触发一次 `refreshConfig()`，让导航尽快收起。
- `StudioNavigation.tsx:65` 的 `studioNavigation(...)` 增加 `enabledPlugins` 参数并调用 `withPluginNav`；`AppLayout.tsx:50` 传入。
- `router.tsx:119` 的 `projects/:pid` children 末尾展开 `pluginProjectRoutes(PLUGINS)`。

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

- `0080` 只新增一张表，可降级（删表）；降级后所有插件视为停用。
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
