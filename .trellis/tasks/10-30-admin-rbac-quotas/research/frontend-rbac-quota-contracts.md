# Research: 前端 RBAC、权限失效与用户配额契约

- Query: 盘点所有二元管理员判断及真实操作路径，给出操作级权限、自定义角色、多角色分配、配额展示/编辑与撤权失效的最小接入方案。
- Scope: internal；仅规划调研，不修改产品代码，不启动任务。
- Date: 2026-11-01
- Requirements: 本任务 `prd.md` 及主会话最新确认；用户已采用内置角色不可改删可复制、自定义在用不可删、旧管理员迁超管、权限变更后续请求生效且不强制登出的生命周期规则。

## Findings

### 1. 现有事实与改造原则

- 技术栈沿用 React 18、TypeScript strict、Ant Design、Zustand、React Router、Vitest，具体依赖见 `frontend/package.json`；无需新增权限框架、查询缓存框架或全局 CRUD 框架。
- 当前会话 `User` 只有 `id/username/is_admin/created_at`（`frontend/src/api/types.ts:3`）；管理目标 `AdminUser` 也只有二元角色字段（`:92`）。必须新增显式超管身份、有效操作权限和角色安全摘要，不能用“拥有任一管理权限”替代所有动作授权。
- 当前 `RequireAdmin` 通过 `adminGuard` 一次性放开所有管理页面（`frontend/src/components/RequireAdmin.tsx:8`；`frontend/src/utils/adminUsers.ts:8`），7 组路由均共用它（`frontend/src/router.tsx:57-96`）。应改为同一个带所需权限参数的守卫，角色管理单独要求超管，其他页面要求对应读取能力。
- 后端才是最终权限和配额事实源，客户端隐藏/禁用仅用于避免误操作；不接收浏览器提供的“有效权限”作为授权依据，不存用户可写的授权缓存。

### 2. 所有二元管理员触点清单

以下是 `rg -l 'is_admin|RequireAdmin|isAdmin' frontend/src --glob '!*.test.ts' --glob '!*.css'` 的实质授权触点；每组都需检查，不可只改导航。

