# 前端实施边界与验证

## 写入前边界

- 最小缺口：管理员尚无统一审计检索、只读详情与受保护日期清理交互。
- 行为归属：`frontend/src/api/` 负责传输；管理页负责筛选分页；审计组件负责详情与清理；`AppLayout`/router 负责管理员入口。
- 计划文件：`api/types.ts` 镜像后端 schema；新增 audit API/页面/详情/清理/纯函数及对应测试；局部修改 `router.tsx`、`AppLayout.tsx`。
- 非目标：不改后端、认证/全局状态架构、其他管理页、规划三份文档或已有无关改动；不新增依赖、不提供审计全文差异。
- 跨层数据流：后端 `schemas/audit.py` → `api/types.ts` → 统一 client → 页面/只读详情；主动查看版本独立走 `versionApi.get`。服务端仍为权限、删除与令牌有效性的最终权威。

## 验证结果

### 已实现文件

- `frontend/src/api/types.ts`：镜像 `schemas/audit.py`，含 28 个动作、目标/结果/安全字段/状态枚举、分页查询、详情和清理契约。
- `frontend/src/api/auditLogs.ts`、`auditLogs.test.ts`：统一客户端封装、完整中文标签；查询/详情/预览/确认及 400/401/403/404/409/500 回归。
- `frontend/src/utils/auditLogs.ts`、`auditLogs.test.ts`：本地日历日期→UTC 半开区间、筛选、页码回退、预览有效性、过期响应门闩与安全摘要投影。
- `frontend/src/pages/AdminAuditLogsPage.tsx`：组合筛选、稳定服务端分页、空态/加载/错误重试、桌面表格/移动列表，清理后读取最后有效页。
- `frontend/src/components/auditLogs/AuditLogDetailDrawer.tsx`：历史快照、只读摘要及版本 available/changed/missing；主动查看才调用 `versionApi.get`，正文不进审计 API、全局缓存或本地存储，关闭后丢弃。
- `frontend/src/components/auditLogs/AuditCleanupModal.tsx`：独立日期、范围/时区/预计/保护数、确认复选框+危险按钮；日期/关闭/账号变化撤销旧响应，执行时消费令牌且拒绝重复提交，失败后必须重新预览，成功显示实际删除数。
- `frontend/src/components/auditLogs/auditLogsUI.test.ts`：node + react-dom/server 展示/权限/快照/版本/移动布局回归。
- `frontend/src/router.tsx`、`frontend/src/components/AppLayout.tsx`：RequireAdmin 懒路由、桌面与移动共用侧栏/账号菜单、页面名与面包屑。

### 实际命令与结果

工作目录均为 `frontend/`（git/Python 检查除外）。

| 命令 | 结果 |
| --- | --- |
| `npm run lint` | 最终通过，无错误/警告。初版的 preserve-caught-error 已通过保留 `cause` 修复。 |
| `npm run build` | 环境受阻：现有 root 所有的 `node_modules/.tmp/*.tsbuildinfo` 不可写，TS5033/EPERM；未改配置或权限。以下等价检查与构建通过。 |
| `npm exec -- tsc -p tsconfig.app.json --incremental --tsBuildInfoFile /private/tmp/admin-audit-frontend-app.tsbuildinfo` | 通过。 |
| `npm exec -- tsc -p tsconfig.node.json --incremental --tsBuildInfoFile /private/tmp/admin-audit-frontend-node.tsbuildinfo` | 通过。 |
| `npm exec -- vite build --configLoader runner --outDir /private/tmp/admin-audit-frontend-dist` | 通过；3445 modules，新增 AdminAuditLogsPage chunk 约 22.19 kB（gzip 8.16 kB），vendor-antd 1147.01 kB < 1500 kB；RelationGraph 仍独立。仅提示 outDir 在项目外，不自动清空该临时目录。 |
| `npm test -- src/api/auditLogs.test.ts src/utils/auditLogs.test.ts` | 环境受阻：`node_modules/.vite-temp` 配置缓存 EPERM；runner 还需避开 Vitest 的用户令牌及文件系统模块缓存，未改测试配置。 |
| `env HOME=/private/tmp/admin-audit-frontend-home npm test -- --configLoader runner --no-cache --no-fsModuleCache src/api/auditLogs.test.ts src/utils/auditLogs.test.ts src/components/auditLogs/auditLogsUI.test.ts` | 3 个文件、30 条测试全部通过。 |
| `env HOME=/private/tmp/admin-audit-frontend-home npm test -- --configLoader runner --no-cache --no-fsModuleCache` | 全量 64 个文件、489 条测试全部通过。现有两个 worker 发出 Node `--localstorage-file` 路径警告，无失败。 |
| `git diff --check -- frontend .trellis/tasks/10-30-admin-audit-log/research/frontend-report.md` | 通过。 |
| Python AST 对照后端 Literal 和前端联合类型 | 6 组共 94 个枚举值完全对齐（28 action、8 target、2 result、8 error、33 field、15 state）。 |

日期测试涵盖上海 UTC+08:00、同日、非法日期/反向范围及纽约夏令时 23/25 小时日，避免固定 24 小时算法。异步纯函数测试证明撤销旧请求后晚到响应不能复活令牌。UI SSR 测试检查窄屏可换行、无固定大 min-width、权限与版本失效提示；它不替代真实浏览器的尺寸/事件验证。

### 集成路径及剩余验证

- 生产构建产物：`/private/tmp/admin-audit-frontend-dist`；主会话已收到该路径，将在隔离数据库环境做实际浏览器与后端联调，不对用户真实数据执行清理。
- 本代理未启动浏览器；桌面/375px、明暗主题、键盘、取消/快速改日期/关闭/切账号及真实 409 预览重放交互由主会话继续验收，不能将 SSR 当作浏览器证据。
- 前端按钮与守卫不替代后端权限、令牌绑定、防重放、受保护事件过滤及清理原子事务；这些由后端实现及测试覆盖。
- 没有新增依赖，没有修改全局状态架构、其他管理页、后端或任何既有无关文件。
