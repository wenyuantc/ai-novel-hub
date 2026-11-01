# 规范摘录

权威来源：`.trellis/spec/frontend/component-guidelines.md`。仅截取本任务相关段落；完整规范以原文件为准。

## 原文第 121–147 行

### 页面模式

- **面板 CRUD**（`WorldPanel`、`CharactersPanel`、`OutlinesPanel`、`ChaptersPanel`）：`const pid = useProjectId()`；`const loader = useCallback(() => worldApi.list(pid), [pid])`；`const { data: items = [], loading, reload } = useLoader(loader)`；在 `Modal` 中使用 AntD `Form.useForm` 进行创建/编辑；用 `Popconfirm` 删除；每次变更成功后调用 `reload()`。
- **项目级数值**：在会改变字数/章节数的变更之后，调用 `useProjectStore((s) => s.refresh)`（`ChaptersPanel`、`OutlinesPanel`、章节编辑器）。`OverviewPanel` 通过 `setProject(updated)` 回写 PATCH 结果。
- **破坏性确认**使用 `modal.confirm`（`ProjectListPage` 中的项目删除、编辑器中覆盖已有正文的重新生成）或 `Popconfirm`（行删除）。
- **路由参数**：`:pid` 使用 `useProjectId()`；用 `Number.isFinite` 拦截无效 id，并渲染 `<Result status="404" …/>`。

### 用户管理页（`src/pages/AdminUsersPage.tsx`）

同级的 `AdminProjectsPage` 提供管理员正常项目列表，`AdminRecycleBinPage` 管理已删除作品；从正常列表进入现有编辑区，归属提示、人工管理能力与自动任务/Reader进度隔离遵守[管理员作品管理跨层契约](../backend/admin-projects.md)。不得通过切换认证用户复用页面。

- AntD `Table`，服务端分页（`adminApi.listUsers({ q, page, page_size })`，`q` 会 trim，为空时省略）并带用户名搜索框。列：用户名（自己那一行标记 `我`）、角色、状态、作品数（含回收站）、`formatTime(created_at)`、操作。作品数非零时提供按 owner 筛选的正常作品/回收站链接。
- `新建用户` 弹窗：用户名、两次输入初始密码、`设为管理员` 复选框。重置密码弹窗：两次输入新密码。
- 启用/禁用与授予/撤销管理员都经过 `modal.confirm`。名下有任何作品（含回收站/待清理）时 `rowActions.canDelete=false`，提示可以先禁用或先清理作品；服务器的 409 仍是最终保护。只有零作品用户才能打开删号弹窗，且 `canConfirmDelete(input, username)` 严格相等（不 trim/大小写折叠）才可确认。并发冲突中的 `project_count` 仅经 `userProjectCountFromError` 收窄读取，更新旧弹窗并禁用确认；删掉某页最后一行时回退一页。
- `rowActions(row, currentUserId)` 会禁用当前用户自己那一行的**所有**操作（tooltip 为 `不能对自己执行该操作`）——包括重置密码，因为递增 `token_version` 会让管理员自己被登出；自己改密码应通过 `修改密码`。
- 测试仅在 node 环境运行（无 jsdom）：`utils/adminUsers.test.ts`（守卫 + 行操作表 + 删除匹配）、`api/admin.test.ts`（URL/method/body/`credentials: 'include'`、400/403/409 的 `detail` 透传）、`utils/validation.test.ts`。

### 管理员作品回收站（`src/pages/AdminRecycleBinPage.tsx`）

- `/admin/recycle-bin` 是 `RequireAdmin` 保护的 lazy 路由；桌面/移动导航、账户菜单和页名一起维护。传输与生命周期完整契约见[作品回收站](../backend/recycle-bin.md)。
- `recycleBinApi` 独占列表/恢复/purge 请求；搜索、owner 筛选、分页与末页回退沿用 `useLoader`。桌面表格允许内部横滚，375px 使用卡片，均不展示正文或私人配置。
- `RecycleProjectModal` 普通恢复确认不自动续跑；purge 要求完整名称逐字相等并明确不可恢复。`deleted_at` 原始微秒字符串直接回传，不能经 Date/格式化时间重建；后端再次判断期限、代次和权限。
- 忙碌期间同步 ref 防重复提交并禁用关闭/取消；组件卸载、目标代次变化、管理员身份变化使旧响应失效，成功后才刷新列表并目标级 `discard` / `discardProject`。
- 所有普通作品删除入口共用 `DeleteProjectModal`，统一引用 `PROJECT_DELETE_NOTICE`：移入回收站、保留30天、管理员恢复、自动取消任务并等待、超时作品未删除且已取消任务不自动复原。不能继续把普通删除描述为“永久删除”。
- 前端仅用 `restorable` 和本地时钟提前禁用明显到期的恢复按钮，最终以后端 UTC 时间为准。测试覆盖精确名称、旧响应/会话失效、到期边界、API请求体与分页；静态UI测试不能代替真实浏览器取消/恢复和移动布局验收。