| 触点 | 文件与证据 | 最小迁移方向 |
| --- | --- | --- |
| 登录身份/传输 | `frontend/src/api/types.ts:3,92,123,130`；`frontend/src/stores/auth.ts:37` | 统一身份/角色/权限/配额类型；角色变更不模拟401 |
| 路由守卫 | `frontend/src/components/RequireAdmin.tsx:8`；`frontend/src/utils/adminUsers.ts:8`；`frontend/src/router.tsx:57` | 一个权限守卫复用，逐路由声明读取或超管要求 |
| 菜单、移动导航、账户入口、面包屑 | `frontend/src/components/AppLayout.tsx:43-68,104-110,123` | 同一权限表决定全部入口；没有 dashboard 权限时不能把“管理后台”面包屑固定指到 dashboard |
| 用户列表/创建 | `frontend/src/pages/AdminUsersPage.tsx:21,53,77,157,167,181` | 多角色标签/分配入口替代布尔开关；创建普通账号能力与超管角色分配能力分开 |
| 用户详情/关联作品 | `frontend/src/pages/AdminUserDetailsPage.tsx:27,36,76,98,130` | 显示多角色与配额；读账号不自动读取作品，相关区块和深链独立授权 |
| 账号动作 | `frontend/src/utils/adminUsers.ts:28`；`frontend/src/components/adminUsers/useAdminUserActions.ts:13,26`；`adminUserActionsController.ts:21-24,44-79`；`AdminUserActionButtons.tsx:21`；`AdminUserActionDialog.tsx:18,34` | actor+action+target 判定；撤下旧“设为/取消管理员”动作，角色操作仅超管 |
| 任务监控 | `frontend/src/pages/AdminTasksPage.tsx:120`；`frontend/src/components/adminTasks/useAdminTasks.ts:11,18`；`adminTasksController.ts:129,143,223-224` | task.read 与 task.cancel 分开；仅撤取消权限不能清掉仍合法的只读视图 |
| 系统设置 | `frontend/src/pages/AdminSystemSettingsPage.tsx:83`；`frontend/src/components/systemSettings/useSystemSettings.ts:9,16` | read/edit 分离，只有写权限才可挂保存/恢复确认和提交回调 |
| 首页统计 | `frontend/src/components/dashboard/useAdminDashboard.ts:8` | dashboard.read；不可用“任意管理权限”代替 |
| 回收站 | `frontend/src/pages/AdminRecycleBinPage.tsx:98`；`frontend/src/components/project/RecycleProjectModal.tsx:56-79` | list/restore/purge 独立；当前 modal 的 isCurrent/submit 也要按所选动作复核 |
| 审计 | `frontend/src/pages/AdminAuditLogsPage.tsx:118`；`frontend/src/components/auditLogs/AuditCleanupModal.tsx:48`；`AuditLogDetailDrawer.tsx:86,97` | audit.read/cleanup 分离，关联版本正文另需作品读取能力 |
| 作品管理身份 | `frontend/src/utils/projectAccess.ts:4-8`；`frontend/src/hooks/useProjectAccess.ts:8` | 继续真实 actor/owner 分离，并新增只读/人工编辑/回收等具体能力 |
| Skill | `frontend/src/pages/SkillsPage.tsx:20,29-30,62-84,94,110`；`frontend/src/components/skills/SkillCard.tsx:18,37`；`SkillEditDrawer.tsx:18,57,130` | 全局创建/编辑/删除/恢复使用具体能力；个人 Skill 权限及他人私有404不变 |
| 工坊 | `frontend/src/pages/WorkshopPage.tsx:20-65`；`frontend/src/utils/workshop.ts:16,51`；`frontend/src/components/workshop/WorkshopItemDrawer.tsx:46,112,121`；`WorkshopItemControls.tsx:27-31`；`ShareToWorkshopModal.tsx:31,47,59-60` | 审核读取/批准拒绝、官方发布、上下架、元数据管理、删除分开；作者个人行为仍按 owner |

检索中的非授权命中不要机械替换：`frontend/src/utils/adminTasks.ts:67` 的 `isAdminTaskKey` 是 UUID 格式校验；`frontend/src/api/auditLogs.ts:49` 和 `api/types.ts:155,162` 中 `is_admin` 是历史审计字段，旧日志仍需可读，不应清除历史含义。

### 3. 作品只读权限是本任务最大的前端扩大点

现有 `projectAccess` 只返回 `ready/managingOther/canUseAI`，并没有 `canEdit`（`frontend/src/utils/projectAccess.ts:4-8`）。此前所有管理员都可人工编辑，所以众多人工按钮没有额外授权条件；引入仅 `project.read` 的自定义角色后，这些界面必须真实变为只读，不能把它们留给后端403兜底。

明确例子：

- 章节保存快捷键只检查编辑锁、saving、dirty，不检查管理写权限（`frontend/src/pages/project/ChapterEditorPage.tsx:299-318`）；保存/完成按钮及标题、选角、正文均可操作（`:671-680,867-896`）。不能只隐藏“保存”按钮而保留 Cmd/Ctrl+S 或可提交的编辑抽屉。
- 项目概览人工保存及删除在 `frontend/src/pages/project/OverviewPanel.tsx:124,270,286`；封面上传/删除在 `frontend/src/components/project/CoverPanel.tsx:74-93`。
- 人工设定编辑分布在 `frontend/src/pages/project/WorldPanel.tsx`、`CharactersPanel.tsx`、`OrganizationsPanel.tsx`、`RelationshipsPanel.tsx`、`CareersPanel.tsx`、`ForeshadowsPanel.tsx`；子抽屉/关联成员/职业分配/类型维护均需跟进，不只是页面新增按钮。
- 大纲移动、批量删除、从大纲建章、快照恢复及细纲编辑在 `frontend/src/pages/project/OutlinesPanel.tsx` 和 `frontend/src/components/outlines/*`。
- 章节版本正文修改/恢复/删除在 `frontend/src/components/chapter/VersionDrawer.tsx`；选角 `CastSelect`、章节伏笔、人物关系/组织/职业等子组件都能产生写请求。
- 知识库已有文档/分块与人工设置、剧情记忆人工维护分别在 `frontend/src/pages/project/KnowledgePanel.tsx`、`MemoriesPanel.tsx` 及 `frontend/src/components/kb/*`。

