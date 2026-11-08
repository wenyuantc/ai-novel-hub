<!-- 自动摘录自 .trellis/spec/frontend/component-guidelines.md；逐行保留原文，使用时按行号回查完整规范。 -->

<!-- .trellis/spec/frontend/component-guidelines.md:365-405 -->
### 项目备份与导入 / EPUB 导出（`10-23-project-io-reader`）

后端契约见 [../backend/project-transfer.md](../backend/project-transfer.md)。入口：项目概览「导出 TXT / Markdown / EPUB」（`ExportModal`）与「备份项目」（`BackupModal`）；项目列表页「导入备份」（`ImportBackupModal`，在「灵感模式」左侧）。两个弹窗都在打开时才挂载，由页面用 `React.lazy` + `<Suspense fallback={null}>` 按需加载。

| 组件 | 契约 |
|-----------|------------------------|
| `transfer/BackupModal` | `{ projectId; onClose }`。四个可选项 `BACKUP_OPTION_ITEMS`（章节版本默认勾选；大纲快照、生成记录、关联的灵感会话默认不勾，`DEFAULT_BACKUP_OPTIONS`），每项附 `backup/estimate` 的行数与估算大小；总估算 `estimateTotal`（核心始终计入）+ `backupSizeHint`（> 30MB warning「备份较大」、> 100MB error「超过导入上限，导出的文件将无法再导入」）。「下载备份」→ `transferApi.download`（blob 下载，文件名取 `Content-Disposition` RFC 5987）。 |
| `transfer/ImportBackupModal` | `{ onClose; onImported(project) }`。`Upload.Dragger`（`beforeUpload` 返回 false）→ `precheckBackupFile`（`.json` / 非空 / ≤100MB）→ `transferApi.preview`（multipart，可中止）。预览：文件、原书名、备份版本（+ `app_schema`）、导出时间、可选数据（`includedOptionsText`）、可编辑书名（预填 `final_title`，留空 = 原书名，后端重名加后缀）、统计表格（`countRows`：中文标签、固定顺序、未知表排后、0 行不显示）、风格处理 Tag（`styleActionLabel`）、warnings。预览 400 → 错误 `Alert`，`importFileErrors(err)` 列出响应体 `errors`（前 20 条）。「导入为新项目」→ `importBackup({token, title})`；本地按 `expires_in` 计时（`tokenExpiredLocally`，留 5 秒余量）或后端 400 `导入凭据已过期`（`isTokenExpired`）→ 回到选择文件并提示重新选择；导入失败的其他 400 显示 detail + errors（令牌仍可用，可直接重试）。成功后 `message.success`（排队了重建索引时注明），有 warnings 时 `modal.warning` 列出，`onImported` 由列表页跳转到 `/projects/{id}`。 |

- 纯函数在 `utils/transfer.ts`（表标签 `TRANSFER_TABLE_LABELS` 与后端 `importer.TABLE_LABELS` 一致、`countRows`、`formatBytes`、估算与提示、可选项、上传前检查、`importFileErrors` / `isTokenExpired` / `tokenExpiredLocally`、风格处理标签），测试 `utils/transfer.test.ts`；`exportQuery` / `decodeExportWarnings` / `parseContentDispositionFilename` / `backupQuery` 在 `api/projects.test.ts`。
- 包体积（本任务后）：入口 ≈ 140 kB（只多了 EPUB 选项与 `downloadAttachment` / `decodeExportWarnings`）；`BackupModal-*.js` ≈ 2 kB、`ImportBackupModal-*.js` ≈ 5 kB、共享的 `transfer-*.js`（`api/transfer.ts` + `utils/transfer.ts`）≈ 3.5 kB 按需加载。**规则**：入口 chunk 中的模块（`OverviewPanel`、`ProjectListPage`）不得静态 import `api/transfer.ts`、`utils/transfer.ts` 或 `components/transfer/*`。

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

<!-- .trellis/spec/frontend/component-guidelines.md:464-529 -->
### 阅读器（`pages/ReaderPage.tsx`，`10-27-reader-ui`）

