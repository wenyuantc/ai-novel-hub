# 全范围独立检查报告

日期：2026-11-02。检查者：`/root/rbac_full_scope_check`（原生 `trellis-check`）。

## 范围与结论

按 native hook 原始完整输出、check.jsonl、PRD/design/implement、spec-read-map、三领域调研和实际API契约加载上下文；以原始 HEAD `991b0a3e643fdeb4d2ff3bb59377bb75f560ab3b` 对比当前工作区，包含全部修改及未跟踪产品/测试文件，不限暂存或最后一批。共 **173 文件：backend 65、frontend 108**；完整清单见末节，最终 SHA-256 见 `check-final-snapshot.json`。

独立追查真实服务/调度/组件路径并修复下述局部缺口。当前没有已知、尚未修复的本任务产品缺陷；实际浏览器AC矩阵由主会话另记，本报告不把Node/SSR当浏览器、不把SQLite当PostgreSQL。

没有派生代理、修改规划/规范/任务状态、提交、推送或归档。原有 `.codex/config.toml`、`.gitignore`、两个source-command SKILL四文件的hash均与基线一致。角色移动布局由主会话裁定后短暂交原前端实现者补齐，交回后已重新独立审阅和运行完整前端门禁；不存在交接期间并改同文件。

## Findings (fixed)

### F1：任务领取完成到执行收尾之间仍有可取消空隙

- 文件：`backend/app/services/task_engine/engine.py`、`backend/tests/test_rbac_concurrency.py`。
- 问题：原 `_claiming` 在commit/admitted完成后、领取session的 `__aexit__` 仍可能await之前已清除。owner取消或stop此时可直接cancel执行体；它尚未进入 `_run` 的异常/终态收尾，可能遗留孤立running并提前释放内存占用。
- 修复：把清除动作放到整个领取临界段/会话退出后的finally。其后无await地进入 `_run` 自身try，取消/关停只登记意图并由原执行体安全cancelled/interrupted。
- 回归：真实HTTP生成、Event控制commit返回/会话退出两个窗口，分别owner cancel/stop，共4项；断言持久终态、章节恢复、admitted释放和FakeLLM零调用，均通过。保留原UUID/queued_at、受理意图与独立drain契约。

### F2：角色表单边界与服务端不一致

- 文件：`frontend/src/pages/AdminRolesPage.tsx`。
- 问题：名称UI限制64、说明允许1000，服务端分别100/500；合法长名称无法在UI正常编辑，过长说明只能到提交时422。
- 修复：名称100、说明500，同时对说明加入Form长度规则。保留后端严格schema为最终权威，不修改接口。

### F3：工坊读权撤回后，刷新失败仍留旧管理隐私数据

- 文件：`frontend/src/pages/WorkshopPage.tsx`。
- 问题：仅触发useLoader刷新会保留旧data；workshop.read已撤销时，旧隐藏条目与匿名真实署名在新请求失败后仍可能持续显示。
- 修复：Browse按真实actor ID和workshop.read布尔重建；个人区按actor隔离。撤写不重挂仍合法的只读列表，不因无关revision清空个人分享草稿。
- 证据：主会话已用旧产物、两独立真实会话和工坊列表500故障复现红例；在本报告产物重复相同条件，旧真实署名与隐藏条目均立即消失，账号保持登录，恢复网络后仅公开条目且作者匿名。浏览器细节由主会话独立记录。

### F4：关联人工子表单/移动动作仍有活跃写控件

- 文件：`CareerEditDrawer.tsx`、`CharacterCareersTab.tsx`、`CharacterMembershipsTab.tsx`、`ForeshadowsPanel.tsx`、`ChaptersPanel.tsx`及新增 `components/project/manualReadOnly.test.ts`。
- 问题：职业StageListEditor在Form外且只跟saving禁用；职业卸下、加入组织、伏笔操作下拉、移动章节删除菜单漏接canEdit。后端与事件写前守卫已拒绝提交，但界面并非完整只读。
- 修复：按canEdit禁用上述控件；移动章节编辑文案改为查看。未放宽owner-only模型/向量能力。
- 回归：5项实际组件渲染/事件回调测试，覆盖可写/只读阶段控件、已有组织下的加入按钮、真实保存/卸下/移动删除确认回调零API调用，不是源码字符串断言。浏览器交互仍由主会话完成。

