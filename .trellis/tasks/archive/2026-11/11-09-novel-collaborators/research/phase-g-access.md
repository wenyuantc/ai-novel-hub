# 阶段 G1：作品能力与异步作用域

## 范围和接口

- `src/api/types.ts` 的 `Project.access` 必填，`ProjectAccess` 镜像后端 `ProjectAccessOut` 的 `role`、`is_collaborator`、`authorization_version`、`allow_owner_ai` 和全部能力布尔值。协作者授权版本必须为非空字符串，其他来源为 null；旧响应缺少任何能力字段时运行时失败关闭，未把生产类型改为 optional 迁就旧测试数据。
- `projectAccess(project, actor, projectId)` 使用服务端能力布尔值，不通过 `role` 推导普通人工、导出、备份、进度及协作者管理能力；`role` 只用于展示和既有“是否管理他人作品”的 UI，作者身份也要求与可得的 owner ID 一致。`isProjectAccess` 对缺失或错误类型快照失败关闭。
- `useProjectAccess` 提供 `ready`、`role`、`isCollaborator`、`allowOwnerAI`、`managingOther` 及 `canRead`、`canEdit`、`canRecycle`、`canExport`、`canManageCollaborators`、`canBackup`、`canWriteReadingProgress` 与对应 `*Now()`。`canUseAIEntitled` / `canUseAIEntitledNow()` 返回服务端 AI 能力。
- **临时阶段门禁**：现有 UI 的 `canUseAI` / `canUseAINow()` 仍仅允许作者，避免在 E 完成前意外挂载 editor 的 Agent、任务或 AI 入口。E/G3 完成后需按真实 `canUseAIEntitled` 重新接线并验证，当前不代表 AI 前端已完成。
- `captureProjectScope(projectId)` 获取当前认证身份、项目和能力代次；`isProjectScopeCurrent(scope, capability?)` 在 await 后验证当前作用域及指定能力。`setProject(project, scope)` 强制有效 scope，null、旧登录、旧项目、旧授权或旧能力的响应均被拒绝。纯函数 `projectAccess()` 只解析传入快照，不能证明该快照属于当前 actor；独立 Reader 等不走共享 store 的页面必须自行绑定请求身份和代次。
- 项目 store 接受真实协作者快照。请求使用 actor、RBAC 版本、会话 generation、项目和请求序号绑定；同项目的较早 load 与 refresh 不得覆盖后发请求。认证变化时立即使旧 scope 失效，仅保留服务器旧快照中仍有独立来源的能力，并重新获取真实快照；临时网络错误保留已有作品，明确 403/404 清空。权限降级后即使恢复，旧 scope 仍不可复活。协作者撤权若发生在另一会话，前端仍需通过刷新、服务端拒绝或后续通知得知；最终授权由后端负责。
- 前端把后端新增的不透明 `authorization_version` 纳入作用域比较：普通刷新版本和能力均相同时保留 scope 和表单，重新邀请后版本变化会淘汰旧回调。后端按作品、真实 actor、邀请 UUID 和 revision 生成摘要，仅 accepted 协作者获得非空版本，作者及纯管理员为 null；版本不作为后端授权凭据。在客户端获知远端变化之前仍依赖服务端授权。
- 授权版本或能力确实变化时 scope 回调随之变化；当前 `useSceneEditor` 会重建草稿控制器，`RelationEditModal` 的加载 effect 会重置表单，仍需页面侧确保授权变化时未保存草稿的处理方式并做浏览器回归。
- 生产写响应作用域调用点由 G3 页面代理接入：`OverviewPanel`（项目保存、自动分析开关）、`WizardPage`（向导进度）、`CoverPanel`（封面保存）；`CoverGenerateModal` 在发送前捕获并传递封面 scope。不得添加无 scope 的 `setProject` 调用。

## 验证和余项

- `npm run test -- src/stores/project.test.ts src/hooks/useProjectAccess.test.ts src/utils/projectAccess.test.ts src/utils/permissions.test.ts src/utils/shelf.test.ts src/components/project/adminProjectsUI.test.ts`：47 passed（新增 hook 授权轮次用例前）。包括真实 store 的协作者、管理员+viewer 降级、项目/身份切换、同账号退出重进、403/404、旧回调/刷新、同项目 load/refresh 逆序、授权轮次变化与能力降级再恢复；hook 静态渲染测试验证实际返回的即时回调。Node 静态渲染不替代真实浏览器卸载/effect 测试。
- 随后 `npm run test -- src/hooks/useProjectAccess.test.ts`：3 passed，覆盖同能力的新邀请使旧 hook 回调失效、同版本刷新仍有效。授权版本字段接入后所属文件的定向 `npx eslint` 和 `git diff --check` 均通过。
- 后端真实 HTTP：`LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_access_version.py`：1 passed；覆盖作者、纯管理员、协作者详情和书架、模型授权变更及同能力重新邀请。修改范围 Ruff 通过；尚未替代完整跨层或浏览器验证。
- 增加授权版本前的 `npx tsc -b --pretty false`、所属类型/store/hook/工具及测试的 `npx eslint`、完整 `npm run lint` 和 `npm run build` 均通过。授权版本字段及其他代理的 fixture 修改之后需要错峰重跑最终全量检查。
- Reader 的本地数据和请求作用域、viewer 快捷键/订阅/弹窗卸载、其他实体页人工只读及 AI 副作用、成员面板、全量 lint/test/build 与真实浏览器均由 G2/G3/G5 和最终门禁负责；本记录不把它们标为完成。