- 路由 `/projects/:pid/read` 与 `/projects/:pid/read/:cid`：**不在 AppLayout / ProjectLayout 内**（全屏），外层 `RequireAuth` + `<Outlet/>`，两条子路由都 `lazyPage` 懒加载同一模块（`ReaderPage-*.js` ≈ 16 kB）。不带 `:cid` 时 `startChapterId(toc, reading)` 定位（进度章在目录中 → 该章；否则第一个已写章节；都没有 → 提示「还没有写好正文的章节」）。
- 数据：`projectApi.get`（`reading`）+ `readerApi.toc`（只含已写章节，`volume_no` / `volume_title` 可为 null）一起加载；正文用 `chapterApi.get`；`ReaderView` 以章节 id 为 key 重建。
- 设置 `utils/reader.ts`：localStorage `ainovel.reader`（全局，`normalizeReaderSettings` 校验：字号 14–24 取整、行距 1.5/1.8/2.0/2.2、版心 560/680/800、背景 跟随全局 / 浅色 / 护眼 / 暗色、字体 宋体 / 黑体栈）。背景经 `readerTone(background, resolved)` 取 `READER_TONES`；暗色背景时整个阅读器包一层 `ConfigProvider darkAlgorithm`，抽屉等组件同步变暗。
- 正文 `splitParagraphs`（按行切段、去空行），每段 `<p>` 首行缩进 2em；章末「上一章 / 目录 / 下一章」，`tocNeighbors(toc, chapter_no)` 按章节号找相邻**已写**章节（跳过空章，当前章不在目录中也能定位）。←/→ 切章（`arrowDirection`：焦点在输入控件 / 可编辑区、按修饰键、任一抽屉打开时不响应）。切章用 `navigate(..., { replace: true, state })`，不堆积历史；返回按钮回 `location.state.from`（入口传入），没有则回书架。
- 进度：用窗口滚动。进入章节后 `restoreRatio`（只有该章就是上次读到的章节才恢复，否则回顶部）→ `scrollTopForRatio` 滚动，并登记一次进度；滚动时 rAF 节流计算 `scrollRatio`，停 2 秒后 `readerApi.saveProgress`（PUT 204，失败静默）；`visibilitychange → hidden`、`pagehide`、卸载（含切章）时若有未上报的进度用 `saveProgressKeepalive`（`fetch keepalive`，不走 api 客户端、不触发 401 处理）。空章不记进度。本页上报后更新内存中的 `reading`，切回读过的章节据此恢复。
- 顶栏（sticky）：返回、书名 / 章节标题、目录（左抽屉，`groupToc` 按卷分组、当前章高亮并滚动到可见处）、本章分析（右抽屉、无遮罩：`components/reader/ReaderAnalysisPanel`，取 `analysisApi.list` 第一条再 `get`，显示摘要 / 情节点 / 钩子 / 伏笔候选（`readCandidates` + `candidateLabel`），过期标签；没有分析时「本章还没有分析」+「去分析」→ 编辑器 `?panel=analysis`）、阅读设置（右抽屉）、编辑本章。移动端三个抽屉都改为底部抽屉（72vh，设置为自适应高度）。
- 入口：书架「阅读」、章节列表每行「阅读」（正文为空禁用）、编辑器工具栏「阅读模式」（有未保存修改时由离开守卫拦截），都带 `state.from`。
- 包体积（本任务后）：入口 `index-*.js` ≈ 154 kB（由 140 kB 增加：书架重写 + `utils/shelf.ts`、主题 store / 切换、移动端抽屉与章节卡片列表；阅读器、分析侧栏、`api/foreshadows.ts` 不在入口）。

### 项目智能体面板（`components/agent/`，`10-23-project-agent`）

后端契约见 `backend/agent.md`。

