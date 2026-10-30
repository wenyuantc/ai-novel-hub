# 本任务相关规范摘录：ui-contracts.md

来源：`.trellis/spec/frontend/component-guidelines.md`。规划时逐段原文摘录，避免上下文注入截断；规范更新时以原文件为准。


## 来源 .trellis/spec/frontend/component-guidelines.md:17

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



## 来源 .trellis/spec/frontend/component-guidelines.md:68

### 设定类组件（`10-23-story-entities`）

| 组件 | 契约 |
|-----------|------------------------|
| `entities/characterForm.ts` + `CharacterFormFields` | `CharacterFormValues`、`EMPTY_CHARACTER`、`characterToForm` / `formToCharacterInput`；具名导出 `CharacterBasicFields`（名字、别名 `Select mode=tags`（校验复用 `cleanAliases`）、定位下拉 `roleTypeOptions(当前值)`——旧自由文本值以「xx（原值）」保留、年龄、性别、存活状态、始终注入上下文；`creating` 时定位选「主角」自动打开开关）、`CharacterDescFields`（外貌 / 性格 / 背景 / 简介）、`CharacterStateFields`（当前状态）。抽屉与 AI 草稿预览共用。 |
| `entities/CharacterRelationsTab` | `{ projectId; character; characters }`。`relationshipApi.list(pid, character.id)` 的视角字段渲染「`other_role` `other_name`（我是 `self_role`）」、亲密度（`intimacyColor`）、状态、描述；新增 / 编辑走 `RelationEditModal`（`viewerId` 固定），`历史` → `RelationHistoryModal`。`relations_text` 非空时显示「旧关系备注（只读）」卡片 + `AI 解析为关系`（`EntityTaskModal kind="relations.parse"`）。 |
| `entities/CharacterMembershipsTab` | `{ projectId; character }`。`GET /characters/{cid}/memberships`；加入组织（已加入的组织禁用）、改职位 / 状态 / 备注、移除。 |
| `relations/RelationEditModal` | `{ projectId; open; characters; types; viewerId?; editing?; onClose; onSaved }`。类型下拉用 `typeOptions(types, 我/甲名, 对方名)` 按视角列出称呼并按类别分组，value 为 `${type_id}:a|b`（`parseTypeOptionValue`）；新建时 `a_id = viewerIsA ? self : other`。新建可切到「自定义称呼」（`role_a`=自己、`role_b`=对方，后端匹配 / 反向 / 新建类型）。编辑只发 `type_id` + `a_id`（调换方向；选中对称类型时不发 `a_id`，回填时对称类型固定取 `:a` 选项，避免下拉显示原始值、写入无意义的方向历史）+ 数值字段；`chapter_no` / `note` 只记入历史。亲密度 `Slider` -100～100。 |
| `relations/RelationHistoryModal` | 倒序列出历史：时间、来源（`RELATION_SOURCE_LABELS`）、第几章、备注、`字段：旧 → 新`（`HISTORY_FIELD_LABELS`、`formatChangeValue`）。 |
| `relations/RelationTypeManager` | 预置类型显示「预置」只读；自定义类型新增 / 删除（409 被引用、400 预置由后端返回 detail）。 |
| `relations/RelationGraph` | **只在此文件引用 `@xyflow/react` 与 `@dagrejs/dagre`**，由 `RelationshipsPanel` 用 `React.lazy` 加载（独立 chunk + 独立 CSS）。dagre `rankdir: LR` 布局；节点颜色 `nodeColor`（主角金、反派红、重要配角蓝、配角青），非存活虚线框灰底；边 `label`、颜色 `intimacyColor`、过去式 / 破裂虚线、复杂动画、非对称类型带箭头（a → b）。节点用 `useNodesState` 可拖动，数据 / 筛选变化时重置布局。`onNodeClick(id)` 回调。 |
| `entities/EntityTaskModal` | `{ projectId; kind: 'character.generate' \| 'organization.generate' \| 'relations.parse' \| 'career.generate'; open; onClose; characterId?; characterName?; onSaved? }`。仅 `open` 时挂载。表单（AI 配置、要求、角色定位）→ 创建 202 任务 → `track` + `useTaskStream().follow` → 成功后渲染注册表中的结果组件。挂载时按 `project_id + kind + active` 接上活动任务：生成类取最新一个；解析任务再按 `TaskOut.character_id === characterId` 匹配本角色（同一角色同时只有一个解析任务，`resource_key = relparse:{cid}`）。**运行中允许关闭**（只断开订阅，提示到任务面板查看结果）——与 `WizardModal` 不同，因为结果只是草稿、不需要刷新列表。 |
| `entities/taskResultViewers.ts` | **任务面板扩展点**：`TASK_RESULT_VIEWERS: Partial<Record<TaskKind, ComponentType<TaskResultViewerProps>>>`（`{ projectId; result: unknown; onSaved? }`）与 `hasResultViewer(kind)`。新增「只产出草稿」的任务类型只需在此注册，`TaskPanel` 不改。结果组件一律 `React.lazy(() => import(...))` 注册（不进入口 chunk），渲染方（`TaskResultModal`、`EntityTaskModal`）负责包 `<Suspense fallback={<Spin/>}>`；不要在别处静态 import 这些结果组件，否则拆分失效。 |
| `entities/TaskResultModal` | `{ task; onClose }`。对**已结束**任务调 `follow(task)`：引擎对已结束任务返回 `snapshot` + `done{result}`（`TaskOut` 本身不含 result），据此渲染注册的结果组件；失败 / 丢失显示错误。 |
| `entities/CharacterDraftResult` / `OrganizationDraftResult` / `RelationsParseResultView` | 结果组件，内部用 `parse*Result` 收窄。角色：草稿表单（基础 / 描写 / 状态）+「关系建议」Tab（`RelationSuggestionList`），保存 `POST /characters/with-relations`；组织：草稿表单（上级下拉）+ 成员建议勾选（可改职位），保存 `POST /organizations/with-members`；解析：建议列表，保存 `POST /relationships/batch`（`source=ai_parse`，`a_id = subject_is_a ? 主体 : 目标`）。保存成功后按钮禁用，避免重复写入；不保存则不写任何数据。 |
| `entities/RelationSuggestionList` | 关系建议复选表：`resolved=false` 行删除线 + 红字 `reason`，不可勾选 / 编辑；可改两侧称呼（改了即丢弃 `type_id`，按主体为 role_a 重新提交）、亲密度、描述；`type_id` 为空显示「新类型」。 |
| `entities/CardExportModal` | 仅打开时挂载；角色多选（初始为列表勾选）+ 组织多选 → `cardApi.export` → `downloadJson(data, cardFilename(项目名))`。 |
| `entities/CardImportModal` | 仅打开时挂载；`Upload.Dragger`（`beforeUpload` 返回 false）读取文件：文件 > `MAX_CARD_FILE_BYTES`（8MB，容纳缩进）先粗筛，再由 `parseCardText` 拦截非 JSON 与紧凑序列化后 > 2MB（与后端同一口径），再调预览。角色 / 组织表逐项：新增直接导入；同名冲突 `Radio` 跳过（关联已有）/ 重命名为 `rename_to`，「导入后名称」列由 `importedName` 计算，标题行统计 `summarizeImport`；关系表显示可关联 / 无法关联；warnings 用 `Alert`。确认后 `cardApi.import(data, resolutions)`，展示结果计数与 warnings。 |

