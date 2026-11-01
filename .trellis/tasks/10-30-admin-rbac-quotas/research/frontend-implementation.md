# 前端实现报告（2026-11-02）

## 所有权与交接

实现者：`/root/rbac_frontend_implement`。仅修改 `frontend/` 以及本报告/前端验证日志；没有修改后端、规范、规划、task 状态或用户原有配置，没有提交、推送或归档。

已按主会话要求冻结产品代码，交由 Trellis checker 接管。浏览器由主会话使用独立 SQLite / FakeLLM 服务验收，本报告不把静态渲染测试等同于完整浏览器验收。

## 实现范围 / 主要文件

- 传输：`src/api/types.ts`、`roles.ts`，完整对齐 `backend-api-contract.md`。User/AdminUser 的新 RBAC 字段必填，权限为33项 `PermissionCode` 联合；管理目标不虚构 effective_permissions。移除创建/更新用户的旧 is_admin 字段。
- 授权：`utils/permissions.ts`、`components/RequirePermission.tsx`、`router.tsx`、`components/AppLayout.tsx`，具体动作只使用 `is_super_admin` / effective_permissions，无旧 bool fallback。管理路由进入按 status / actor ID / pathname / permission 触发独立 single-flight me 刷新；全部侧栏、移动菜单、面包屑和首页跨域链接按对应 read 权限呈现。
- 角色：`pages/AdminRolesPage.tsx`、`components/adminUsers/UserRolesButton.tsx`、`RoleTags.tsx`、`utils/roles.ts`。内置只读可复制、自定义新建/编辑/删除、多角色整体分配、在用角色不可删、自己的超管身份保护；危险标记和递归依赖显式勾选。角色搜索不更新目标账号的原始 CAS 快照，跨搜索保留已选角色。
- 配额：`components/adminUsers/UserQuotaPanel.tsx`、`utils/quotas.ts`，继承/limited（含0）/无限明确区分；显示用量、有效上限和来源、全站并发硬约束；非超管管理目标只读，CAS 冲突不自动重发。最后一处调整：同 actor 撤写只关闭编辑器，不再重挂仍合法的只读用量表。
- 账号管理：`AdminUsersPage` / `AdminUserDetailsPage` / `adminUsers/*`，撤除 bool 角色 toggle；动作分别按 create/status/reset_password/delete，管理目标需 super；详情作品、回收站和配额各自 read 守卫。
- 既有后台：任务、系统设置、审计清理、作品删除、回收站恢复/清理和 owner 查询分别守卫。任务/设置 controller 撤写不误清合法读取；审计关联版本需要 projects.read，不能借 audit.read 展示正文。审计类型/中文动作与安全 RBAC 摘要对齐新增 role/assignment/quota/bootstrap。
- 人工工作区：`utils/projectAccess.ts` / `hooks/useProjectAccess.ts` 提供 read/edit/recycle 与实时 mounted + actor + revision 检查。世界观、角色/组织/关系/职业及关联子表单、章节新建删除、标题/正文/Cmd-S、选角、版本标星/备注/删除/恢复、大纲/细纲/快照、封面上传删除、知识库人工设置和记忆维护均覆盖只读禁写与提交前后守卫。
- 私有边界：管理他人仍不挂载其 AI、模型/Key、生成记录/任务输出、私人 Skill/MCP、阅读进度写入或 owner-only 备份。`managingOther` 由实际 owner 差异决定，不随权限撤销误变回 owner。
- 工坊/全局 Skill：逐动作 read/approve/reject/publish/update/hide/unhide/delete 和 global_skills.create/update/references/delete/restore_builtin；参考资料权限独立于正文修改。个人草稿/操作只跟 actor，不因无关管理角色刷新被取消。全局 Skill 授权以当前明确权限为准，不被旧 editable 缓存阻挡。
- 异步与会话：`hooks/useAuthorizationScope.ts` / `useAuthorizationCleanup.ts`、auth store 与统一 client 的代次守卫，权限刷新不依赖 init/publicConfig，可见30秒/前台/具体 permission_denied403；网络/500/403不误登出，仅有效代次401清登录。旧 logout 不清新 login/register，旧 me/401/RBAC403不覆盖、清除或刷新新会话；旧提交不 toast/刷新/导航，确认框只销毁本页拥有对象。
- 缓存：项目失去跨用户读权立即清空并拒绝迟到加载/刷新；同 actor 撤写保留只读内容。账号缓存清理改为账号通知同步执行，避免父 effect 在新页面 load 后作废请求；Skill/风格/模型列表补 reset 代次，旧账号响应不能复活到新账号。

未新增依赖，沿用 AntD 中文、主题 token、既有 controller、路由懒加载与原 chunk 配置。

## 测试与真实结果

各命令独立执行，未 chmod/chown/sudo，未修改依赖目录或工具配置。

