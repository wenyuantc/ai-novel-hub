# 本任务规范摘录：.trellis/spec/frontend/component-guidelines.md

为避免上下文注入截断，以下从当前原规范逐字摘录本任务相关段落。原文件是权威来源；若实施涉及摘录外内容，直接读取原规范相关部分。

- 提取日期：2026-10-30
- 来源 SHA-256：`8c04dbacb3aa8eff4859e3873249d58737c357c5c1939ee0ff80a8e048aa5415`

## 原文 .trellis/spec/frontend/component-guidelines.md:17–67

## 概述

表单、按钮、菜单与浮层沿用 Ant Design 5；知页公开首页、外壳和书库的布局、导航链接、状态筛选与装帧使用语义 DOM 和有作用域的 CSS，基础页面组件在 `StudioUI.tsx`。语言环境为中文：`App.tsx` 中使用 AntD `zh_CN`，`main.tsx` 中使用 dayjs `zh-cn`。反馈（`message`、`modal`）始终来自 `App.useApp()`，以继承 `ConfigProvider` 的主题/语言环境——绝不使用静态导入的 `message.success(...)`。

```tsx
// src/App.tsx
const colors = studioPalette(resolved)
<ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: colors.primary } }}>
  <AntApp>
    <RouterProvider router={router} />
  </AntApp>
</ConfigProvider>

// src/main.tsx
import 'dayjs/locale/zh-cn'
dayjs.locale('zh-cn')
```

---

## 组件结构

函数组件使用 default export、本地 `interface Props`，以及一行描述用途的中文 JSDoc。数据加载使用 `useLoader` 配合经 `useCallback` memo 的 loader；变更操作是带 `try/catch/finally` 的 `async` 处理函数，出错时显示 `message.error(errorMessage(err))`。

### 可复用组件（`src/components/`）