- **挂载与懒加载**：`ProjectLayout` 静态引入轻量的 `AgentDock`，它用 `React.lazy(() => import('./AgentPanel'))` 加载面板（`Suspense` 的 fallback 是 `Skeleton`）。**面板关闭时不挂载 `AgentPanel`**，会话、时间线、markdown 和 diff 都不进入口 chunk。`AgentPanel` 以 `key={projectId}` 挂载，切换项目时重建。
- **打开、收起与宽度**：内容区面包屑一行右侧的「项目智能体」按钮负责开关（`aria-expanded`）。偏好存在 localStorage `ainovel.agentPanel` `{open, width}`，经 `utils/agentPreferences.ts` 的 `readAgentPreferences` / `writeAgentPreferences` 读写，损坏或禁止存储时退回 `{open: false, width: 400}`。宽度一律经 `clampAgentWidth` 限制在 `AGENT_MIN_WIDTH` 320 到 `AGENT_MAX_WIDTH` 720 之间。查询串 `?agent=open`（任务面板里 `agent.turn` 的链接 `taskLink` → `/projects/:pid/overview?agent=open`）也会打开面板；用户在这次导航（`location.key`）里关掉后，就不再因这个参数重新打开。
- **桌面**：右侧 `<aside aria-label="项目智能体">`，`position: sticky; top: 64`，高 `calc(100dvh - 64px)`，`maxWidth: calc(100% - 240px)`。左边缘是 `role="separator"` 拖拽条：指针拖动时 `setPointerCapture`；可聚焦，←/→ 每次 ±20，Home/End 跳到 320/720；带 `aria-valuemin/max/now`。
- **手机**：`AgentDock` 改为全宽 `Drawer`（`width="100%"`、`destroyOnHidden`，body 无内边距），由同一个按钮打开，没有拖拽条。
- **面板结构**（`AgentPanel` → `AgentConversationView` → `AgentTimeline` / `AgentChangeCard`）：
  - 顶部 `Space.Compact`：会话 `Select`、新建、重命名（`Modal` + `Form`，1–200 字）、删除（`modal.confirm`，说明已写入项目的内容会保留）。没有会话时显示空状态，带「开始新会话」按钮。
  - 「自动执行小修改」`Switch` 通过 PATCH `auto_execute` 存在服务端，本轮未结束时禁用。下方文案说明规则：每轮首次不超过 3 项小修改直接执行，删除、后续修改、启动任务和外部插件调用仍需确认。
  - 会话插件多选 `AgentPluginSelect`（`10-23-skills-mcp`）：只在 `useAuthStore` 的 `mcpEnabled` 为真时渲染；`useLoader(mcpApi.list)` 读插件列表，读取失败（如 403）或没有插件且会话也没启用时不显示；
    已停用的插件标「（已停用）」，未被选中的不可再选；变更即 PATCH `mcp_plugin_ids`，本轮未结束或 `busy` 时禁用。下方提示「插件工具的调用默认需要确认，且无法撤销」。
  - 输入区：`AIConfigSelect`；`SkillSelect`（本轮强制启用的 Skill，可清空，发送成功后清空，优先于触发词）；`TextArea` 包在 `AutoComplete` 里做 `/` 补全（被 AutoComplete 包裹的 TextArea 不能传 `maxLength`，否则每次渲染都会出现 BaseSelect 受控警告；长度上限在 `onChange` 里截断）
    （`utils/skills.ts::triggerOptions`：只在输入框内容是 `/前缀` 且还没有空白时出候选，前缀匹配、大小写不敏感，同一触发词只保留优先级最高的「我的」）；
    命中（`matchTrigger`，与后端同规则：开头 `/词` 后接空白或结束）或已选 Skill 时在下方提示「本轮将启用 Skill「X」」。最多 8000 字（按码点计）。
    Enter 发送，Shift+Enter 换行，输入法组合中（`isComposing`）不发送；**补全列表展开时 Enter 用于选择候选，不发送**。`busy`、有未结束轮次或流在运行时禁止发送。输入框上方显示当前附带的页面上下文（章节、大纲、角色，以及选区字数）。
  - 轮次控制：有未结束轮次时显示阶段文案（`paused` 时为「请确认或拒绝修改，随后继续对话」），以及「停止」（Popconfirm → `taskApi.cancel`）。最近一轮可重试（`canRetryTask`）且没有未结束轮次时，显示「重试本轮」。