建议维持粗粒度的“作品人工内容编辑”权限覆盖现有人工维护面的既有范围，不在本任务再发明每种人物字段/每部作品的独立 ACL。统一扩展 `projectAccess` 及 `useProjectAccess`，按真实 owner 先保留个人原有能力，再对他人作品根据具体权限派生 `canRead/canEdit/canRecycle` 等；各手工页面和相关子控件消费该能力。最终权限名称由后端目录确定，不在不同组件各造一套。

必须保持的隔离：

- `ProjectLayout` 的 AgentDock/向导阻断已经按 `canUseAI/managingOther` 控制（`frontend/src/pages/project/ProjectLayout.tsx:18,34-41`）。任何只读或可编辑管理角色都不能因新权限打开他人模型、AI、智能体、个人任务、私人风格/Skill/MCP、备份导入导出等 owner-only 行为。
- Reader 已用 `projectAccess(...).managingOther` 忽略 owner 阅读进度（`frontend/src/pages/ReaderPage.tsx:78,85,151`）；不能因从 is_admin 迁出导致其误判为本人，继而上报对方阅读进度。
- 失去他人作品 read 权限时，必须清掉当前他人项目内容及其未交付请求，而不是只使顶部入口消失；失去 edit 但保留 read 时转只读并撤下写操作确认。本人作品与个人缓存不应因后台角色变化一概重置。

### 4. 拆分读取能力后的组合接口陷阱

1. `AdminProjectsPage` 的 owner 下拉无条件请求用户列表（`frontend/src/pages/AdminProjectsPage.tsx:25-26,44`）。`project.read` 不应隐式得到 `user.read`。最小方案是无 user.read 时不挂载用户搜索 loader，保留合法 owner ID 深链/安全数值筛选；如确有必要，应由资源目录提供只含 ID/用户名的受该资源 read 保护的 options，不开放完整用户详情。
2. 任务监控同样通过完整用户列表做 owner 搜索（`frontend/src/components/adminTasks/adminTasksController.ts:35-36,120-126`），需同样拆开。
3. 用户详情无条件挂所属作品区块（`frontend/src/pages/AdminUserDetailsPage.tsx:98`），统计中的管理目录链接也无条件展示（`:36,78`）。用户信息读取授权不等于作品正文/目录/回收站读取；区块、深链、请求挂载应分别校验。
4. 审计详情“读取关联版本正文”实际调用版本内容接口（`frontend/src/components/auditLogs/AuditLogDetailDrawer.tsx:88-99`）；audit.read 只能读审计摘要，不能隐式得到 project.read。
5. `AdminProjectsPage` 删除按钮当前不区分读取与回收能力（`frontend/src/pages/AdminProjectsPage.tsx:36,50`），回收站 restore/purge 也需独立。

权限目录可定义变更动作对同域读取能力的必要依赖，但前后端必须有一致且可见的规则：编辑角色时展示依赖并由后端验证，不要为了旧 UI 正常工作，默默授予跨域读取能力。

### 5. 权限刷新与403，不破坏会话

#### 可复用事实

