# 插件框架：实施计划

> 需求 `prd.md`（F1–F15、AC1–AC13），设计 `design.md`。按顺序执行；每一步先写测试再写实现。

## 回归策略

中途各步只跑受影响的测试加哨兵测试，全量门禁留到第 10 步收口时跑一次（项目约定，见开发者记忆「精简回归策略」）。

- 后端哨兵：`tests/test_system_settings.py`、`tests/test_auth*.py`、任务引擎的基础用例（实施时从 `backend/tests/` 中按文件名确认一组不超过 5 个文件）。
- 前端哨兵：`src/components/StudioNavigation` 相关测试、`src/utils/publicConfig.test.ts`、`src/stores/auth` 相关测试。

## 步骤

### 1. 清单与注册表（后端，纯代码）

- [x] 新增 `backend/app/plugins/{__init__,manifest,registry}.py`：`PluginManifest`、`all`、`get`、`plugin_of_kind`、`load(extra=())`，`REGISTERED = ()`。
- [x] 校验：标识格式与唯一、任务类型前缀与冲突、router 前缀。
- [x] 测试 `backend/tests/test_feature_plugins_registry.py`：合法登记；重复标识、非法标识、任务类型无前缀、与核心类型冲突、router 前缀不符各自报错（AC10）。
- [x] 测试用示例插件放在 `backend/tests/plugins/sample_plugin.py`（一个 router、一个任务处理器），**不进** `app/`。

验证：`cd backend && uv run ruff check app/plugins tests/test_feature_plugins_registry.py && uv run pytest tests/test_feature_plugins_registry.py -q`

### 2. 状态表与迁移

- [x] 模型 `backend/app/models/feature_plugins.py`，在 `app/models/__init__.py` 导出。
- [x] 迁移 `backend/alembic/versions/0080_feature_plugins.py`（父修订 `0079`），同步超级管理员权限，验证空库降级及已有插件状态、插件任务与取消审计、启停审计、授权历史或委派权限时的降级保护。
- [x] 按现有迁移测试的写法补 `0079 → 0080 → 0079` 往返与结构断言；确认作品备份的反射测试与整库备份表目录测试的期望值需要怎样调整，并调整。

验证：`uv run pytest tests/ -q -k "migration and (0080 or feature_plugin)"`，加上被改动期望值的那两个既有测试文件。

### 3. 状态服务与门禁函数

- [x] `backend/app/plugins/state.py`：`is_enabled`、`enabled_ids`、`require_enabled`、`require_kind_enabled`、`set_enabled`；`PluginDisabled` 错误。
- [x] `set_enabled`：授权写屏障 → 未登记 404 → 在持有写屏障时检查修订号并写入→ 同事务审计。取消行为见步骤 6。
- [x] 审计动作 `feature_plugin.enable` / `feature_plugin.disable` 加入 `backend/app/schemas/audit.py` 的动作清单及其摘要约束。
- [x] 测试 `backend/tests/test_feature_plugins_api.py`：默认停用；启用 / 停用；修订过期 409；并发首写只有一个成功；审计记录存在且失败回滚不留状态（AC3、AC5 的服务层部分）。

验证：`uv run pytest tests/test_feature_plugins_api.py -q`

### 4. 权限与管理接口

- [x] `backend/app/core/permissions.py` 增加 `plugins.read`、`plugins.update`；同步所有断言权限数量或清单的测试与前端类型 `PermissionCode`。
- [x] `backend/app/schemas/feature_plugins.py`、`backend/app/api/v1/admin_plugins.py`，在 `app/api/v1/__init__.py` 挂载。
- [x] 测试 `backend/tests/test_feature_plugins_api.py`：列表内容；无权限 403；只读权限改状态 403；未登记 404；非法请求体 422；409；`no-store`（AC1、AC4、AC5）。

验证：`uv run pytest tests/test_feature_plugins_api.py tests/test_rbac*.py -q`（后者按实际文件名）

### 5. 接口门禁与挂载

- [x] `backend/app/plugins/deps.py::require_plugin`；`app/main.py` 在每个应用内遍历注册表挂载插件 router；`create_app` 增加仅测试使用的 `extra_plugins` 参数并在装配前调用 `plugins.load(extra)`。
- [x] 测试 `backend/tests/test_feature_plugins_api.py`（用示例插件）：未启用 403 且带 `plugin_disabled`；未登录访问未启用接口同样 403；启用后 200；停用后再次 403；数据行仍在（AC2、AC3、AC7）。
- [x] 不登记插件时路由表与改动前逐项相同的断言（AC11 的后端部分）。

验证：`uv run pytest tests/test_feature_plugins_api.py -q`

### 6. 后台任务门禁（风险最高，先读后写）

- [x] **先读代码**：`backend/app/services/task_engine/cancellation.py`、`owner_cancellation.py`、`engine.py` 中领取与取消相关段落，
      以及 `.trellis/spec/backend/admin-task-monitoring.md`。确认可复用的最小取消入口，把结论补进 `design.md` 3.4。
      若现有入口都带具体任务类型的前置条件，停下来修订设计，不绕开。