### F5：角色移动端缺少获批的卡片呈现

- 文件：`frontend/src/pages/AdminRolesPage.tsx`、新增 `frontend/src/pages/AdminRolesPage.test.ts`。
- 问题：初版375px仅将760px表格内部横滚，成员与操作隐藏在右侧，偏离已批准移动卡片方案。
- 处理：向主会话报告而非擅改设计。主会话明确裁定补卡片，原前端实现者窄改这两文件，之后交回检查者。
- 核验：移动List卡片、可换行动作/长文本、全宽搜索和紧凑分页；桌面Table保留；两种布局共享内置/在用禁用规则。新增4项渲染回归及完整lint/tsc/Vitest/Vite通过。

### F6：旧logout仍能导航干扰新账号

- 文件：`frontend/src/components/AppLayout.tsx`、新增 `frontend/src/components/AppLayout.test.ts`。
- 问题：store的epoch已防止旧logout清新login，但外壳菜单await后仍无条件navigate('/login')，会干扰新账号所在页面。
- 修复：只在返回时真实会话仍anonymous才导航。
- 回归：捕获实际账号菜单回调，延迟真实auth store的logout，再完成新login；旧响应不清新账号也不导航。普通退出仍正确导航，共2项通过。

### 补充验证：四真实入口同时争最后作品名额

仅在完整pytest之后扩充已有并发测试，**产品代码没有再变**。普通创建持owner User屏障时，同账号灵感转换、预览token备份导入、拆书start均已到达准入；最后一名额仅普通创建成功，其余3个均quota_exceeded。断言作品总数、无多余风格/queued Task、灵感未converted、拆书revision保留、备份token未消费。随后整个 `test_rbac_concurrency.py` **17/17通过**。不把此前完整suite冒称已包含这些后来补充的断言。

## Findings (not fixed) / 未验边界

1. **真实PostgreSQL行锁/独立事务竞争未实测**。用户明确要求本轮暂缓，不启动Docker、不连接backend/.env；这是环境/用户约定，不以SQLite/Event通过替代。生产发布前仍建议补PG角色撤权、互相降超管、四入口最后名额、Role删除/分配、领取/调低等竞争。
2. **既有SQLite User ID + JWT sub/ver实体复用风险未修**。任务方案明确不扩成认证实体迁移；新全局revision只保护新增角色/配额陈旧写，不能声称修复旧JWT身份复用。
3. **标准前端命令的root-owned缓存阻挡仍在**。实现阶段标准npm test/build的EACCES证据见frontend-implementation.md；未chmod/chown/sudo、未修改依赖配置，本检查用原配置的隔离缓存/输出等价完整门禁。不要将标准命令写为成功。
4. **真实浏览器全AC矩阵非本子代理执行**。主会话负责最终产物上的交互、375px/浅暗、快捷键、跨会话撤权、慢请求等；本报告只引用其已明确反馈的工坊红/绿例，不预填其他浏览器成功。
5. 保持单进程TaskEngine边界；全局管理gate与大型owner导入之间的队头阻塞为已批准保守方案代价，不声称多实例租约或高吞吐扩展已完成。

除这些明确边界，没有选择性留下已确认的范围内机械问题；所有新发现均如上修复，或按角色要求先交主会话裁定后补齐。

## 全范围语义核对

