# 场景任务规范摘录：备份／导入、阅读、主题与响应式

来源：.trellis/spec/frontend/component-guidelines.md

来源 SHA-256：b19616bd315b82c211b8a770c26ddd6ec28c15194ecfa189c5210f4d547a8a72

摘录日期：2026-11-07。用于避免 native context 单文件 32768 字节截断，不替代原规范。开工先核对来源是否变化；超出摘录职责时读取原文。原文相对链接以来源文件所在目录为基准。新场景行为以本任务已审阅的 PRD／design 为准，未改动的原合同仍适用。

## 原文 365–406 行

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



## 原文 464–474 行

### 阅读器（`pages/ReaderPage.tsx`，`10-27-reader-ui`）

- 路由 `/projects/:pid/read` 与 `/projects/:pid/read/:cid`：**不在 AppLayout / ProjectLayout 内**（全屏），外层 `RequireAuth` + `<Outlet/>`，两条子路由都 `lazyPage` 懒加载同一模块（`ReaderPage-*.js` ≈ 16 kB）。不带 `:cid` 时 `startChapterId(toc, reading)` 定位（进度章在目录中 → 该章；否则第一个已写章节；都没有 → 提示「还没有写好正文的章节」）。
- 数据：`projectApi.get`（`reading`）+ `readerApi.toc`（只含已写章节，`volume_no` / `volume_title` 可为 null）一起加载；正文用 `chapterApi.get`；`ReaderView` 以章节 id 为 key 重建。
- 设置 `utils/reader.ts`：localStorage `ainovel.reader`（全局，`normalizeReaderSettings` 校验：字号 14–24 取整、行距 1.5/1.8/2.0/2.2、版心 560/680/800、背景 跟随全局 / 浅色 / 护眼 / 暗色、字体 宋体 / 黑体栈）。背景经 `readerTone(background, resolved)` 取 `READER_TONES`；暗色背景时整个阅读器包一层 `ConfigProvider darkAlgorithm`，抽屉等组件同步变暗。
- 正文 `splitParagraphs`（按行切段、去空行），每段 `<p>` 首行缩进 2em；章末「上一章 / 目录 / 下一章」，`tocNeighbors(toc, chapter_no)` 按章节号找相邻**已写**章节（跳过空章，当前章不在目录中也能定位）。←/→ 切章（`arrowDirection`：焦点在输入控件 / 可编辑区、按修饰键、任一抽屉打开时不响应）。切章用 `navigate(..., { replace: true, state })`，不堆积历史；返回按钮回 `location.state.from`（入口传入），没有则回书架。
- 进度：用窗口滚动。进入章节后 `restoreRatio`（只有该章就是上次读到的章节才恢复，否则回顶部）→ `scrollTopForRatio` 滚动，并登记一次进度；滚动时 rAF 节流计算 `scrollRatio`，停 2 秒后 `readerApi.saveProgress`（PUT 204，失败静默）；`visibilitychange → hidden`、`pagehide`、卸载（含切章）时若有未上报的进度用 `saveProgressKeepalive`（`fetch keepalive`，不走 api 客户端、不触发 401 处理）。空章不记进度。本页上报后更新内存中的 `reading`，切回读过的章节据此恢复。
- 顶栏（sticky）：返回、书名 / 章节标题、目录（左抽屉，`groupToc` 按卷分组、当前章高亮并滚动到可见处）、本章分析（右抽屉、无遮罩：`components/reader/ReaderAnalysisPanel`，取 `analysisApi.list` 第一条再 `get`，显示摘要 / 情节点 / 钩子 / 伏笔候选（`readCandidates` + `candidateLabel`），过期标签；没有分析时「本章还没有分析」+「去分析」→ 编辑器 `?panel=analysis`）、阅读设置（右抽屉）、编辑本章。移动端三个抽屉都改为底部抽屉（72vh，设置为自适应高度）。
- 入口：书架「阅读」、章节列表每行「阅读」（正文为空禁用）、编辑器工具栏「阅读模式」（有未保存修改时由离开守卫拦截），都带 `state.from`。
- 包体积（本任务后）：入口 `index-*.js` ≈ 154 kB（由 140 kB 增加：书架重写 + `utils/shelf.ts`、主题 store / 切换、移动端抽屉与章节卡片列表；阅读器、分析侧栏、`api/foreshadows.ts` 不在入口）。



<!-- excerpt-end -->