- 当前 auth 初始化使用 `/auth/me` 和公开配置；`me` 任何失败（含网络错误）都将 status 改为 anonymous（`frontend/src/stores/auth.ts:37-46`）。不能直接把 `init()` 放进权限轮询或每次403回调。
- 公开配置刷新是独立的 `PublicConfigController`，含 single-flight、AbortController、generation、apply 后作废旧读（`frontend/src/utils/publicConfig.ts:6-37`），可借鉴模式但不把用户权限塞入公开配置。
- 可见性/前台刷新已有 `startVisiblePolling`，公开配置包装可见30秒及 focus 去重（`frontend/src/utils/publicConfig.ts:45-56`）。建议用户权限另设独立刷新动作/控制器，复用调度器，不重写轮询框架；具体周期可沿用30秒，不扩大实时广播范围。
- 401 仅由统一客户端触发登出（`frontend/src/api/client.ts:80-87`，相关 handler 注册在 `frontend/src/App.tsx:27-33`）；403 当前只是 `ApiError`，不清登录。这一边界必须保留。

#### 推荐最小契约

- `/auth/me`（或等价的认证私有身份投影）增加当前 roles、effective_permissions、is_super_admin，以及能覆盖“所分配角色权限被编辑”的权限快照版本。无角色时个人能力不消失，未知权限 fail closed。
- 新 `refreshAuthorization` 只刷新身份/能力，保持 authenticated；401 才 clear，网络/500失败保留最近成功的能力快照并让后端继续拒绝越权。不要因此重设 unknown、清掉用户草稿或重新请求所有私人配置。
- 每次 login/register/logout/clear/切账号使旧身份请求 epoch 失效；同一账号成功角色调整后先作废旧读再 apply 新快照。慢 `/auth/me` 不得在降权或退出后重新赋权。
- 在返回前台、进入管理路由和具体 RBAC403 时触发刷新；后台可见轮询负责其他标签页撤权后的入口同步。后端下一请求撤权是安全保证，前端轮询不是授权保障，不承诺撤回已显示的数据或已完成的操作。
- 建议后端对 RBAC 拒绝返回稳定 `code=permission_denied`（及安全的 required_permission 可选），客户端可只对这一类403触发 single-flight 权限刷新。不要把 MCP 关闭、业务禁止等所有403当角色撤权，更不要把它们改成401。若不用统一 hook，至少管理 controller 对403显式刷新能力，不能各自复制网络身份逻辑。
- 快照语义变化时失效相关动作 gate；同一 actor 只失去 task.cancel 也必须撤下已打开取消框，保留 task.read 视图。权限失去再恢复时旧操作仍须作废，不能因当前允许就接纳上一代响应。

### 6. 异步 actor gate 必须从“任意管理”升级为“目标动作”

- 账号动作已有良好页级 controller：同步 busy、prepare 异步校验后复核、旧目标/请求代次防护、卸载清理（`frontend/src/components/adminUsers/adminUserActionsController.ts:26-83`）。复用并扩展，不要另造一套账号 CRUD controller。
- 当前 `allowed()` 只检查 actor ID 与 is_admin（`frontend/src/components/adminUsers/useAdminUserActions.ts:13,26`），`permitted` 只检查 self 与作品计数（`adminUserActionsController.ts:21-24`、`frontend/src/utils/adminUsers.ts:28-36`）。需要加入实际操作权限、超管身份、目标是否为管理账号；`rowActions` 由 actor 身份快照而非仅 actorId 派生。
- 当前角色 toggle 直接 PATCH `{is_admin: !user.is_admin}`（`adminUserActionsController.ts:68`），应从新版 UI 撤除，用超管专属角色多选/角色分配请求替代；不能把二元开关保留成另一个绕过入口。
- 任务 controller 当前任一通道401/403都会 deny 整页（`frontend/src/components/adminTasks/adminTasksController.ts:223-224`）。权限拆分后，owner搜索403、取消403不能错误清空仍有权的任务只读数据；动作失权清该通道，主读失权才撤下整体数据。
- 回收站 modal 的提交与响应 gate 都查 is_admin（`frontend/src/components/project/RecycleProjectModal.tsx:58,63`），应按 restore/purge 各自权限，并保持原始 deleted_at、严格名称和同步 busy。
- 审计清理 gate（`AuditCleanupModal.tsx:48`）、关联版本 gate（`AuditLogDetailDrawer.tsx:86`）、系统设置和 dashboard 的 hooks 均需相同迁移。
- 工坊、Skill 现有部分操作使用 `modal.confirm` 和简单 busy，没有统一权限订阅（例如 `frontend/src/pages/SkillsPage.tsx:68-83`；`frontend/src/components/workshop/WorkshopItemDrawer.tsx:94,117`）。本任务应窄补本页确认框拥有权/失效，不使用全局 `Modal.destroyAll`，不顺带把所有页面改成通用状态框架。