- **事件归并时间线**（`utils/agent.ts` 的 `buildAgentTimeline(messages, changes, liveSteps)`）：
  - 按持久化消息顺序展开：user 消息、assistant 文本，再接 assistant 的每个 `tool_call`。
  - 工具步骤以 `${turn_task_id}:${call_id}` 去重，不同轮次重用同一个 call id 时互不覆盖。持久化的 tool 结果和实时 `tool_start` / `tool_end`（`mergeToolStep`）合并到同一位置。对应的提案卡片紧跟在该工具步骤之后，且只出现一次。
  - 没有 `tool_call_id` 的 tool 消息是撤销备注，显示为「操作记录」。没挂到任何工具上的提案追加在末尾。
  - `proposal` / `executed` 事件经 `upsertAgentChange` 按 id 替换卡片，按 `seq, id` 排序，重连后不会追加重复卡片。
  - 实时文本等于 `stream.text` 去掉本轮已持久化的 assistant 文本前缀（runner 每次运行先 `reset` 再重放已落库文本）。
  - 自动滚动只在用户停留在底部（距底 < 80px）时跟随。
- **助手文本渲染**：`AgentMarkdown` 是自写的极简解析器，支持标题、列表、粗体、行内代码、代码块和链接。它只生成 React 节点：原始 HTML 一律当作文本，链接只允许 `http(s)`，并加 `target="_blank" rel="noopener noreferrer"`。**不得**使用 `dangerouslySetInnerHTML`，也不得引入完整的 markdown 库。
- **提案卡片按钮的显示条件**（`AgentChangeCard`，状态标签取 `AGENT_CHANGE_STATUS`）：
  - `proposed`：显示「确认」「拒绝」。只有本轮任务处于 `paused`（`confirmationReady`）时才可点；运行中的轮次里新出现的提案先只读显示。
  - 「撤销」只在 `canUndoChange` 为真时显示，即 `status === 'executed' && kind !== 'task' && kind !== 'external'`；点击前先 Popconfirm，说明之后被修改过会提示冲突。
  - `kind === 'external'`（MCP 调用，`10-23-skills-mcp`）：`externalCall(change)` 从 `preview.external` 收窄出插件、工具、参数（结构不完整时回落到 `change.arguments`）；
    卡片显示橙色 Tag「外部操作，无法撤销」、插件声明只读时另显示「插件声明只读」（tooltip 说明只是插件自己的提示）、插件名与标识、工具名（`code`）、
    完整参数 JSON（`pre`，`overflowWrap: anywhere`、限高可滚动）。确认按钮文案为「确认调用」。确认请求内同步调用外部服务，最长约 30 秒：
    `useAgentConversation` 记录 `actingId`，只有正在处理的那张卡片 `loading`（并显示「正在调用外部插件，最长约 30 秒…」），其他卡片只禁用。
    执行后经 `externalResult(change)` 显示折叠的「调用结果」或「调用失败」文本，**永远不显示撤销**。卡片自带「外部操作，无法撤销」标签，所以 `preview.warnings` 中含「无法撤销」的那条对 external 不再渲染，避免重复；其余警告（如刷新后的「继续确认会失败」）照常显示。整轮撤销返回 `notice`（跳过的外部操作）时 `message.info`。
  - `kind === 'task'` 且已执行（`changeTaskId` 能从 `result.task_id` 或 `result.task.id` 取到 id）：显示任务状态 Tag 和「查看任务」（`TaskOutputModal`）。任务未结束（`isOpenTask`）时再显示「取消任务」，**不显示撤销**。
  - `executing` / `rejected` / `failed` / `undone`：没有操作按钮。`failed` 显示 `error` Alert。
  - 字段对比是表格（`tableLayout: fixed`、`overflowWrap: anywhere`），值经 `agentValue` 格式化。`kind === 'text'`，或前后合计超过 500 字的字符串字段，额外提供折叠的 `TextDiff` 逐字对比。
  - 轮次级按钮（`AgentTimeline`）：本轮待确认超过 1 项时，显示「全部确认（N）」「全部拒绝」；可撤销超过 1 项时，显示「撤销整轮」（Popconfirm，说明后台任务需要单独取消、任一项冲突时整轮都不撤销；运行中禁用）。
