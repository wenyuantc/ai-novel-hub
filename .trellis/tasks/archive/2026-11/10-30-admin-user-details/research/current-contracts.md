# Research: 管理员用户详情的生命周期、账号动作与作品入口

- Query: 核实现有正常作品/回收站统计分组、账号动作复用与异步失效、按所属用户进入管理工作区的契约。
- Scope: internal；只读代码调研，仅写本文件；不运行产品服务或测试、不联网。
- Date: 2026-11-01
- Task: `10-30-admin-user-details`，规划阶段，未执行 `task.py start`。

## 用户已确认的范围

- 只统计作品数、章节数、正文总字数，区分正常作品与回收站。
- 不做磁盘占用、Token、费用及最近活跃时间。
- 最近登录包含成功密码登录、注册后自动登录；失败/禁用/日常访问/改密续签不更新，历史无记录不回填。

## Findings

### 1. 生命周期并非天然只有两个状态

| 状态 | 精确数据库条件 | 现有列表归属 |
|---|---|---|
| 正常 | `deleted_at IS NULL AND deletion_started_at IS NULL` | 正常作品目录 |
| 删除中 | `deleted_at IS NULL AND deletion_started_at IS NOT NULL` | 正常目录和回收站均不展示 |
| 回收站，含到期待清理 | `deleted_at IS NOT NULL` | 回收站目录，不按期限过滤 |
| 已彻底清理 | Project 行不存在 | 不再参与任何实时计数 |

- 集中条件 `backend/app/services/project_lifecycle.py:38-39`；`ensure_active` 在 `:42-47` 分别将回收站目标映射为 404、删除中映射为 `409 project_deleting`。
- 数据库约束 `backend/app/models/entities.py:175-180` 禁止同时存在 `deleted_at` 和 `deletion_started_at`，并要求意图时间与 token 同空/同非空。
- 删除协调先写 `deletion_started_at` 并提交，排空任务后才写 `deleted_at` 并清意图，见 `backend/app/services/recycle_bin_service.py:59-88`。失败 finally 释放意图，见 `:90-99`；单进程启动只释放旧意图，不自动完成删除，见 `:256-264`。
- 正常管理员目录从集中正常条件开始，见 `backend/app/services/admin_project_service.py:23`。
- 回收站目录只以 `deleted_at IS NOT NULL` 开始，见 `backend/app/services/recycle_bin_service.py:130-149`；`restorable` 仅在响应投影时计算，不决定行是否展示，见 `:153-169`。
- 期限为连续 UTC 30 天，等于期限即不可恢复，见 `backend/app/services/project_lifecycle.py:30-35`、`backend/app/services/recycle_bin_service.py:191-192`。过期但未清理的行仍须计入回收站。
- purge 在 `backend/app/services/recycle_bin_service.py:249-250` 删除 Project 并提交；周期清理的固定 cutoff/键集分页/逐项目事务见 `:267-293`。

**规划风险：**不能把“全部作品数减正常作品数”当成回收站作品数，否则会把删除中误标为回收站，与已有目录不一致。现有账号 `project_count` 是全部未物理删除作品：`backend/app/services/admin_service.py:23-28`、`:42-44`，因此删除进行中时，`project_count` 可以大于正常数与回收站数之和。主会话需在设计中明确删除中如何提示；推荐保留真实两组条件，不伪装归类。若展示总作品数，应说明含删除中，或另有轻量的删除中数量提示；这不是增加磁盘等资源维度。

### 2. 基础聚合可复用现有分组子查询

- `backend/app/services/project_service.py:71-80::chapter_stats_subquery` 按 `Chapter.project_id` 预聚合 `count(Chapter.id)` 与 `coalesce(sum(Chapter.word_count), 0)`，返回每作品一行。
- 正常管理员目录使用 outer join，空作品统计为 0，见 `backend/app/services/admin_project_service.py:33-41`、`:52-53`；回收站复用相同 helper，见 `backend/app/services/recycle_bin_service.py:141-169`。
- 账号详情应从目标用户的 Project 集合出发，以该子查询 outer join 后按生命周期条件聚合，或使用同口径的条件子查询；不要直接 join 所有 Chapter 后 `count(Project.id)`，否则作品数随章数重复；不要分页后相加或按作品逐条请求。
- 章节数包含空正文的章节；字数只累加当前 `Chapter.word_count`，不叠加 `ChapterVersion.word_count`、大纲/日志/设定等内容。正文口径是非空白字符数，见 `backend/app/services/chapter_service.py:14-19`。
- 过滤目标用户必须是服务端 `Project.user_id == target_user_id`；统计应覆盖其全部相关作品而非当前作品列表页。

### 3. 管理路由与导航可以容纳详情子路由

