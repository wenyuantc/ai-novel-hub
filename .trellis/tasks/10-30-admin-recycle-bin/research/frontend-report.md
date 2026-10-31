# 前端实施报告

## 修改边界（实施前）

- 行为差距：现有界面仍将作品删除描述为永久删除，没有管理员回收站；删号界面允许携带作品级联删除，审计操作人假定始终有用户 ID。
- 行为归属：`src/api` 的传输类型/客户端、管理页面及确认组件、`router.tsx`/`AppLayout` 导航、项目/任务 store 的目标失效与审计展示。
- 预计修改：新增回收站 API/纯逻辑/页面/确认组件及测试；修改原删除组件与书架入口、用户管理护栏、导航、审计类型/展示和必要状态失效。
- 明确不做：不改后端、Trellis 规范或主会话任务文件，不引入组件库，不提供个人回收站/一键清空，不重写通用加载 hook 或现有认证流程。
- 局部复用：书架改用已有作品名称确认组件；列表沿用 AntD/useLoader，危险确认复用逐字名称判断。定向测试确保原人工管理和普通作品能力不变。

## 实际结果

### 已实现

1. 新增管理员懒加载 `/admin/recycle-bin`，同步桌面账户菜单/侧栏、移动导航和页名；列表支持搜索、归属筛选、分页与末页回退，桌面表格和 375px 卡片展示删除者/到期/章节字数。
2. 新增 `api/recycleBin.ts` 和镜像 schema 类型；恢复/彻底删除均 POST，`deleted_at` 微秒字符串原样回传，purge 名称不 trim。危险弹窗防重复提交、禁止忙碌关闭，并在卸载/换身份后废弃旧响应。
3. 统一书架、概览与管理员目录的删除确认，全部使用已有名称确认组件及 30 天/管理员恢复/任务取消失败说明，不再误称普通删除为永久删除。
4. 用户管理计数注明含回收站，有作品时删除禁用且提供原因与正常作品/回收站筛选入口；安全处理并发 409 的 `project_count`。按管理员 ID 重建页面，保留禁用/启用和既有自删保护。
5. 审计新增 restore/purge/expire 标签，空 actor 显示为「系统自动清理（系统）」而非 #null；展示明确的生命周期动作、30 天保留和取消任务计数。
6. 目标级 project/task store 失效不影响其他作品；迟到加载/保存/轮询/track 不能重新挂回已删内容。新增明确 404 撤下缓存项目，临时网络刷新失败仍保留原作品，避免把网络抖动当删除。

### 修改文件

新增：
- `frontend/src/api/recycleBin.ts`、`recycleBin.test.ts`
- `frontend/src/utils/recycleBin.ts`、`recycleBin.test.ts`
- `frontend/src/pages/AdminRecycleBinPage.tsx`
- `frontend/src/components/project/RecycleProjectModal.tsx`、`recycleBinUI.test.ts`

修改：
- `frontend/src/api/types.ts`、`api/auditLogs.ts`
- `frontend/src/router.tsx`、`components/AppLayout.tsx`
- `frontend/src/pages/AdminUsersPage.tsx`、`AdminProjectsPage.tsx`、`AdminAuditLogsPage.tsx`、`ProjectListPage.tsx`
- `frontend/src/components/project/DeleteProjectModal.tsx`
- `frontend/src/components/auditLogs/AuditLogDetailDrawer.tsx`、`auditLogsUI.test.ts`
- `frontend/src/stores/project.ts`、`project.test.ts`、`tasks.ts`、`tasks.test.ts`
- `frontend/src/utils/adminUsers.ts`、`adminUsers.test.ts`、`auditLogs.ts`、`auditLogs.test.ts`

### 检查结果

- `npm run lint`：通过（最后一次 exit 0）。
- `npm run build`：标准命令被既有环境阻挡，`node_modules/.tmp/*.tsbuildinfo` 与 `dist` 为 root:staff，只读；TypeScript 报 TS5033/EACCES，并非类型检查失败。没有修改依赖目录权限、源码配置或 package scripts 来掩盖问题。
- 等价 TypeScript 检查通过：
  - `node node_modules/typescript/bin/tsc -p tsconfig.app.json --incremental --tsBuildInfoFile /private/tmp/ainovel-recycle-tsconfig.app.tsbuildinfo`
  - `node node_modules/typescript/bin/tsc -p tsconfig.node.json --incremental --tsBuildInfoFile /private/tmp/ainovel-recycle-tsconfig.node.tsbuildinfo`
- 等价 Vite 生产构建通过：临时 config 仅导入原 `vite.config.ts` 并重定向 `cacheDir`，`--configLoader runner --outDir /private/tmp/ainovel-recycle-frontend-dist`。保留了原 root-owned dist，不清理或覆盖它。最终可用产物路径为 `/private/tmp/ainovel-recycle-frontend-dist`，最新源码已重建；回收站为独立 lazy chunk，无 1500kB 超限警告。
- Vitest 标准缓存/令牌目录也不可写，因此使用可写临时 HOME、Vite cacheDir 与 fsModuleCachePath；不改变测试内容、发现规则、隔离或断言。
- 定向首次检查：9 文件 64 测试通过。
- 最终全套：**67 文件、515 测试通过**（包括新增明确404与临时网络失败回归），exit 0。仅出现既有 Node `--localstorage-file` 警告。
- `git diff --check`：通过。

最终完整测试命令（工作目录 frontend）：

```text
HOME=/private/tmp/ainovel-recycle-test-home npm test -- --config /private/tmp/ainovel-recycle-frontend.config.mts --configLoader runner --fsModuleCachePath /private/tmp/ainovel-recycle-module-cache
```

临时配置 `/private/tmp/ainovel-recycle-frontend.config.mts`：导入原前端 config，唯一覆盖为 `cacheDir: /private/tmp/ainovel-recycle-frontend-cache`。构建日志 `/private/tmp/ainovel-recycle-vite-build.log`。

### 交接验收

真实浏览器由主会话在隔离 SQLite/FakeLLM 服务执行，本子代理未点击用户数据或真实模型：
- 管理员 `/admin/recycle-bin` 搜索/归属/分页、恢复、purge 取消/错误名称/确认、过期按钮。
- 普通用户无回收站入口，直接访问由 RequireAdmin/后端拒绝。
- 书架与概览普通删除说明及完整名称确认；取消无请求，删除成功跳回目录并清理目标缓存。
- 用户管理有作品行不可删号但仍可禁用，作品计数链接分别进入正常目录/回收站筛选。
- 审计系统清理与恢复/彻底删除摘要，桌面/375px 明暗主题。

### 规范提醒与限制

主会话需同步 spec：ProjectStore.refresh 只有临时网络故障继续静默；明确404会撤下旧项目以停止编辑和任务副作用。标准 build 的 root-owned 生成目录权限问题仍存在，必须如实保留此环境缺口；等价类型检查、打包和全测试已验证，不等于原 npm run build 在当前权限下可直接运行。没有 commit/push，也未修改后端或其他参与者文件。
