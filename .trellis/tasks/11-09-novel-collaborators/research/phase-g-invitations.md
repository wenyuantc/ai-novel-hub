# 阶段 G2：邀请、成员面板和书架

## 实现与接入

- `frontend/src/api/collaboration.ts` 镜像 `schemas/collaboration.py`：作品邀请携带角色和 `allow_owner_ai`，系列邀请仅含用户名；接受/拒绝使用 scope、invitation UUID、`expected_revision`；修改和撤销使用成员 revision。所有请求可取消，沿用统一 API 客户端。
- `components/collaboration/Invitations.tsx` 在书架展示作品和系列待处理邀请、作者、角色、费用授权，接受/拒绝前明确确认；处理结束后刷新邀请与书架事实。
- `components/collaboration/MemberPanel.tsx` 默认导出，可由作者界面嵌入：`{ scope: 'project' | 'series', resourceId: number, canManage: boolean, onChanged?: () => void }`。作品成员可邀请、修改角色和作者模型费用授权或撤销；系列成员固定 editor，仅邀请或撤销。组件不会因管理员身份自行显示作者专属面板。
- 版本冲突、失败或响应未知后不重发写请求，仅重新读取事实。刷新失败时保留已显示内容及草稿，但禁用旧事实的写操作；403/404 清空受限资料。成员 revision 变化时保留编辑草稿，必须显式采用最新状态并再次确认；邀请 UUID 已变化时要求重新选择。
- 控制器与 hook 按 actor、会话请求代次、资源和挂载生命周期防止旧响应落地。书架外层按 actor/授权版本/会话代次重挂载，旧作品列表不能恢复到新会话。共享作品卡使用后端 `owner.username` 与 `access`，展示共享角色及模型授权；viewer 显示只读阅读入口，操作菜单按 `can_export`、`can_backup` 和 `can_recycle` 决定，`can_edit_content` 单独控制创作入口。
- `ProjectLayout` 与系列页面的成员面板接线属于其他并行分工；本分工不修改这些页面。系列编辑者对共享条目删除的界面约束及 AI/向量费用执行仍属于 G3/G4/E。

## 验证

- 聚焦测试：在 `frontend` 执行 `npm test -- src/api/collaboration.test.ts src/components/collaboration/factsController.test.ts src/components/collaboration/collaborationUI.test.ts src/components/shelf/LibraryBookCard.test.ts src/pages/ProjectListPage.test.ts`，14 passed；覆盖接口请求、冲突与未知结果的事实刷新、单次写入、旧响应、失权、卸载、角色展示和页面刷新。
- 本分工文件运行 ESLint 通过；`npx tsc -b --pretty false` 通过。全量 lint/test/build 和最终集成验证由主任务 Gate G 执行。
- 浏览器尝试使用隔离 SQLite 假数据、Vite `127.0.0.1:5174` 与后端 `127.0.0.1:8901`，Chrome CUA 新建标签导航以及接管标签均超时，未能观察页面，因此**不计为浏览器验证**。两个专用端口已确认停止监听；待 G5 使用可连接的浏览器重试。