### 7. 新增 UI 与已有复用边界

#### 角色管理（超管专属）

- 建议新增懒加载 `/admin/roles`，使用现有 AntD Table/移动卡片、局部表单与声明式 Modal/Drawer；不引入通用权限树框架。
- 列表展示名称、内置/自定义、权限摘要、分配人数、更新时间；内置定义只读，仅复制；自定义编辑需说明“影响全部成员”，分配人数非零禁止删除并明确先撤回。后端409是最终准绳，不能只信列表旧人数。
- 权限按现有业务模块分组，展示中文动作及危险标识、必要依赖；创建/编辑/复制复用同一具体表单。复制提交创建新角色，不修改内置记录。
- 用户列表/详情显示多个角色标签；分配入口仅超管，支持一次替换全部分配并携带 revision，不能由普通账号更新字段隐式赋权。本人超管身份和最后启用超管保护仍后端复核。

#### 用户配额

- 优先扩展现有 `AdminUserDetailsPage` 为两项紧凑用量行/表格，不重新实现三组资源统计或账号动作。作品使用已有全生命周期总数口径；任务当前运行数需由后端明确来源，不用活动任务总数冒充运行数。
- 每项编辑清晰区分“继承系统默认 / 指定数值 / 不限额”；0是明确禁止新建/新启动，不用 `value || default`、`0 → null` 等隐式转换。正整数技术上界以服务端约定与 JS safe integer 为准，严格拒绝小数、负数、布尔和字符串伪装。
- 显示有效值、来源及当前占用；降低不删除作品、不强杀任务，全局并发仍有效。目标是管理账号且 actor 非超管时，不挂编辑弹窗/提交路径，即使拥有 quota.write。
- 配额读/写应有独立安全 API；quota.read 或 quota.write 不必隐式授予完整 user.read。若单独配额管理页并非需求，不主动增加；但仅拥有 quota.write 的角色必须有可达的最小合法管理入口，方案可显式规定读依赖或复用仅安全账号选择器，不能让权限成为不可用的空勾选。
- 个人书架当前仅请求正常作品列表（`frontend/src/pages/ProjectListPage.tsx:27`），不能用 `items.length` 计算含回收站的占用。创建失败可利用后端安全 quota_exceeded 结构显示用量/上限，并保留表单（`frontend/src/components/shelf/CreateProjectModal.tsx:18-28` 已只在成功 reset）。导入、灵感转换等其他立项路径也要展示同一错误，不只禁侧栏新建按钮。
- 个人任务面板列表包含 queued/running/paused 等活动任务（`frontend/src/components/TaskPanel.tsx:35-42,99`），其长度不能当后台运行并发用量；超限继续排队，不提示创建失败或显示“任务已取消”。

### 8. 前后端需对齐的安全 API 传输契约

具体路由命名由主设计与后端调研统一；以下是必需信息而不是要求新增一套平行 API：