### 章节编辑器（`src/pages/project/ChapterEditorPage.tsx`）

## 原文第 200–237 行

## Props 约定

- Props 是本地 `interface Props`（不导出），在函数签名中解构，默认值内联写出（`existingCount = 0`、`maxHeight = 360`）。
- 受控值组件遵循 AntD 的 `value` / `onChange` 形式，以便在 `Form.Item` 中使用（`AIConfigSelect`）。
- 泛型组件以类型参数携带向导类型，使 `onDone` 按类型区分：`WizardGenerator<K extends WizardKind>` → `onDone?: (items: WizardItemMap[K][]) => void`。
- 回调是可选的（`onDone?.(…)`、`onRunningChange?.(…)`），除非父组件必须响应（`WizardModal` 要求 `onClose`/`onDone`）。
- 派生类型使用 `Omit<Props, 'open'>`，而不是重复定义接口（`ExportModalInner`）。

---

## 样式模式

- AntD 布局原语（`Layout`、带 `xs`/`xl` 断点的 `Row/Col`、`Space direction="vertical"`、`Card size="small"`）加上内联 `style={{ … }}` 对象。不使用 CSS modules、Tailwind 或 styled-components。
- 共享的内联样式常量定义在模块级：`const pre: CSSProperties = { whiteSpace: 'pre-wrap', marginBottom: 0 }`（`StoryPreview.tsx`）。模块级常量只放与主题无关的属性；背景 / 边框 / 文字颜色在组件内用 token 合并（`style={{ ...pre, background: token.colorFillQuaternary }}`）。
- 主题定制通过 `studioPalette(resolved)` 同步 `ConfigProvider theme` 和 CSS 变量（`algorithm` 随明暗切换）；**颜色一律取 `theme.useToken()` 或 `theme/palette.ts`，不得写死**，见下文「主题与配色」。
- 状态颜色来自标签映射（`CHAPTER_STATUS[s].color`、`PROJECT_STATUS[status].color`），以 `<Tag>` 渲染。

---

## 无障碍

项目依赖 AntD 内置的语义（带标签的 `Form.Item`、可键盘操作的 `Button`/`Menu`/`Modal`）。几乎所有按钮都使用可见的文字标签。已知缺口：编辑器的返回按钮只有图标（`<Button type="text" size="small" icon={<ArrowLeftOutlined />} …/>`），没有 `aria-label`；新增纯图标按钮时，请添加 `aria-label` 或 `title`。

---

## 常见错误

- 使用 `antd` 的静态 `message`/`Modal` 而不是 `App.useApp()`——会丢失语言环境/主题上下文。
- 允许用户编辑或保存状态为 `generating` 的章节——后端会返回 409；保留 `stuck` 的禁用逻辑。
- 带着未保存的编辑开始 AI 生成——生成读取的是服务端内容；务必像 `runAI` 那样先 `save()`。
- 在只注册一次的 `keydown` 监听器中直接捕获 `handleSave`——会形成过期闭包；使用 ref 模式。
- 在流式过程中关闭 `WizardModal`——这是刻意禁止的；不要添加 `maskClosable` 或页脚关闭按钮。
- 在关闭状态下渲染 `ExportModalInner`——它只会加载一次章节并显示过期数据。
- 写死颜色（`'#fafafa'`、`'#f0f0f0'`、`rgba(...)`）——暗色主题下会留下浅色块；`noHardcodedColors.test.ts` 会失败。
- 用 `window.innerWidth` 判断移动端——不随窗口缩放更新；一律用 `useIsMobile()`。
- 给控件写固定的大 `minWidth`（如 `minWidth: 320`）——375px 下会撑出横向滚动；用 `min(320px, calc(100vw - 130px))` 之类的上限。

