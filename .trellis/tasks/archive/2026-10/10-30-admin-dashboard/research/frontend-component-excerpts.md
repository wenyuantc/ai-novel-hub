# component-guidelines：本任务相关规范原文摘录

来源：`.trellis/spec/frontend/component-guidelines.md`；SHA-256：`870b5b1661c76644bad0718acac0c9414c7967e3e2109fd6f4f10d4bb662a47e`。

按相关原文摘录以避免上下文注入截断；新增完整交付契约见 `.trellis/spec/backend/admin-dashboard.md`。源文件变化后须核对摘录，需要其他章节时按源路径读取。

## 原文 7–18 行

## 提示词工坊

- 用户级懒加载路由 `/workshop`（`pages/WorkshopPage.tsx`），`StudioNavigation` 的创作资源分组与用户菜单有入口。Tab 为「浏览」「我的分享」「审核」，审核和官方发布仅管理员可见；数量来自待审核列表的 `total`。
- `components/workshop/ShareToWorkshopModal` 在写作风格库的本人风格、模板页已保存的覆盖上预填分享数据，也用于条目更新与官方发布；分类、标签复用 `WorkshopMetadataFields`。模板 key 在分享已有模板和更新时锁定。
- `WorkshopItemDrawer` 展示署名、计数、正文与操作；以服务端 `is_mine` 判归属，使用 `author_name ?? '匿名'`，不得推导或显示匿名作者 id。`WorkshopItemControls` 依条目状态及 `hidden_by` 控制上架 / 下架操作，管理员可改分类标签，正文仅作者提交更新。
- 模板导入前复用 `PromptDiffModal`，`utils/workshop.ts::workshopImportPairs` 对照当前生效内容：覆盖启用才取覆盖，否则取系统默认；确认后调用导入。导入结果提示覆盖已启用且可从历史恢复；风格导入后强制刷新 styles store，刷新失败也需显示错误。
- `ReviewPanel` 展示审核快照和真实管理员署名，管理员只能调整分类和标签，通过审核不要求驳回原因；驳回必须填原因。作者可撤回 pending、修改被驳回 / 撤回提交并重交。
- 列表与详情加载沿用 `useLoader` 过期响应保护；异步写操作期间禁用冲突控件，错误以 Alert / `errorMessage` 呈现。详情渲染必须从 `item.status` 明确定义状态，不能依赖隐含全局变量。
- 契约、状态、404/403、导入版本和测试断言见 [工坊规范](../backend/workshop.md)。

> 本项目中组件的构建方式。


## 原文 21–44 行

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


## 原文 117–142 行

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


## 原文 196–232 行

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


## 原文 357–386 行

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
- 完整适配的页面：书库（按内容区以 360px 最小列宽自适应，窄屏保持左书封右信息）、概览（表单 / 统计单列，`Descriptions column={{ xs: 1, sm: 2 }}`）、章节列表（移动端改为 `List` 卡片：章号 + 标题、状态 / 排队 / 分析标签、字数与时间，「⋮」菜单内编辑 / 阅读 / 删除（`modal.confirm`））、章节编辑器（工具栏只在正文卡片宽度 ≥ `TOOLBAR_IN_HEADER_MIN_WIDTH`（920px，`useElementWidth` 实测，≈ 原 `xxl` 且停靠面板关闭）时放进卡片标题栏，其余宽度放正文上方；移动端 AI 助手卡片收进底部 `Drawer`（85vh，`forceRender`，关闭不丢局部改写状态），由 `FloatButton`（`RobotOutlined`，在任务面板按钮上方，任务运行中带红点）打开）、阅读器、灵感会话（移动端单栏：步骤条横向滚动 `responsive={false}`，「立项草稿与补充想法」按钮打开底部抽屉，抽屉内跳步骤会关闭抽屉）。其余页面只保证无页面级横向溢出。
- **项目页内容区宽度 ≠ 视口宽度**：右侧停靠面板（默认 400px，可调 320–720）打开时内容区会变窄，所以 `ProjectLayout` 下的页面**不要**用 `<Col xs md xl>` / `Grid.useBreakpoint()` 这类按视口断点的分列或判断：
  - 卡片 / 统计网格用 `AutoGrid`。
  - 两栏 / 多栏用 `utils/layout.ts`：`splitRow`（flex 换行、`gap: 16`）+ `splitPane(basis)`（`flex: basis 1 basis px`，`minWidth: 0`，剩余空间按基准宽度等比分配，比例与原 `Col` 一致），各栏基准之和放不下就上下堆叠。现用：组织树 / 详情 240 / 480，记忆与知识库 600 / 300，章节编辑器正文 / AI 助手 640 / 264，概览统计 200 / 200 / 400；概览表单行用 `{ ...splitRow, rowGap: 0 }`，题材 + 状态作为一组（360，内含 172 / 172），窄时书名独占一行、二者并排。
  - 需要按宽度切换内容（而不只是换行）时用 `hooks/useElementWidth()`（ResizeObserver）量容器实际宽度。
  - 窄卡片的标题用 `styles={{ title: { whiteSpace: 'normal' } }}` 允许换行，名字和标签不被省略号截断。
  - 弹窗 / 抽屉内的表单分栏宽度固定，不受停靠面板影响，可继续用 `Col span`。