| 对象/操作 | 必需安全信息与行为 |
| --- | --- |
| 当前登录身份 | actor ID、is_super_admin、roles 安全摘要、effective_permissions、覆盖角色编辑的授权快照版本；与 login/register/me 输出一致 |
| 管理目标账号 | roles 标签/标识、是否具有管理能力的真实投影（例如 is_management_account）、可用于并发更新的版本；不返回密码/哈希/密钥或私人配置 |
| 权限目录/角色 CRUD | 稳定操作 code、中文标签/分组、危险标记、读取依赖；角色内置标记、revision、member_count；只有超管管理 |
| 多角色分配 | role_ids + revision 的明确替换语义；省略不变与空集合的行为明确；事务内验证目标与防最后超管保护 |
| 每用户配额 | 两项配置状态与数值、有效上限、来源、当前占用、revision；0与不限额不可混淆，运行数不含排队数 |
| RBAC 拒绝 | 401仅认证失效；403说明权限不足，建议稳定 code；不依赖中文文案识别 |
| 配额/角色冲突 | 409 code 明确 quota_exceeded / role_in_use / stale_revision 等；安全计数/上限可展示，不把原始请求或敏感SQL带出 |

配额配置可用判别联合表达，例如 `{mode:'inherit'}`、`{mode:'limited',value:0}`、`{mode:'unlimited'}`；effective_limit 可用 null 表不限，但不能让同一 null 同时表示继承和不限。此为传输设计建议，须与后端统一，不能由前端擅自发明不匹配字段。

### 9. 必要测试与浏览器矩阵

#### 自动化落点

- `frontend/src/utils/adminUsers.test.ts`：super/普通目标/管理目标、各动作单权限、self/最后超管前端提示（后端仍兜底）；`utils/projectAccess.test.ts`：本人、他人只读、他人编辑、丢权限、错误pid/未加载，无 AI 扩权。
- 新权限 helper/守卫测试：多角色有效权限由服务端快照表达、super单独识别、未知code拒绝、同域依赖、只读/变更分离；不要仅断言 JSX 存在某个字符串。
- `frontend/src/stores/auth.test.ts` 与新增授权刷新 controller 测试：保持登录、401清会话、网络失败不登出、single-flight、退出/切账号/快照更新后旧读失效、角色编辑触发快照变化、StrictMode setup/cleanup/setup。
- `frontend/src/api/client.test.ts`：Cookie/ApiError 原契约、RBAC403刷新但不 unauthorized、业务403不误清会话/不循环；`api/admin.test.ts` 与新增 role/quota API 测试：PATCH/PUT replacement、revision、AbortSignal、0/继承/不限额、未知字段不传、中文错误。
- `components/adminUsers/adminUserActionsController.test.ts`：只丢一项权限、异步prepare期间撤权、目标从普通变管理、退出又回同账号、旧响应无toast/reload/导航、角色toggle不再发旧PATCH。
- `components/adminTasks/adminTasksController.test.ts`：仅取消撤权仍可读；read撤权全部清；owner搜索403不扩大权限或误撤主列表；慢取消已受理结果不被前端误标最终取消。
- `components/systemSettings/systemSettingsController.test.ts`、`useSettingsConfirmation.test.ts`：只读模式与编辑撤权、dirty冲突、409结果未知不自动重发。
- `components/project/adminProjectsUI.test.ts`、`recycleBinUI.test.ts`、`components/workshop/workshopUI.test.ts`、`components/skills/skillsMcpUI.test.ts`、`components/auditLogs/auditLogsUI.test.ts`：按具体能力的展示、触发和请求挂载，不只改 fixtures 的 is_admin。
- 所有既有测试继续运行；node/SSR/源结构测试不能代替 DOM交互验收。命令在 `frontend/`：`npm run lint`、`npm run build`、`npm test`。新增页面必须路由懒加载，不提高 bundle warning limit。

#### 真实浏览器必须验证