### 设定页面

- `CharactersPanel`：工具栏 `AI 生成角色`（`EntityTaskModal`）、`导出卡片（勾选数）`、`导入卡片`、`AI 重新生成全部`（原角色向导，整体替换）、`新增角色`。卡片列表按定位 `Segmented` 筛选（空定位归「未设定」），每张卡带勾选框，「全选当前」只作用于当前筛选。编辑在 `Drawer`（宽 760）中，Tab：基础 / 描写 / 状态（`forceRender`，共用一个 Form）+ 已有角色才有的关系 / 组织；保存后留在抽屉内（新建后即可编辑关系）。删除提示会级联清理关系与成员记录。
- `OrganizationsPanel`（`/projects/:pid/organizations`，懒加载路由）：左 `Tree`（`buildOrgTree`，标题含类型与成员数），右 `OrgDetail`（`key=org.id`，详情 + 成员表 CRUD）。编辑上级时排除自身与下级（`descendantIds`），路径用 `orgPath`；`parent_id` / `power_level` 清空时显式发 `null`。
- `RelationshipsPanel`（`/projects/:pid/relationships`，懒加载路由）：Tab「列表」（按角色 / 类别 / 状态筛选、`describeRelation`、编辑 / 历史 / 删除）与「图谱」（`destroyOnHidden`，每次切入重新拉 `/relationship-graph`；按组织（含下级）/ 类别多选 / 隐藏孤立角色筛选（`utils/graphFilter.ts` 的 `filterGraph`），点击节点弹出角色卡）；工具栏 `关系类型`、`新增关系`。
- `WorldPanel`：`Segmented` 按分类筛选（全部 + 有条目的分类），列表按 `groupWorld` 固定顺序分组；表单新增分类下拉，新建时默认取当前筛选分类。
- `StudioNavigation.PROJECT_GROUPS`：在「角色」后新增「组织」「关系」「职业」。`StoryPreview.CharacterPreview` 展示结构化字段（只显示非空行），`WorldPreview` 带分类标签。



## 来源 .trellis/spec/frontend/component-guidelines.md:180

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



## 来源 .trellis/spec/frontend/component-guidelines.md:341

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