- 唯一super事实仍为User.is_admin；Super/ordinary特殊成员投影，异常独立super关联不能提权；复制super仅复制33可委派项，角色/授权入口仍super-only。
- 后端目录固定33项、显式依赖，前端PermissionCode[]和required身份字段镜像；没有基于旧is_admin的授权fallback。逐项真实HTTP正/反授权测试包含获授权后到达业务404的案例，实际业务写另由对应既有套件覆盖，不将404描述为已成功变更资源。
- 管理目标保护在共同gate后重读有效权限；禁用管理目标仍受保护。旧HTTP创建/PATCH is_admin拒绝；bootstrap仅窄内部系统审计，不扩大其他人工子模块采集范围。
- 用户列表/详情、Role/Quota投影通过同一SELECT拼接身份、角色、全局revision；详情保留原聚合单SELECT、无用户列表逐项N+1；不输出密码/hash/内部token_version。
- 固定锁序runtime→auth DB gate→User升序→资源；设置_lock在runtime前。最后super guard在gate下exists，不再在User之后无序锁全体User；两阶段回收drain外释放gate、第二阶段按原primitive token版本重验。
- 人工18服务按read/edit/recycle显式授权；严格owner私有模型、Key、AI/任务输出/重试/继续、MCP、阅读进度和备份边界不扩大。原明确管理审计同事务，失败/未知提交不伪造相反结果。
- Role/Quota对象revision与全局CAS、Role AUTOINCREMENT、普通删号推进全局revision均存在；0029仅新四表，不重建users/projects父表；有新语义数据/审计时阻止有损降级。
- 四Project入口在副作用前取owner User锁，持到原整体提交；0、inherit/unlimited、软删不释放、恢复不扣、超管不豁免一致。
- 候选与admitted区分；真实claim重验用户额度与全局硬顶，runtime跨commit await到内存发布；admitted延续到on_success和真实_on_done。Settings load/reload/recovery统一发布，未知事实暂停新准入且dispatch不热循环；取消/关停领取窗口现覆盖session退出。
- 前端管理路由/菜单/跨域读取、账号动作与目标保护、私有权限刷新/init/公开config分离、403不登出、同actor撤写保读/撤读清理、epoch/旧响应/局部Modal拥有权及不重发均追查实际代码；新发现的漏网控件、工坊旧数据及logout迟到导航已补齐。

## Verification