| 组件 | 契约（源自源码） |
|-----------|------------------------|
| `AppLayout` | 知页外壳：216px 浅侧栏、56px 顶栏、面包屑与 `<Outlet/>`。`StudioNavigation` 按 `/projects/:pid` 切换全局/项目分组，桌面与移动抽屉共用一份定义，MCP 入口遵循 `mcpEnabled`；项目名称只读 `cachedProject.id === pid` 的缓存。侧栏新建跳 `/projects?create=1`；真实账号菜单保留改密/退出，管理员才显示用户管理。任务计数取 `useTasksStore.active.length`，只在 loaded 后显示，侧栏控制始终挂载的 `<TaskPanel open onOpenChange hideTrigger/>`。移动导航按 `location.key` 关闭，避免在导航 effect 中同步 setState。 |
| `StudioUI` | `StudioBrand({onClick?})` 在官网页头/页脚与工作区共用，始终链接公开 `/`；hover/focus 显式保持主题前景色，避免 AntD 全局链接悬停色降低暗色可读性，focus-visible 保留主色轮廓。`StudioPage({title, hint?, action?, children})` 提供 `<main>`、h1 与页面边距；`StudioPanel({children})` 为无阴影纸面；`StudioEmpty({title, text, action?})` 提供空态与下一步操作；`ProjectBadge({status})` 使用既有 `PROJECT_STATUS` 枚举标签。 |
| `RequireAuth` | 路由守卫：`status === 'unknown'` → `<FullPageSpin/>`；`'anonymous'` → `<Navigate to="/login" replace state={{ from: location.pathname + location.search }} />`。`LoginPage` 读取 `state.from`（默认 `/projects`）。 |
| `RequireAdmin` | 包裹 `/admin/users`（位于 `RequireAuth` 内部）。判定逻辑是 `utils/adminUsers.ts` 中的纯函数 `adminGuard(status, user)`：`'loading'` → `<FullPageSpin/>`，`'login'` → `/login`，`'home'`（非管理员）→ `/`，`'allow'` → children。`is_admin` 只从 `/auth/me` 读取一次；被降级的管理员在刷新前仍会看到菜单，但 API 调用会返回 403 并显示提示信息。 |
| `ChangePasswordModal` | `{ open; onClose }`。原密码 + 两次输入新密码（`utils/validation`）。成功 → `其他设备上的登录已失效`；后端会重新签发 cookie，因此当前标签页保持登录。400 `原密码不正确` 通过 `errorMessage(err)` 在表单内显示，不会触发全局登出。 |
| `AIConfigSelect` | `{ value?: number \| null; onChange?: (v: number \| null) => void; disabled?: boolean }`。挂载时调用 `useAIConfigStore().load()`。空选择 = `null` = "使用默认配置"（placeholder 为 `默认：{name}（{model}）`）。加载完成且配置数为零时，渲染一个链接到 `/settings/ai` 的警告 `Alert` 以代替下拉框。 |
| `StreamOutput` | `{ text; running; placeholder?; maxHeight? = 360 }`。`pre-wrap` 等宽文本框，`running` 时自动滚动到底部，运行中显示一个小的 `Spin`。 |
| `WizardGenerator<K extends WizardKind>` | `{ projectId; kind: K; existingCount?; showResult?; onDone?(items: WizardItemMap[K][]); onRunningChange?(running) }`。走**后台任务**：`runWizard(...)` 返回 202 `Task` → `useTasksStore.track(task)` → `useTaskStream().follow(task)`；完成时用 `parseWizardDone(kind, result)` 取 items。**挂载时**按 `GET /tasks?project_id&kind=wizard.{kind}&active=true&limit=1` 查活动任务并自动接上（关闭页面 / 弹窗后再打开可继续看实时输出）。`running = 订阅中 \|\| 创建请求中`，表单禁用；`useHasAIConfig() === false` 时开始按钮禁用；运行中显示危险样式按钮（排队时为 `取消排队`，否则 `停止`），调用 `task.cancel()`（`POST /tasks/{id}/cancel`）。排队中显示 `queueLabel(task)` 的 info `Alert`，运行中显示进度条 + `stage`，断线重连时显示警告。取消时显示 `已停止生成，原有数据未改变`；失败 / 中断时 `生成失败` Alert。`onRunningChange` 通过 ref + `[running]` effect 上报。`existingCount > 0` 时显示"将会替换"的 `Alert`。`WizardPage` 的步骤初始取 `?kind=`（没有时按 `resumeWizardStep(project.wizard_progress)` 定位），之后每次导航（`location.key` 变化，含跳到同一 `kind`）都在渲染期按 `kind` 切换步骤。大纲步骤在已有大纲时显示「覆盖现有大纲 / 追加到末尾」切换（`outline_mode`，覆盖提示会先自动存快照、章节只解除关联；追加提示从下一卷下一章继续），追加成功文案 `大纲已追加 N 条`。 |
| `WizardModal<K>` | 将 `WizardGenerator` 包在 `Modal` 中（`maskClosable={false}`、`destroyOnHidden`、`footer={null}`）。运行中拒绝关闭（`message.warning('正在生成，请等待完成或先停止')`）——任务本身不会因关闭而中断，但面板需要 `onDone` 回调来刷新列表；重新打开会自动接上。完成时：先 `onDone(items)` 再 `onClose()`。被 `WorldPanel`、`CharactersPanel`、`OutlinesPanel` 使用。 |
| `TaskPanel` | `{ open?: boolean; onOpenChange?: (open: boolean) => void; hideTrigger?: boolean }`。受控时同时传 open/onOpenChange；未传时保留内部开关。`hideTrigger` 只隐藏原 FloatButton，不卸载轮询。挂载时 `useTasksStore.startPolling()`（卸载时停止）；AppLayout 通过侧栏打开 `Drawer`（宽 520）：Tab「进行中（N）」= store 的 `active`；Tab「最近」= `taskApi.list({ active: false, limit: 50 })`，依赖 store 的 `version`（任务结束 / 本地操作后自动重载）。每行：标题、`TASK_STATUS` 标签、活动任务的 `Progress`、`queueLabel` 或 `stage`、创建 / 结束时间（`formatTime`）、已结束任务的 `error`。操作：成功且 kind 在 `components/entities/taskResultViewers.ts` 注册了结果组件时显示 `查看结果`（`TaskResultModal`，见下方「设定类组件」）；`查看输出`（`TaskOutputModal`：活动任务用 `useTaskStream().follow` 实时显示，已结束读 `/tasks/{id}/output`）、`跳转`（`taskLink`：章节任务 → 编辑器（`chapter.analyze` / `chapter.revise` 带 `?panel=analysis` 打开分析抽屉）；向导任务 → `/projects/{pid}/wizard?kind=…`；`character.generate` / `relations.parse` → 角色页；`organization.generate` → 组织页；`career.generate` → 职业页；`outline.continue` / `outline.expand` → 大纲页；`wizard.careers` → 向导职业步骤）、活动任务 `取消`（Popconfirm → `track(返回的 TaskOut)`）、可重试状态 `重试`（→ `track(新任务)`）、已结束 `删除`（→ `bump()`）。抽屉顶部 `刷新`、`清理已结束`（Popconfirm → `DELETE /tasks?finished=true` → `已清理 N 个已结束任务`）。 |
| `ExportModal` | `{ projectId; open; onClose }`。仅在 `open` 时渲染 `ExportModalInner`（使其 `useLoader` 每次打开都获取最新章节）。格式单选 `txt`/`md`/`epub`；选 EPUB 时显示作者名（默认当前用户名，≤100，留空 = 不署名）与「包含简介页」（默认勾选）；行选择会禁用 `generating` 状态的章节；不选则导出全部。调用 `exportProject(pid, format, {chapterIds, author, includeSynopsis})` 并显示 `已导出：{filename}`；返回的 `warnings`（`X-Export-Warnings`，如跳过的空章节）用 `message.warning` 显示。被 `OverviewPanel` 使用。 |
| `AutoGrid` | `{ children; minItemWidth? = 260; gap? = 16; mobileColumns? = 1; style? }`。按**容器宽度**自适应列数的 CSS grid：桌面 `repeat(auto-fill, minmax(min(<minItemWidth>px, 100%), 1fr))`，容器比最小宽度还窄时收缩到 100% 不溢出；移动端（`useIsMobile`）固定 `mobileColumns` 列。用于项目页里的卡片 / 统计网格（角色、职业卡片；伏笔统计 `minItemWidth={96} mobileColumns={3}`）。 |
| `StoryPreview` | 具名导出 `WorldPreview`、`CharacterPreview`、`OutlinePreview`（使用 `utils/outline.ts` 中的 `groupOutlines`/`volumeLabel`）。 |
| `TextDiff` | `{ a; b; maxHeight? = 480 }`。逐字（按码点，`utils/textDiff.ts` 的 `computeDiff` → jsdiff `diffChars`，`timeout` 3 秒）对比 a → b：删除 `<del>` 标红加删除线，新增 `<ins>` 标绿；顶部显示 `+N 字 / -M 字`（非空白码点数）。超时返回 null 时显示警告 `两份文本差异过大，无法逐字对比`。 |
| `chapter/VersionDrawer` | `{ pid; chapterId; open; onClose; currentContent; currentDirty; restoreDisabled; onRestore(v) => Promise<boolean> }`。`Drawer destroyOnHidden`，每次打开重新拉取。Tab「版本」：表格（星标按钮、`v{seq}` + 最新标签、`VERSION_SOURCE` 来源标签、`updated_at`、字数、可编辑备注 ≤200、查看 / 对比当前 / 恢复 / 删除）；勾选最多 2 个（只保留最近勾选的两个）后「对比所选」按 seq 从旧到新对比；最新版本的删除按钮禁用（后端也会 400）。版本正文按 `id + updated_at` 缓存。恢复走 `modal.confirm`，确定后调用编辑器的 `onRestore`。Tab「生成记录」：`GenerationLogList`。 |
| `chapter/GenerationLogList` | `{ pid; chapterId }`。列表（时间、类型 `GENERATION_KIND_LABELS`、模型、状态 Tag（有 error 时 tooltip）、字数、耗时）；「详情」弹窗只读展示参数 JSON 与全部提示词消息；token 恒为 `-`。 |
| `chapter/RewritePanel` | `{ pid; chapterId; content; selection（UTF-16）; aiConfigId; disabled; ensureSaved; onLockChange(locked); onApplied(chapter) }`。有选区可选改写/扩写/缩写/去 AI 味/续写，无选区只能续写（光标处）；选区 >5000 码点、全空白、光标前无正文时禁用开始。开始前 `ensureSaved()`，偏移经 `utf16ToCodePoint` 换算。自带 `useStreamTask`；`locked = 流式中 \|\| 有待采纳结果`，通过 `onLockChange` 通知编辑器设为只读。完成后 `TextDiff(original, text)` + 采纳（把 `done` 的 `start/end/original/before` 原样传回，不再换算）/ 重新生成（沿用上次请求的码点偏移）/ 放弃。采纳 409 → 提示并作废预览。 |
| `chapter/CastSelect` | `{ pid; chapterId; disabled? }`。编辑器正文标题下的「出场角色」多选（`Select mode=multiple`）。`useLoader` 读 `GET /chapters/{cid}/cast`，**变更即** `PUT /cast` 整体替换（乐观更新，失败回滚并提示），与正文保存分开、不参与 `dirty`；tooltip 说明主角始终注入、梗概与上一章结尾自动匹配。AI 任务进行中禁用。 |
| `chapter/AnalysisDrawer` | `{ open; onClose; chapterId; analysis: ChapterAnalysis; autoAnalyze; analyzeDisabled; onAnalyze(); reviseBlockedReason; reviseRunning; onRevise(body) => Promise<boolean>; compare: ReviseCompare \| null }`。`Drawer`（宽 600，`destroyOnHidden`），标题含分析状态 Tag 与「N 项待确认」徽标。顶部：`分析本章` / `重新分析` 按钮（运行中改为 `Spin` + `queueLabel` / stage + `取消分析`）、最近分析时间、`stale` 警告、失败 `Alert`；`CompareCard`：重写进行中提示 → 成功后等待晚于 `sinceAnalysisId` 的新分析（项目关闭自动分析时提示手动重新分析）→ 显示「重写前 → 后」四项评分与 `formatDelta` 差值。Tabs：「分析结果」（默认最新一次；从历史切换到旧分析时显示「正在查看历史分析」+ `回到最新`）= 四项评分（`Statistic`，后缀为与历史中上一次的差值 ↑↓，附 `SCORE_RUBRIC`）、客观指标（字数 / 段落 / 平均段长 / 对白占比）、摘要、情节点（重要度）、钩子（类型 + 强度）、冲突与情感走向、改进建议 `Checkbox.Group`（优先级 + 类别）+ 重写表单（补充要求 ≤2000、保留要素 `Select mode=tags` 预置 `REVISE_PRESERVE_PRESETS` 最多 10、目标字数 300–20000 留空 = 原文字数）→ `buildReviseRequest` 校验 → `onRevise`；`reviseBlockedReason` 非空时禁用并显示原因；「本章识别的伏笔」卡片（`readCandidates` 规范化 + `candidateLabel`：回收 #N（部分）/ 埋下 #N / 新埋设，附关键词、文本、预计回收章；`10-26-foreshadows` 起取代原始 JSON）；解析提示放在 `Collapse`。「回写清单」（Tab 标题 `Badge` = 待确认数）= `AnalysisChangeList`。「历史（N）」= `AnalysisHistory`。历史列表 `GET /chapters/{cid}/analyses` 随 `analysis.version` 重新加载。 |
| `chapter/AnalysisChangeList` | `{ changes; onChanged(updated) }`。`groupChanges` 按类型固定顺序分组；每条：`describeChange` 的对象 + 字段 + `CHANGE_STATUS` Tag，第二行「前 → 后」，第三行 `reason`（失败标红）。`applied` → `撤销`（Popconfirm）；`pending` → `确认` / `拒绝`（`foreshadow_new` 的按钮为 `确认…`，打开 `foreshadows/ForeshadowConfirmModal`，可修改字段后以 overrides 提交；已确认新伏笔的撤销提示「将删除由这条确认创建的伏笔」）；操作返回的条目经 `onChanged` 替换（抽屉同时更新 hook 的最新分析与本地查看的历史分析），409 文案原样 `message.error`。 |
| `chapter/AnalysisHistory` | `{ items（新在前）; loading; selectedId; onSelect(id) }`。≥2 次时显示纯 SVG 评分走势（`trendSegments`：时间正序、纵轴 1–10、缺失值断开、单点画圆；整体粗线 + 三项细线 + 图例，**不引入图表库**）；表格列：时间（最新 / 旧正文标签）、四项评分、`查看`（当前查看中的显示文字）。 |
| `chapter/ChapterAIForms` | 具名导出 `GenerateForm`（目标字数、「字数不足时自动续写」开关——切换即写 `utils/preferences.ts` 的 localStorage、补充指令）与 `PolishForm`。 |