1. 超管创建、复制、修改自定义角色，多角色分配，内置改删拒绝、在用删除409、成员权限并集/撤回；普通用户直达 `/admin/roles` 拒绝。
2. 只有 project.read 的账号可查看他人内容，但标题/正文/设定/版本/封面等实际变为只读，Cmd/Ctrl+S、旧弹窗和关联子表单不能提交。
3. 内容编辑角色可人工修改但无他人 AI/智能体/任务输出/私人资源/阅读进度请求；本人作品保留全部个人功能。
4. 任务只读角色不能取消；管理员在另一个会话撤 task.cancel 时，已有取消框撤下，任务只读视图保留；撤 task.read 后数据撤下而账号仍登录。
5. user.read 与 project.read/recycle.read 独立组合，无额外 owner/所属作品请求；audit.read 不能读取版本正文；Skill/workshop各动作独立授权。
6. 对管理目标的禁用/改密/删除/配额修改由非超管拒绝，即使持有对应普通目标权限；角色编辑导致普通目标新获管理权限时旧确认失效/后端拒绝。
7. 配额继承、有限值、0、不限额；作品计数含回收站/删除中；两会话竞争新建；任务超限排队、降低不强杀、提高后能调度；错误表单不清空。
8. 两个会话分别编辑角色、旧响应延迟、离页/退出/换账号、刷新身份失败，确认无过期toast/重开弹窗/权限倒退或误登出。
9. 所有新增页及关键弹窗在375px与桌面、浅暗主题、键盘与读屏标签下可用，避免页面级横向溢出。

## Related Specs

- `.trellis/spec/frontend/index.md`
- `.trellis/spec/frontend/component-guidelines.md`：现有管理员页面、表单/确认、主题与移动端规范。
- `.trellis/spec/frontend/state-management.md`：auth/private缓存、actor/请求代次、项目真实owner、用户切换reset。
- `.trellis/spec/frontend/type-safety.md`：types.ts镜像schema、PATCH与统一Cookie/ApiError契约。
- `.trellis/spec/frontend/quality-guidelines.md`：node Vitest、lint/build/test、懒加载与包体积。
- `.trellis/spec/backend/auth-admin.md`：原会话版本与管理员写入保护；本任务获批后更新二元角色相关契约。
- `.trellis/spec/backend/admin-projects.md`：人工内容与owner-only能力隔离、无模型副作用。
- `.trellis/spec/backend/system-settings.md`：设置revision、来源、dirty草稿、可见轮询与迟到响应失效。
- `.trellis/spec/backend/admin-task-monitoring.md`、`admin-audit.md`、`recycle-bin.md`：处置/清理/恢复的专有确认和事务语义继续保留。

## External References

无。本次结论来自仓库代码、依赖清单与现有规范，不新增第三方权限库，不需要外部版本/API研究。

## Caveats / Not Found

- 尚无角色/权限/配额 API、稳定 RBAC403错误码或授权刷新私有快照控制器；上文新增字段和路由是设计需求/建议，不是仓库既有事实。
- 当前普通用户个人能力是默认可用而非可任意撤销的菜单权限。本任务只细分管理能力，不应把个人创作功能迁成必须显式分配某个角色才能使用。
- is_admin 兼容字段的最终含义必须由后端设计统一；不论保留何种兼容投影，前端具体授权不得继续依赖它。真实管理账号判定需服务端显式返回，不能以角色显示名或 is_admin=false 推断为可被普通账号管理员改密。
- 权限快照刷新成功前可能有旧按钮短暂可见；安全保证仍来自后端每请求/写入复核。无法承诺从用户记忆、截图或已经交付的页面中撤回数据，也不能用前端取消fetch撤回已提交业务。
- 所有管理能力分开后，原来“一进管理页即有全部读取权限”的便利不再成立；owner选择、用户详情作品区块、关联版本正文和quota-only操作入口都要纳入设计，不能靠隐式扩权补洞。
- 本文只进行了静态调研，没有执行产品测试、启动浏览器或声称功能已实现。最终实现前须再次核对最新代码与完整权限目录。