- [x] 受理与重试 / 继续两处加入 `require_kind_enabled`（`task_service.py:547`、`:758` 之后）。
- [x] 领取时门禁（`engine.py:1710` 附近）：插件停用的任务不执行，按已取消收尾。
- [x] `set_enabled(enabled=False)` 登记该插件未结束任务的取消，并在提交后通知本进程引擎。
- [x] grep `require_task_admission_open` 的全部调用点，逐个确认是否可能产生插件类型任务，把结论写进测试文件头注释。
- [x] 测试 `backend/tests/test_feature_plugins_tasks.py`：未启用时新建 / 重试 / 继续被拒；启用后任务能跑完；
      有运行中与排队中任务时停用，两者均以已取消结束且未完成执行（AC2、AC3、AC6）；停用提交后才入队的任务不被执行。

验证：`uv run pytest tests/test_feature_plugins_tasks.py -q`，加任务引擎哨兵文件。

回滚点：本步改动集中在 `task_service.py` 两处、`engine.py` 一处与 `state.py`；出问题可单独回退这一步而保留 1–5。

### 7. 公开配置

- [x] `AuthConfigOut` 增加 `enabled_plugins`，`auth_config` 填充。
- [x] 更新既有的公开配置测试；新增「启用后出现、停用后消失、未登记的残留行被忽略」。

验证：`uv run pytest tests/test_auth*.py tests/test_system_settings.py -q -k config`

### 8. 前端：登记、状态、守卫、路由与导航

- [x] `frontend/src/plugins/{types.ts,registry.tsx,RequirePlugin.tsx}`，`PLUGINS = []`。
- [x] `api/types.ts` 的 `AuthConfig` 增加 `enabled_plugins?`；`stores/auth.ts` 增加 `enabledPlugins`（初始 `[]`），登出 / 401 时清空。
- [x] `StudioNavigation.tsx`、`AppLayout.tsx`、`router.tsx` 接入；API 客户端遇 `plugin_disabled` 触发一次配置刷新。
- [x] 测试（Vitest，传入一个假的插件对象）：导航合成（启用才出现、作品内外各自生效）；路由合成被 `RequirePlugin` 包裹；
      守卫的三种状态（加载中、未启用、已启用）；`PLUGINS` 为空时导航与路由和改动前一致（AC2、AC9、AC11 的前端部分）。

验证：`cd frontend && npx vitest run src/plugins src/components/StudioNavigation src/stores && npm run lint`

### 9. 前端：管理页

- [x] `api/adminPlugins.ts`、`pages/AdminPluginsPage.tsx`、`router.tsx` 的 `admin/plugins`、`AppLayout.tsx` 的管理导航入口、`utils/permissions` 的首个可进入管理页判断。
- [x] 测试：只读权限下开关禁用；停用确认文案；409 后重新读取；成功后调用 `applyConfig`；离页后旧响应不反写；空状态。
- [x] 真实浏览器走查（用临时在本地登记的示例插件，走查后撤销，不提交）：启用 / 停用、另一会话 30 秒内同步、375px 宽度、浅色与深色。

验证：`npx vitest run src/pages/AdminPluginsPage src/api/adminPlugins && npm run lint`

### 10. 跨实例、文档与收口

- [x] 跨实例测试 `backend/tests/test_feature_plugins_cross_process.py`：按 `tests/test_system_settings_cross_process.py` 的写法起两个共享数据库的应用，
      验证 A 启用后 B 下一次请求成功、A 停用后 B 下一次请求被拒（AC8）。
- [x] 确认 `/mcp-plugins` 页面、路由、接口测试未改动且通过（AC12）。
- [x] 规范：新增 `.trellis/spec/backend/feature-plugins.md`、前端对应条目，更新 `system-settings.md`、`rbac-quotas.md`、`admin-backups.md` 与两个 `index.md`。
- [x] README：功能概览、管理与运维、目录结构（AC13）。
- [x] 全量门禁：
  - `cd backend && uv run ruff check . && uv run pytest -n 4 --dist loadfile --durations=20`
  - `cd frontend && npm run lint && npm run test && npm run build`

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 引擎取消路径有既有约束，复用不当会破坏章节占用或执行槽 | 第 6 步先读后写；不新造终态路径；出问题可单步回退 |
| 权限数量、备份表目录、迁移结构等「冻结契约」测试大量失败 | 第 2、4 步各自先 grep 出所有断言点再改，不靠跑全量发现 |
| 插件 router 的门禁依赖与鉴权依赖的执行顺序 | 第 5 步用「未登录访问未启用接口」的测试钉住 |
| 前端初始 `enabledPlugins = []` 导致启用的插件页面先闪「未启用」 | 守卫区分「尚未取到配置」与「未启用」，测试覆盖 |

## 开始实现前的检查

- [x] 用户已明确批准最新的规划总结。
- [x] `implement.jsonl` 与 `check.jsonl` 各有至少一条真实的规范条目。
- [x] 工作区干净，或已与用户确认现有改动的归属（本仓库常有并发会话）。