## 原文 .trellis/spec/frontend/component-guidelines.md:113–127

### 页面模式

- **面板 CRUD**（`WorldPanel`、`CharactersPanel`、`OutlinesPanel`、`ChaptersPanel`）：`const pid = useProjectId()`；`const loader = useCallback(() => worldApi.list(pid), [pid])`；`const { data: items = [], loading, reload } = useLoader(loader)`；在 `Modal` 中使用 AntD `Form.useForm` 进行创建/编辑；用 `Popconfirm` 删除；每次变更成功后调用 `reload()`。
- **项目级数值**：在会改变字数/章节数的变更之后，调用 `useProjectStore((s) => s.refresh)`（`ChaptersPanel`、`OutlinesPanel`、章节编辑器）。`OverviewPanel` 通过 `setProject(updated)` 回写 PATCH 结果。
- **破坏性确认**使用 `modal.confirm`（`ProjectListPage` 中的项目删除、编辑器中覆盖已有正文的重新生成）或 `Popconfirm`（行删除）。
- **路由参数**：`:pid` 使用 `useProjectId()`；用 `Number.isFinite` 拦截无效 id，并渲染 `<Result status="404" …/>`。

### 用户管理页（`src/pages/AdminUsersPage.tsx`）

- AntD `Table`，服务端分页（`adminApi.list({ q, page, page_size })`，`q` 会 trim，为空时省略）并带用户名搜索框。列：用户名（自己那一行标记 `我`）、角色、状态、项目数、`formatTime(created_at)`、操作。
- `新建用户` 弹窗：用户名、两次输入初始密码、`设为管理员` 复选框。重置密码弹窗：两次输入新密码。
- 启用/禁用与授予/撤销管理员都经过 `modal.confirm`。删除会打开一个显示该用户项目数的弹窗；仅当 `canConfirmDelete(input, username)` 时确定按钮才可用（严格相等，不做 trim/大小写折叠）。删除某页的最后一行时回退一页。
- `rowActions(row, currentUserId)` 会禁用当前用户自己那一行的**所有**操作（tooltip 为 `不能对自己执行该操作`）——包括重置密码，因为递增 `token_version` 会让管理员自己被登出；自己改密码应通过 `修改密码`。
- 测试仅在 node 环境运行（无 jsdom）：`utils/adminUsers.test.ts`（守卫 + 行操作表 + 删除匹配）、`api/admin.test.ts`（URL/method/body/`credentials: 'include'`、400/403/409 的 `detail` 透传）、`utils/validation.test.ts`。



