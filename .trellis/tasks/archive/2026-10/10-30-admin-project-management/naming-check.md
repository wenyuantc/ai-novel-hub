# “作品管理”名称复核

范围仅为 `AppLayout.tsx`、`AdminProjectsPage.tsx`、`ProjectManagementNotice.tsx`、`AdminUsersPage.tsx`。读取了本任务最新 PRD/design/implement，并沿用已加载的 check 上下文；未检查或修改并行书架及预览文件。

## Findings (fixed)

未发现需要继续修复的文案缺漏，没有修改产品代码或新增测试。

| 位置 | 核对结果 |
|---|---|
| 账户菜单、全局侧栏 | 均为“作品管理”。 |
| 管理列表页面标题、当前页面名称、面包屑 | 均为“作品管理”。 |
| 工作区侧栏返回入口 | 显示“作品管理”，仍链接 `/admin/projects`。 |
| 工作区与 Reader 共用的归属提示 | 返回按钮为“返回作品管理”。 |
| 升降管理员提示 | 均明确包含“用户管理和作品管理”。 |

`/admin/projects`、`admin-projects` 菜单 key、组件及 API 内部标识保持不变。“项目名称”“项目状态”等原领域字段不属于本次菜单名称调整。

## Findings (not fixed)

无本轮待修问题。历史研究与旧截图验收记录保留；本轮没有重新执行完整浏览器交互或后端全量测试。

## Verification

在 `frontend/` 执行，退出码均为 0：

- Lint：`npx eslint src/components/AppLayout.tsx src/pages/AdminProjectsPage.tsx src/components/project/ProjectManagementNotice.tsx src/pages/AdminUsersPage.tsx` 通过。
- Tests：`npm test -- src/components/project/adminProjectsUI.test.ts src/api/admin.test.ts src/utils/adminUsers.test.ts src/utils/projectAccess.test.ts`，**4 文件、18 测试通过**，1.58s；只有既有 Node localStorage ExperimentalWarning。
- TypeCheck/Build：`npm run build`，`tsc -b` 与 Vite 均通过，3440 模块，最大 vendor chunk 1146.12 kB，未超既有 1500 kB 阈值。
- 仓库根目录执行这四个授权文件的 `git diff --check -- ...` 通过。

未执行 Git 写操作。