### 章节分析（`10-23-analysis-memory`）

## 原文第 514–535 行

### Skill 与 MCP 插件页（`10-23-skills-mcp`）

后端契约见 [../backend/skills-mcp.md](../backend/skills-mcp.md)。

- **入口与懒加载**：`StudioNavigation` 创作资源分组提供「写作技能」（`/skills`）与「MCP 插件」（`/mcp-plugins`），桌面侧栏与移动抽屉共用，两页都是 `router.tsx` 的 `lazyPage` 路由，
  代码不进入口 chunk。**MCP 关闭时隐藏入口**：`useAuthStore.mcpEnabled` 取自 `GET /auth/config` 的 `mcp_enabled`（读取失败按开启处理），为 false 时共用导航与
  智能体面板都不渲染 MCP 相关控件；直接访问 `/mcp-plugins` 时列表请求 403，页面显示错误 `Alert` 并禁用「添加插件」。
- **`pages/SkillsPage`**：`useSkills()` 读 store；Tabs「我的（N）」/「全局（N）」，卡片网格（`Row` / `Col xs=24`，375 宽度无固定宽度）。普通用户在全局 Tab 看到说明
  「全局 Skill 由管理员维护，只读；复制为我的之后可以修改」，且全局 Tab 上不显示新建 / 导入按钮；管理员在全局 Tab 新建 / 导入的归入全局，其余归入「我的」。
  导入用 `Upload`（`showUploadList={false}`，`beforeUpload` 里调 `skillApi.import`，返回 false 阻止自动上传）。任何变更后 `useSkillStore.load(true)`。
- **`skills/SkillCard`**：按 `skill.editable` 决定操作——不可写（全局 + 普通用户）只有「查看 / 复制为我的 / 导出」；可写的有「编辑 / 导出 / 删除」；
  内置 Skill 且管理员时另有「恢复内置内容」（Popconfirm）。前端**不自己判断归属**，只看服务端给的 `editable`。
- **`skills/SkillEditDrawer`**：`useModalWidth(760)`；新建时 `SkillForm`，编辑时先 `skillApi.get` 再以 `key = updated_at` 挂载表单。字段：归属（仅新建且管理员可选）、
  标识（`suggestSlug` 由名称给建议）、名称、说明、分类、触发词（`Select mode=tags`，提交前 `normalizeTriggers`）、正文 Tabs「编辑 / 预览」（预览复用
  `agent/AgentMarkdown`，不引入 markdown 库）、参考资料列表（增删、上下移动）。保存 = PATCH 基本字段 + 参考资料有变化（`refsKey` 比较）时
  `PUT /references` 整体替换。不可写时整个表单只读，底部给「复制为我的」。
- **`skills/SkillSelect`**（复用点；选项标签为「名称（slug）」，因为副本和全局 Skill 常同名）：`{ value?: number | null; onChange?; disabled?; placeholder? = '不使用 Skill'; style?; 'aria-label'? }`，`useSkills()` 加载，
  `skillSelectOptions` 分「我的」「全局」两组，可清空（= null = 不使用 Skill）、可搜索。用于：章节编辑器 AI 助手（一个 `skillId` 状态，生成 / 润色 / 局部改写
  `RewritePanel.skillId` / 按建议重写都带 `skill_id`）、`BatchGenerateModal`（表单字段 `skill_id`）、智能体输入区。新增「可选 Skill」的入口一律复用它，不另写下拉。
- **生成记录与任务**：`GenerationLogList` 用 `skillRef(params.skill)` 收窄后显示所用 Skill；`TaskPanel` 行与 `TaskOutputModal` 在 `task.skill` 存在时显示「Skill：名称」Tag（创建时的快照）。
- **`pages/McpPluginsPage`**：卡片网格（`Col xs=24 md=12 xl=8`），达到 `MAX_MCP_PLUGINS`（10）时禁用添加；刷新失败时 `message.error` 后再 `GET` 一次插件以显示最新 `last_error`。
- **`mcp/McpPluginCard`**：名称、传输类型、地址（长地址 `overflowWrap: anywhere`）、`pluginStatus`（连接失败 > 已连接 > 未测试）、工具数量、启用 `Switch`；