## 原文 .trellis/spec/frontend/component-guidelines.md:181–217

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



## 原文 .trellis/spec/frontend/component-guidelines.md:342–370

### 主题与配色（`10-27-reader-ui`）

- **主题 store** `theme/themeStore.ts` → `useThemeStore`：`mode: 'light' | 'dark' | 'system'`（localStorage `ainovel.theme`，缺失 / 非法 = `system`）、`systemDark`（`matchMedia('(prefers-color-scheme: dark)')`）；
  `watchSystemTheme()` 由 `App` 挂载时调用并监听 `change`，跟随系统时实时切换；`useResolvedTheme()` = `resolveTheme(mode, systemDark)`。设备级偏好，**不随登录用户重置**。
- `theme/palette.ts::studioPalette(resolved)` 是知页颜色 token 的唯一字面量来源（浅/暗两套）。`App.tsx` 由此同步 AntD 的主色、语义色、背景、文字、边框与链接；按钮高 36、控件圆角 7、面板圆角 10，主按钮无静态阴影。图表与阅读器原有独立调色板保留。
- `ThemeSync` 将所有颜色逐项映射为 `--color-<name>`，并同步 `<html data-theme>`、`color-scheme` 和兼容变量 `--app-bg` / `--app-text`。CSS 与 AntD 共同读取 palette，不从 AntD 派生另一套纸面颜色；本项目不用 Tailwind。
- `index.css` 本地导入 `@fontsource-variable/fraunces` / `@fontsource-variable/geist`；`--font-display` 为 Fraunces/中文宋体，`--font-sans` 为 Geist/苹方，`--radius-card=10px` / `--radius-control=7px`。正文 14px，桌面标题 30px。
- `ThemeSwitch compact` 在侧栏账号旁显示当前模式图标，点击打开浅色/暗色/跟随系统菜单；非 compact 继续使用三段 `Segmented`。保留 `ainovel.theme` 设备偏好、系统监听及单独导出的主题纯函数，不随账号重置。
- **取色规则**：
  - antd 组件与普通 DOM：`const { token } = theme.useToken()`，常用 `colorBgContainer`、`colorFillQuaternary`（浅灰底）、`colorBorderSecondary`（分隔线）、`colorText*`、`colorPrimary` / `colorSuccess` / `colorError` / `colorWarning`、`colorSuccessBg` / `colorErrorBg`（TextDiff 增删底色）、`colorTextLightSolid`（深色顶栏上的白字）。
  - 非 antd 绘制（关系图、伏笔时间线、评分走势）：`theme/usePalette()` → `chartPalette(resolved)`，浅 / 暗两套（`roles` 节点色、`series` 评分线、`grid`、`muted` 等）。`utils/graphFilter.nodeColor(n, palette)` 第二个参数缺省为浅色调色板；`RelationGraph` 另传 `colorMode={resolved}` 给 React Flow。`api/analysis.deltaColor(delta, token)` 由调用方传入 `colorSuccess` / `colorError`。
  - 书封（`utils/shelf.coverColor`）与阅读背景（`READER_TONES`）的具体色值也集中在 `palette.ts`。