| 命令 | 结果 | 日志 |
|---|---|---|
| `npm test` | 环境失败：root-owned `node_modules/.vite-temp`，EACCES | `frontend-test-standard.log` |
| `npm run lint` | 通过，0错误0警告 | `frontend-lint-final.log` |
| `npm run build` | 环境失败：两份 root-owned tsbuildinfo，TS5033/EACCES，无代码类型错误 | `frontend-build-standard.log` |
| `HOME=/private/tmp/admin-rbac-vitest-home npm test -- --configLoader runner --no-cache --fsModuleCachePath /private/tmp/admin-rbac-vitest-cache` | **87文件，682/682通过** | `frontend-test-final.log` |
| `./node_modules/.bin/tsc -p tsconfig.app.json --tsBuildInfoFile /private/tmp/admin-rbac-quotas-app.tsbuildinfo` | 通过，exit 0 | `frontend-tsc-app-final.log` |
| `./node_modules/.bin/tsc -p tsconfig.node.json --tsBuildInfoFile /private/tmp/admin-rbac-quotas-node.tsbuildinfo` | 通过，exit 0 | `frontend-tsc-node-final.log` |
| `./node_modules/.bin/vite build --configLoader runner --outDir /private/tmp/admin-rbac-quotas-frontend-final-dist` | 通过，exit 0；保留原 chunk 限制，最大 vendor-antd 1147.01kB | `frontend-vite-final.log` |
| `git diff --check -- frontend` | 通过 | 无输出 |

新增/更新的非源码匹配回归包括：真实 fetch 角色/复制/CAS/清空分配/0额度传输；具体 RBAC403与管理目标保护403区别；旧会话401/403迟到；me single-flight、401/500/网络失败、revision不倒退、旧 me 跨账号迟到；旧 logout 与新 login/register；账号动作真实异步校验/已发 mutation 撤写不 toast/刷新/导航；项目撤写保读/撤读清空与迟到响应；私有 Skill/风格/模型缓存旧请求；角色依赖和配额三态；人工工作区只读控件及首页跨域导航静态渲染。

## 构建交接

首份产物（未覆盖/清理）：`/private/tmp/admin-rbac-quotas-frontend-dist`。

独立最终产物：`/private/tmp/admin-rbac-quotas-frontend-final-dist`。

- index.html SHA-256：`9975bacaeca864cb82f96d0dcc8287918ca495c865e92d444dad837344260a85`
- 入口：`/assets/index-DwUp2Ll5.js`，181.25kB
- 该产物之后仅有上述 QuotaPanel“撤写不重挂只读表”的微调；微调后完整682测试、lint、app tsc均已重新通过。主会话已知该差异，由 checker 按最终磁盘代码统一重建，不再由本代理覆盖验收产物。

## 待主会话 / checker 继续

- 使用 checker 最后重建的实际产物完成最终浏览器验收，特别是375px角色/多选分配/三态配额、他人章节只读与撤权中的确认框/慢响应。主会话已预验角色目录、内置保护、依赖显式勾选和真实创建角色。
- 标准命令的 root-owned 缓存权限问题仍存在；本轮遵循要求不修改环境所有权，以完整等价命令验证。不要把标准命令记为“通过”。


## 窄补充：角色页真实移动卡片（2026-11-02）

主会话重新派发后仅编辑 `frontend/src/pages/AdminRolesPage.tsx`，新增 `frontend/src/pages/AdminRolesPage.test.ts`；其他产品文件由 checker 所有，未修改。保留 checker 已修正的角色名称100/说明500校验。

- 375px 分支改用紧凑 `List.Item` 卡片，完整显示名称/内置标记/说明、权限数、成员数、更新时间及复制/编辑/删除；长文本可断行，动作可换行。
- 桌面沿用原 Table 与分页；移动使用独立紧凑分页，搜索占可用全宽，刷新/新建可换行。桌面/移动复用同一动作渲染及权限数投影，不变更角色/API/权限业务。
- 聚焦验证：`HOME=/private/tmp/admin-rbac-vitest-home npm test -- src/pages/AdminRolesPage.test.ts --configLoader runner --no-cache --fsModuleCachePath /private/tmp/admin-rbac-vitest-cache`：**1文件4测试通过**，真实SSR覆盖移动卡片/动作/内置及在用禁用、桌面表格、不授权与空结果。
- `./node_modules/.bin/eslint src/pages/AdminRolesPage.tsx src/pages/AdminRolesPage.test.ts`：通过，0错误0警告。
- `./node_modules/.bin/tsc -p tsconfig.app.json --tsBuildInfoFile /private/tmp/admin-rbac-quotas-roles-mobile.tsbuildinfo`：通过，exit0。
- `git diff --check -- frontend/src/pages/AdminRolesPage.tsx frontend/src/pages/AdminRolesPage.test.ts`：通过。
- 日志：`frontend-role-mobile-{test,lint,tsc}.log`。未覆盖任何临时 dist，未运行全套/构建，由 checker 统一最终验证与产物重建。完成后再次冻结这两份产品/测试文件。