各命令分别执行。backend前缀为 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 UV_CACHE_DIR=/private/tmp/ainovel-uv-cache uv run --no-sync`；全部测试使用隔离SQLite/FakeLLM，完整pytest以正规escalation允许本机MCP loopback。

| 检查 | 命令 | 结果/证据 |
| --- | --- | --- |
| Backend lint | `ruff check .` | exit 0，最后补充测试后再次通过 |
| Alembic唯一head | `alembic heads` | exit 0：0029 (head)；不是迁移开发数据库 |
| Backend完整测试 | `pytest > /private/tmp/admin-rbac-check-pytest.log 2>&1` | **exit 0，1399 passed，1 warning，744.35s**；完整产品树包含checker任务领取修复 |
| Backend补充竞争 | `pytest -q tests/test_rbac_concurrency.py` | **exit 0，17 passed，16.29s**；完整suite之后仅补充四入口竞争测试，无产品变化 |
| Backend辅助定位 | `pytest -q tests/test_writeback.py -k later_chapter_wins` | exit 0，1 passed，8 deselected，1.32s |
| Frontend完整测试 | `HOME=/private/tmp/admin-rbac-check-vitest-home npm test -- --configLoader runner --no-cache --fsModuleCachePath /private/tmp/admin-rbac-check-vitest-cache` | **exit 0，90文件693/693，28.20s**；`/private/tmp/admin-rbac-check-vitest-final.log` |
| Frontend lint | `npm run lint` | exit 0，0错误/0警告；`/private/tmp/admin-rbac-check-eslint-final.log` |
| App类型 | `./node_modules/.bin/tsc -p tsconfig.app.json --tsBuildInfoFile /private/tmp/admin-rbac-check-app-final.tsbuildinfo` | exit 0 |
| Node类型 | `./node_modules/.bin/tsc -p tsconfig.node.json --tsBuildInfoFile /private/tmp/admin-rbac-check-node-final.tsbuildinfo` | exit 0 |
| 原配置构建 | `./node_modules/.bin/vite build --configLoader runner --outDir /private/tmp/admin-rbac-check-frontend-dist` | exit 0，3.49s；`/private/tmp/admin-rbac-check-vite-final.log` |
| Diff空白检查 | `git diff --check -- backend frontend` | exit 0 |
| 原四文件保护 | 比对implementation-baseline.json的SHA-256 | 全部true，见check-final-snapshot.json |

完整pytest唯一warning是既有Gemini Imagen SDK弃用提示。summary后进程曾延迟退出：2秒只读sample确认在 `Py_FinalizeEx → gc_collect_main`，不是应用Task/DB锁；随后正式exit 0。采样 `/private/tmp/admin-rbac-check-pytest-exit.sample.txt`。没有为此放宽测试或中断测试进程。

前端Node打印既有 `--localstorage-file` 路径警告，但测试全部通过；lint无告警。最大chunk vendor-antd **1147.01kB**，保留原1500kB告警门限；未改依赖或chunk配置。

### 最终前端产物（覆盖本检查自己的中间构建，未清理首份/原实现者final-dist）

- 目录：`/private/tmp/admin-rbac-check-frontend-dist`
- index.html SHA-256：`ebf6c1235c98c12ab685fbc1be9d481e26074bbd099827834e3b7aee41aa4924`
- 入口：`/assets/index-C4bIyr3O.js`，181.32kB（gzip56.89kB）
- 此目录包含最后QuotaPanel、checker全部前端修复和角色移动卡片；主会话已切该实际产物复验。

## 全范围文件清单

下面列出的每项均纳入检查/最终快照。新增测试的实际行为与输出见上文，未以仅fixtures/源码匹配替代安全回归。

### backend（65）

- `backend/alembic/versions/0029_rbac_quotas.py`
- `backend/app/api/v1/__init__.py`
- `backend/app/api/v1/admin.py`
- `backend/app/api/v1/admin_audit.py`
- `backend/app/api/v1/admin_dashboard.py`
- `backend/app/api/v1/admin_quotas.py`
- `backend/app/api/v1/admin_recycle_bin.py`
- `backend/app/api/v1/admin_roles.py`
- `backend/app/api/v1/admin_system_settings.py`
- `backend/app/api/v1/admin_tasks.py`
- `backend/app/api/v1/auth.py`
- `backend/app/api/v1/workshop.py`
- `backend/app/core/authorization_runtime.py`
- `backend/app/core/database.py`
- `backend/app/core/deps.py`
- `backend/app/core/permissions.py`
- `backend/app/main.py`
- `backend/app/models/__init__.py`
- `backend/app/models/rbac.py`
- `backend/app/schemas/admin.py`
- `backend/app/schemas/audit.py`
- `backend/app/schemas/auth.py`
- `backend/app/schemas/rbac.py`
- `backend/app/services/admin_service.py`
- `backend/app/services/admin_task_service.py`
- `backend/app/services/audit_service.py`
- `backend/app/services/audit_writer.py`
- `backend/app/services/auth_service.py`
- `backend/app/services/authorization_service.py`
- `backend/app/services/book_import/service.py`
- `backend/app/services/career_service.py`
- `backend/app/services/cast_service.py`
- `backend/app/services/chapter_service.py`
- `backend/app/services/chapter_version_service.py`
- `backend/app/services/cover_service.py`
- `backend/app/services/foreshadow_service.py`
- `backend/app/services/inspiration_service.py`
- `backend/app/services/kb_service.py`
- `backend/app/services/memory_service.py`
- `backend/app/services/organization_service.py`
- `backend/app/services/outline_service.py`
- `backend/app/services/outline_snapshot_service.py`
- `backend/app/services/plan_service.py`
- `backend/app/services/project_service.py`
- `backend/app/services/project_transfer/importer.py`
- `backend/app/services/quota_service.py`
- `backend/app/services/recycle_bin_service.py`
- `backend/app/services/relationship_service.py`
- `backend/app/services/role_service.py`
- `backend/app/services/skill_service.py`
- `backend/app/services/story_service.py`
- `backend/app/services/system_settings_service.py`
- `backend/app/services/task_engine/engine.py`
- `backend/app/services/workshop_service.py`
- `backend/tests/conftest.py`
- `backend/tests/rbac_helpers.py`
- `backend/tests/test_admin.py`
- `backend/tests/test_admin_audit_logs.py`
- `backend/tests/test_admin_user_details.py`
- `backend/tests/test_project_lifecycle.py`
- `backend/tests/test_rbac.py`
- `backend/tests/test_rbac_concurrency.py`
- `backend/tests/test_rbac_migrations.py`
- `backend/tests/test_rbac_permissions.py`
- `backend/tests/test_user_quotas.py`

### frontend（108）

- `frontend/src/App.tsx`
- `frontend/src/api/admin.test.ts`
- `frontend/src/api/auditLogs.ts`
- `frontend/src/api/auth.ts`
- `frontend/src/api/client.ts`
- `frontend/src/api/roles.test.ts`
- `frontend/src/api/roles.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/AppLayout.test.ts`
- `frontend/src/components/AppLayout.tsx`
- `frontend/src/components/RequirePermission.tsx`
- `frontend/src/components/adminTasks/AdminTaskViews.tsx`
- `frontend/src/components/adminTasks/adminTasksController.test.ts`
- `frontend/src/components/adminTasks/adminTasksController.ts`
- `frontend/src/components/adminTasks/adminTasksUI.test.ts`
- `frontend/src/components/adminTasks/useAdminTasks.ts`
- `frontend/src/components/adminUsers/AdminUserActionButtons.tsx`
- `frontend/src/components/adminUsers/AdminUserActionDialog.tsx`
- `frontend/src/components/adminUsers/RoleTags.tsx`
- `frontend/src/components/adminUsers/UserQuotaPanel.tsx`
- `frontend/src/components/adminUsers/UserRolesButton.tsx`
- `frontend/src/components/adminUsers/adminUserActionsController.test.ts`
- `frontend/src/components/adminUsers/adminUserActionsController.ts`
- `frontend/src/components/adminUsers/adminUserDetailsUI.test.ts`
- `frontend/src/components/adminUsers/useAdminUserActions.ts`
- `frontend/src/components/auditLogs/AuditCleanupModal.tsx`
- `frontend/src/components/auditLogs/AuditLogDetailDrawer.tsx`
- `frontend/src/components/auditLogs/auditLogsUI.test.ts`
- `frontend/src/components/careers/CareerEditDrawer.tsx`
- `frontend/src/components/careers/CharacterCareersTab.tsx`
- `frontend/src/components/chapter/CastSelect.tsx`
- `frontend/src/components/chapter/VersionDrawer.tsx`
- `frontend/src/components/dashboard/dashboardUI.test.ts`
- `frontend/src/components/dashboard/useAdminDashboard.ts`
- `frontend/src/components/entities/CharacterMembershipsTab.tsx`
- `frontend/src/components/entities/CharacterRelationsTab.tsx`
- `frontend/src/components/foreshadows/ForeshadowEditDrawer.tsx`
- `frontend/src/components/kb/KbSearchDebug.tsx`
- `frontend/src/components/kb/MemoryEditModal.tsx`
- `frontend/src/components/kb/RetrievalSettingsCard.tsx`
- `frontend/src/components/outlines/OutlineSnapshotDrawer.tsx`
- `frontend/src/components/outlines/PlanList.tsx`
- `frontend/src/components/project/CoverPanel.tsx`
- `frontend/src/components/project/DeleteProjectModal.tsx`
- `frontend/src/components/project/RecycleProjectModal.tsx`
- `frontend/src/components/project/adminProjectsUI.test.ts`
- `frontend/src/components/project/manualReadOnly.test.ts`
- `frontend/src/components/project/recycleBinUI.test.ts`
- `frontend/src/components/relations/RelationEditModal.tsx`
- `frontend/src/components/relations/RelationTypeManager.tsx`
- `frontend/src/components/skills/SkillCard.tsx`
- `frontend/src/components/skills/SkillEditDrawer.tsx`
- `frontend/src/components/skills/skillsMcpUI.test.ts`
- `frontend/src/components/systemSettings/systemSettingsController.test.ts`
- `frontend/src/components/systemSettings/systemSettingsController.ts`
- `frontend/src/components/systemSettings/systemSettingsUI.test.ts`
- `frontend/src/components/systemSettings/useSystemSettings.ts`
- `frontend/src/components/workshop/ReviewPanel.tsx`
- `frontend/src/components/workshop/ShareToWorkshopModal.tsx`
- `frontend/src/components/workshop/WorkshopItemControls.tsx`
- `frontend/src/components/workshop/WorkshopItemDrawer.tsx`
- `frontend/src/components/workshop/workshopUI.test.ts`
- `frontend/src/hooks/useAuthorizationCleanup.ts`
- `frontend/src/hooks/useAuthorizationScope.ts`
- `frontend/src/hooks/useProjectAccess.ts`
- `frontend/src/pages/AdminAuditLogsPage.tsx`
- `frontend/src/pages/AdminDashboardPage.tsx`
- `frontend/src/pages/AdminProjectsPage.tsx`
- `frontend/src/pages/AdminRecycleBinPage.tsx`
- `frontend/src/pages/AdminRolesPage.test.ts`
- `frontend/src/pages/AdminRolesPage.tsx`
- `frontend/src/pages/AdminSystemSettingsPage.tsx`
- `frontend/src/pages/AdminTasksPage.tsx`
- `frontend/src/pages/AdminUserDetailsPage.tsx`
- `frontend/src/pages/AdminUsersPage.tsx`
- `frontend/src/pages/SkillsPage.tsx`
- `frontend/src/pages/WorkshopPage.tsx`
- `frontend/src/pages/project/CareersPanel.tsx`
- `frontend/src/pages/project/ChapterEditorPage.tsx`
- `frontend/src/pages/project/ChaptersPanel.tsx`
- `frontend/src/pages/project/CharactersPanel.tsx`
- `frontend/src/pages/project/ForeshadowsPanel.tsx`
- `frontend/src/pages/project/KnowledgePanel.tsx`
- `frontend/src/pages/project/MemoriesPanel.tsx`
- `frontend/src/pages/project/OrganizationsPanel.tsx`
- `frontend/src/pages/project/OutlinesPanel.tsx`
- `frontend/src/pages/project/OverviewPanel.tsx`
- `frontend/src/pages/project/ProjectLayout.tsx`
- `frontend/src/pages/project/RelationshipsPanel.tsx`
- `frontend/src/pages/project/WorldPanel.tsx`
- `frontend/src/router.tsx`
- `frontend/src/stores/aiConfigs.ts`
- `frontend/src/stores/auth.test.ts`
- `frontend/src/stores/auth.ts`
- `frontend/src/stores/privateCaches.test.ts`
- `frontend/src/stores/project.test.ts`
- `frontend/src/stores/project.ts`
- `frontend/src/stores/skills.ts`
- `frontend/src/stores/styles.ts`
- `frontend/src/utils/adminUsers.test.ts`
- `frontend/src/utils/adminUsers.ts`
- `frontend/src/utils/auditLogs.ts`
- `frontend/src/utils/permissions.test.ts`
- `frontend/src/utils/permissions.ts`
- `frontend/src/utils/projectAccess.test.ts`
- `frontend/src/utils/projectAccess.ts`
- `frontend/src/utils/quotas.ts`
- `frontend/src/utils/roles.ts`



## 提交时补充（主会话记录）

2026-11-02暂存完整新增文件后，cached diff空白检查补发现quotas.ts/roles.ts各多1个EOF LF；原unstaged diff未包含当时未跟踪文件。独立checker仅删这两个LF，两文件ESLint和diff-check通过；主会话比对确认其余字节不变，并单独提交39133fa，不amend原功能c9e4045。原最终hash清单与全套/浏览器证据保留历史身份，当前只这两处非语义差异；没有为EOF清理重跑产品全套。