- **防回归**：`theme/noHardcodedColors.test.ts` 用 `import.meta.glob(?raw)` 扫描 `src/**/*.{ts,tsx,css}`（不含测试），匹配 `#hex`（3–8 位）、`rgb(a)(`、`hsl(a)(`。白名单只有 `theme/palette.ts`，以及行内带 `// theme-allow` 注释的行（需说明理由）。新增颜色先加到 palette 或改用 token。

### 响应式（`10-27-reader-ui`）

- 文档流里的元素不要用 `100vw` 算宽度：`100vw` 包含竖向滚动条，在有占位滚动条的浏览器（Windows、macOS 设为「始终显示滚动条」）上会横向溢出约 15px；用 `100%` 或让元素独占一行。只有 fixed 定位的 Modal / Drawer 可以用 `100vw`（打开时页面滚动已锁定）。

- 断点 768px：`hooks/useIsMobile()`（`Grid.useBreakpoint()`，`screens.md === false` 即移动端；首帧断点未算出时按桌面），窗口缩放实时更新。**禁止**直接读 `window.innerWidth`。`useModalWidth(desired)`：移动端返回 `calc(100vw - 24px)`。
- 全局兜底（`index.css`）：`body { overflow-x: hidden }`；`.ant-table-content { overflow-x: auto }`（所有表格内容超宽时表内横向滚动）；≤767px 时表头 `white-space: nowrap`（列被挤窄时整表横向滚动而不是逐字换行）、`.ant-drawer-content-wrapper { max-width: 100vw }`、非 confirm 的 `.ant-modal` 宽 `calc(100vw - 24px)`。因此一般弹窗 / 抽屉无需逐个改宽度；新写的需要精确宽度的弹窗可用 `useModalWidth`。
- `AppLayout`：移动端顶栏为汉堡按钮（`aria-label="打开菜单"`）、面包屑和账号；全局/项目导航与主题设置均复用左侧 Drawer。`ProjectLayout` 仅负责项目加载、内容与 AgentDock，不再渲染第二个 Sider 或项目菜单；移动内容内边距 12。外壳高 `100dvh`，56px 顶栏以下滚动；AgentDock 桌面高度为 `calc(100dvh - 56px)`。
- 完整适配的页面：书库（≤539px 单列，540–1099px 两列，1100–1279px 三列，≥1280px 四列）、概览（表单 / 统计单列，`Descriptions column={{ xs: 1, sm: 2 }}`）、章节列表（移动端改为 `List` 卡片：章号 + 标题、状态 / 排队 / 分析标签、字数与时间，「⋮」菜单内编辑 / 阅读 / 删除（`modal.confirm`））、章节编辑器（工具栏只在正文卡片宽度 ≥ `TOOLBAR_IN_HEADER_MIN_WIDTH`（920px，`useElementWidth` 实测，≈ 原 `xxl` 且停靠面板关闭）时放进卡片标题栏，其余宽度放正文上方；移动端 AI 助手卡片收进底部 `Drawer`（85vh，`forceRender`，关闭不丢局部改写状态），由 `FloatButton`（`RobotOutlined`，在任务面板按钮上方，任务运行中带红点）打开）、阅读器、灵感会话（移动端单栏：步骤条横向滚动 `responsive={false}`，「立项草稿与补充想法」按钮打开底部抽屉，抽屉内跳步骤会关闭抽屉）。其余页面只保证无页面级横向溢出。
- **项目页内容区宽度 ≠ 视口宽度**：右侧停靠面板（默认 400px，可调 320–720）打开时内容区会变窄，所以 `ProjectLayout` 下的页面**不要**用 `<Col xs md xl>` / `Grid.useBreakpoint()` 这类按视口断点的分列或判断：
  - 卡片 / 统计网格用 `AutoGrid`。
  - 两栏 / 多栏用 `utils/layout.ts`：`splitRow`（flex 换行、`gap: 16`）+ `splitPane(basis)`（`flex: basis 1 basis px`，`minWidth: 0`，剩余空间按基准宽度等比分配，比例与原 `Col` 一致），各栏基准之和放不下就上下堆叠。现用：组织树 / 详情 240 / 480，记忆与知识库 600 / 300，章节编辑器正文 / AI 助手 640 / 264，概览统计 200 / 200 / 400；概览表单行用 `{ ...splitRow, rowGap: 0 }`，题材 + 状态作为一组（360，内含 172 / 172），窄时书名独占一行、二者并排。
  - 需要按宽度切换内容（而不只是换行）时用 `hooks/useElementWidth()`（ResizeObserver）量容器实际宽度。
  - 窄卡片的标题用 `styles={{ title: { whiteSpace: 'normal' } }}` 允许换行，名字和标签不被省略号截断。
  - 弹窗 / 抽屉内的表单分栏宽度固定，不受停靠面板影响，可继续用 `Col span`。
- 验收：Playwright 在 375px 下对一级页面断言 `document.documentElement.scrollWidth <= clientWidth`。