- **冲突（409）**：确认、拒绝、撤销和轮次操作都经 `api/agent.ts` 的 `withAgentRefresh` 执行。遇到 409 先重新 `GET` 会话详情，把服务端最新的预览或状态写回卡片，再把错误交给 `message.error`。**绝不自动重发确认**：`preview_changed` 之后，用户必须对新预览再确认一次。
- **改动后的数据刷新**（`utils/projectRefresh.ts`）：
  - `invalidateProject(pid, resources)` 只广播失效范围，不缓存业务数据。`changedResources(changes)` 由 `effects[].table`（`TABLE_RESOURCES` 映射）和工具名推导受影响的资源，并始终包含 `project`。
  - 触发时机有三个：收到 `executed` 事件、一轮结束（取本轮已执行的提案），以及每次面板操作之后（取已执行和已撤销的提案）。每次还会刷新 `useProjectStore` 与 `useTasksStore`。
  - 页面订阅有两种方式。一是给 `useLoader(loader, { projectId, resources })` 传第二个参数，命中时自动 `reload()`。二是直接用 `hooks/useProjectRefresh(pid, resources, refresh)`，例如编辑器：有未保存修改、生成中或保存中时把新章节放进 `externalChapter` 提示，否则直接替换为服务端内容。
  - 新增项目子页面时，要为它的 loader 声明 resources；新增可被智能体改动的表时，要补 `TABLE_RESOURCES`。
- **跨设备与重连**：有未结束轮次时每 5 秒，以及页面重新可见（`visibilitychange`）时，都会重新拉取会话详情，因为待确认的轮次可能在另一台设备上被处理。流断开时显示「连接中断，正在恢复对话…」。
- **页面上下文**：`hooks/useAgentPageContext.ts` 由页面调用 `usePublishAgentContext(pid, {chapter_id?, outline_id?, character_id?, selection?})` 发布（当前用于角色抽屉、大纲、章节编辑器）。选区截到 2000 个码点，只保存在内存里，页面卸载即清除。面板通过 `useAgentPageContext(pid, pathname)` 读取，并补上 `route` 和路由里的章节 id，发送时附带。服务端会再校验一遍是否属于当前项目。
- **任务面板**：`agent.turn` 不显示「继续」（待确认时由面板确认后自动续跑），取消确认的文案是「已执行的修改保留，可在会话中撤销」。
- 颜色一律取 `theme.useToken()`（`colorBgContainer` / `colorBorderSecondary` / `colorFillQuaternary`），`noHardcodedColors.test.ts` 覆盖本目录。
- 测试（node 环境）：
  - `utils/agent.test.ts`：历史与实时事件合并，提案只出现一次；跨轮次重用 call id；重连时按 id 更新卡片；撤销条件与任务 id 提取；级联 effects 推导刷新范围；宽度收敛到 320–720；存储损坏。
  - `api/agent.test.ts`：路径与方法；409 先刷新、不重发；流事件顺序交付，非法载荷不进入 UI，经 `api/agentEvents.ts` 收窄。
  - `components/agent/agentUI.test.ts`：用 `react-dom/server` 渲染，覆盖各状态下的按钮、安全的 markdown、移动端全宽抽屉、桌面键盘调宽，以及折叠时不挂载面板；
    external 卡片（插件 / 工具 / 完整参数 /「无法撤销」、确认中只有当前卡片转圈、执行后显示结果且没有撤销、失败显示原因）。
  - `utils/agent.test.ts` 另覆盖 external 不可撤销、`externalCall` / `externalResult` 收窄、MCP 与 Skill 工具名的中文标签（`toolLabel`：`mcp__标识__工具` → 「外部插件 标识 · 工具」）。