- 当前 `/admin/users` 在 `RequireAdmin` 中直接挂载 `AdminUsersPage`，见 `frontend/src/router.tsx:88-95`；同类低频管理页面使用 `RequireAdmin + Outlet + lazyPage`，见 `:58-87`。新增 `/admin/users/:userId` 可延续该结构，但不能直接把详情塞到没有 Outlet 的列表路由下。
- `frontend/src/components/RequireAdmin.tsx:12-20`：未知身份等待、匿名转登录并保留 pathname/search、普通用户转首页、管理员放行；后端仍必须每请求以 AdminUser 校验。
- 用户详情无需新增全局菜单：现有导航以 `/admin/users` 前缀选中，见 `frontend/src/components/AppLayout.tsx:40`、`:106`；详情页可延续“用户管理”分组，页内提供用户标识与返回列表。
- `frontend/src/components/StudioNavigation.tsx:49-57` 管理的是创作导航定义；管理员入口实际在 AppLayout，不能只改前者而漏掉真实入口。

### 4. 账号动作适合做窄共享提取，不能复制整页

现有 `frontend/src/pages/AdminUsersPage.tsx` 同时持有新建用户和目标账号动作；详情只需要后者。

- 新建用户专属：`:46-48`、`:61-74`、`:303-327`，应保留列表页。
- 可提取的目标账号动作：重置密码状态/提交 `:50-52`、`:76-89`，启停/改角色确认 `:91-108`，删号状态/提交 `:54-59`、`:110-141`，动作按钮 `:176-247`，目标弹窗 `:329-394`。
- 适合的窄边界是“账号动作控件/控制 hook + 目标弹窗”，接收真实 `actorId`、目标 `AdminUser`、`onChanged`/`onDeleted` 回调；列表保留分页回退，详情自行负责刷新或删号后返回。不要创建通用管理框架，也不要把创建用户流程搬到详情。
- 继续复用 `frontend/src/utils/adminUsers.ts:26-34::rowActions`：本人全部危险账号动作不可用；有任何作品时禁止删号。`canConfirmDelete` 在 `:38-40` 严格逐字相等；`:42-50` 保留安全计数提示与 409 收窄。
- 服务端既有动作全部由真实 AdminUser 路由转发，见 `backend/app/api/v1/admin.py:47-66`；详情不需要新写入端点。自身/最后管理员保护和会话版本仍由 `backend/app/services/admin_service.py:76-86`、`:100-118`、`:121-128`、`:131-156` 执行；账号动作仍走 `AuditOperation` 同事务审计。
- 前端自己不能重置自己的密码，但服务端重置函数未单独禁止自己；提取时必须保留现有 UI 的 `rowActions` 约束，不因可调用接口而开放新按钮。
- 删号保护必须继续使用全部作品 `project_count`，不能改用正常统计。存在 pending 管理取消请求时，后端还会返回 `user_has_pending_tasks`，见 `backend/app/services/admin_service.py:148-153`，应原样显示错误而非当作作品计数。

### 5. 现有异步失效可复用，但当前账号动作并非全部安全

- 列表页面以真实管理员 ID 为 key 重建，非管理员不挂载内部页面，见 `frontend/src/pages/AdminUsersPage.tsx:399-402`；详情还应把目标用户 ID 纳入 key，以避免参数切换复用旧用户数据/表单。
- `frontend/src/hooks/useLoader.ts:19-32` 通过 effect 清理的 `alive` 忽略旧 fn 响应，但 fn 变化时并不清空旧 `data`、也不主动设置 loading。详情必须用目标 key 重挂载或显式校验当前目标，不能在新目标加载时暂显旧用户、旧作品或旧危险动作。
- 删除路径已经有同步 `deletePending`、请求代次和回调阶段真实身份检查，见 `frontend/src/pages/AdminUsersPage.tsx:117-139`。通用请求代次 helper 可复用 `frontend/src/utils/recycleBin.ts:21-29::createRecycleRequestGate`。
- 当前重置与启停/改角色仅 await 后直接 toast/reload，见 `AdminUsersPage.tsx:76-108`，没有与删除相同的代次/身份保护；`modal.confirm` 实例也未在卸载时销毁。共享提取应给这些动作同样的 target/actor/request-generation 保护、忙碌防重复及本组件确认框清理，避免复制既有薄弱点。
- 更完整的局部参考为 `frontend/src/components/project/RecycleProjectModal.tsx:47-80`：mount 清理 invalidate、提交前身份检查、同步 pending、回调身份检查、忙碌禁止关闭；父级用包含 actor/target/代次/action 的 key，见 `frontend/src/pages/AdminRecycleBinPage.tsx:90`。
- 网络仍走 `api/client.ts`：`:104` 携 Cookie，`:78-81` 的 401 统一清会话；不用页面新造鉴权，也不应添加全局缓存。

### 6. 所属作品列表与管理工作区复用边界