- 验收：Playwright 在 375px 下对一级页面断言 `document.documentElement.scrollWidth <= clientWidth`。


## 原文第 49 行：复用契约

| `AppLayout` | 知页外壳：216px 浅侧栏、56px 顶栏、面包屑与 `<Outlet/>`。`StudioNavigation` 按 `/projects/:pid` 切换全局/项目分组，桌面与移动抽屉共用一份定义，MCP 入口遵循 `mcpEnabled`；项目名称只读 `cachedProject.id === pid` 的缓存。侧栏新建跳 `/projects?create=1`；真实账号菜单保留改密/退出，管理员才显示用户管理。任务计数取 `useTasksStore.active.length`，只在 loaded 后显示，侧栏控制始终挂载的 `<TaskPanel open onOpenChange hideTrigger/>`。移动导航按 `location.key` 关闭，避免在导航 effect 中同步 setState。 |

## 原文第 50 行：复用契约

| `StudioUI` | `StudioBrand({onClick?})` 在官网页头/页脚与工作区共用，始终链接公开 `/`；hover/focus 显式保持主题前景色，避免 AntD 全局链接悬停色降低暗色可读性，focus-visible 保留主色轮廓。`StudioPage({title, hint?, action?, children})` 提供 `<main>`、h1 与页面边距；`StudioPanel({children})` 为无阴影纸面；`StudioEmpty({title, text, action?})` 提供空态与下一步操作；`ProjectBadge({status})` 使用既有 `PROJECT_STATUS` 枚举标签。 |

## 原文第 51 行：复用契约

| `RequireAuth` | 路由守卫：`status === 'unknown'` → `<FullPageSpin/>`；`'anonymous'` → `<Navigate to="/login" replace state={{ from: location.pathname + location.search }} />`。`LoginPage` 读取 `state.from`（默认 `/projects`）。 |

## 原文第 52 行：复用契约

| `RequireAdmin` | 包裹 `/admin/users`（位于 `RequireAuth` 内部）。判定逻辑是 `utils/adminUsers.ts` 中的纯函数 `adminGuard(status, user)`：`'loading'` → `<FullPageSpin/>`，`'login'` → `/login`，`'home'`（非管理员）→ `/`，`'allow'` → children。`is_admin` 只从 `/auth/me` 读取一次；被降级的管理员在刷新前仍会看到菜单，但 API 调用会返回 403 并显示提示信息。 |

## 原文第 60 行：复用契约

| `AutoGrid` | `{ children; minItemWidth? = 260; gap? = 16; mobileColumns? = 1; style? }`。按**容器宽度**自适应列数的 CSS grid：桌面 `repeat(auto-fill, minmax(min(<minItemWidth>px, 100%), 1fr))`，容器比最小宽度还窄时收缩到 100% 不溢出；移动端（`useIsMobile`）固定 `mobileColumns` 列。用于项目页里的卡片 / 统计网格（角色、职业卡片；伏笔统计 `minItemWidth={96} mobileColumns={3}`）。 |