- 正常作品 API：`frontend/src/api/admin.ts:5-6::listProjects`，已有 `owner_user_id`/搜索/创作状态/分页；服务端条件、count 与 items 共用筛选并按 `updated_at DESC,id DESC` 排序，见 `backend/app/services/admin_project_service.py:23-41`。
- 正常管理页从查询串初始化 owner，见 `frontend/src/pages/AdminProjectsPage.tsx:15-26`；作品标题与“管理”进入 `/projects/{id}/overview`，见 `:29`、`:37`。详情可以直接请求同一 API，固定 owner 为目标 ID，不提供清空 owner 后意外变成全站列表的交互；同时保留“查看该用户全部正常作品”深链 `/admin/projects?owner_user_id={id}`。
- 回收站 API：`frontend/src/api/recycleBin.ts:4-10`；目录从查询串初始化 owner 并分页，见 `frontend/src/pages/AdminRecycleBinPage.tsx:52-72`；深链 `/admin/recycle-bin?owner_user_id={id}` 已支持。
- 回收站只展示元数据/恢复/彻删，不提供正常工作区链接，见 `frontend/src/pages/AdminRecycleBinPage.tsx:27-46`。如详情内嵌回收站列表，应复用此规则；若只提供带 owner 的回收站入口，也不能把已删作品 ID 链到正常编辑区。
- 正常编辑区按服务器 owner 与当前登录用户派生 `ready/managingOther/canUseAI`，见 `frontend/src/utils/projectAccess.ts:4-8`；`frontend/src/pages/project/ProjectLayout.tsx:27-41` 在正确项目加载后显示归属、禁用他人向导、完全不挂载他人 AI/AgentDock。
- 归属提示现有组件 `frontend/src/components/project/ProjectManagementNotice.tsx:5-6` 明确“管理员正在管理「用户名」的项目”，返回作品管理而非冒用 owner 会话。详情入口不需要更改该权限模型。
- 管理他人作品不因此开放模型配置/平台 Key/私人资源/AI 任务输出或写他人阅读进度；详见 `.trellis/spec/backend/admin-projects.md`。用户详情只能返回明确 allowlist 元数据与计数，不返回完整 User ORM 或私人配置。

## 相关测试与规范

### 现有测试落点

- `frontend/src/utils/adminUsers.test.ts:22-69`：路由守卫、自身/有作品账号动作、精确删号确认、409 安全计数。
- `frontend/src/api/admin.test.ts:26-71`：owner/搜索/分页、账号方法与 body、Cookie、错误透传。
- `frontend/src/components/project/adminProjectsUI.test.ts:38-58`：他人归属与 AI 不挂载、本人正常能力、管理员导航。
- `frontend/src/components/project/recycleBinUI.test.ts:30-87`：窄屏/桌面、无正常编辑链接、到期/空/加载、确认和导航。
- `frontend/src/utils/recycleBin.test.ts:25-39`：关闭/换目标后迟到响应失效。
- `backend/tests/test_admin.py:136-184`：权限/404/分页与精确字段集合；新增最近登录字段若扩展现有列表，需明确更新字段集合断言，而非删断言。
- `backend/tests/test_admin.py:232-458`：密码/禁用会话失效、改角色、自身/最后管理员、删号与自助改密。
- `backend/tests/test_admin_projects.py:25-52`：项目筛选/分页/计数；`:83-101` 降级即时撤权；`:170-238` 他人私有资源/模型副作用拒绝。
- `backend/tests/test_recycle_bin.py:199-265`：owner 筛选、到期边界、正常/回收站/到期待清理都阻止删号。
- `backend/tests/test_project_lifecycle.py:175-191`：启动解除删除中意图而不复活回收站记录。

### 相关规范

- `.trellis/spec/backend/auth-admin.md`
- `.trellis/spec/backend/admin-projects.md`
- `.trellis/spec/backend/recycle-bin.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/frontend/component-guidelines.md:132-149`：账号管理、回收站、危险确认与异步保护；`:365-385`：主题 token、375px 与内部横滚。
- `.trellis/spec/frontend/state-management.md:62-103`：退出/401/切账号重置、派生统计以后端为准、变更后 reload。

## External References

无。本次只核实现有项目代码，不需要外部文档或第三方依赖。

## Caveats / Not Found

1. **需主会话明确的生命周期显示细节：**删除中不是回收站；正常与回收站之和并不始终等于用于删号保护的 `project_count`。不得静默将删除中归为回收站。
2. 当前账号重置/角色/启停缺少完整目标失效保护；应在范围内共享提取时补齐，而不推广成无关全局重构。
3. 正常作品与回收站页面的 owner 查询串均只用于初始 `useState`；若同一路由实例内替换查询串，原页面不自动同步。用户详情自身固定 owner 不依赖这一行为；若在本任务中提供跨用户同路由深链，需要验证挂载/重新导航后的筛选正确，避免顺手扩大到全站筛选架构重构。
4. 本调研未运行测试/浏览器/数据库，不宣称已验证运行结果；以上均为静态源码证据。暂无需要新增依赖或联网才能解除的技术阻碍。
