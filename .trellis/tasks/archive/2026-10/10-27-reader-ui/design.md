# 技术设计：阅读器、书架、暗色主题、移动端

## 1. 后端（小改动，迁移 `0016_reading_progress`）

- `projects.reading_progress`：JSON 列，默认 `{}`，结构为 `{chapter_id, ratio: 0–1, updated_at}`。项目只属于一个用户，所以进度直接存在项目上，不另建表。
  - 必须登记到 `project_transfer` 注册表：`json_refs` 中的 `chapter_id` 要重映射，映射不到时清空。反射测试会强制检查这一点。
  - 删除章节后，如果进度指向的就是这一章，读取时退回该章前面最近的已写章节（读时兜底，不在删除时联动修改）。
- `PUT /projects/{pid}/reading-progress`，请求体 `{chapter_id, ratio}`：
  - 校验章节属于该项目、`ratio` 在 [0, 1] 内；
  - 前端节流写入：滚动停下 2 秒后，或离开页面时写一次；
  - 返回 204。
- `ProjectOut` 新增字段（列表和详情都返回）：
  - `last_edited_chapter_id`：本项目中正文非空、`updated_at` 最大的章节；
  - `reading`：`{chapter_id, chapter_no, ratio} | null`。章节已删除时，按上面的兜底规则处理。
  - 这两个字段在 `_stats_subquery` 之外再做一次批量查询得到，不产生 N+1 查询。
- 阅读器接口：
  - `GET /projects/{pid}/reader/toc`：返回已写章节的 `[{id, chapter_no, title, word_count, volume_no, volume_title}]`，卷信息通过 `outline_id` 取得。
  - 章节正文复用现有的 `GET` 章节接口。
  - 侧边栏复用现有的分析接口，取最新一条。

## 2. 主题（`src/theme/`）

- `themeStore`（zustand）：`mode: 'light' | 'dark' | 'system'`，持久化在 localStorage 的 `ainovel.theme` 键；`resolved` 由 `matchMedia('(prefers-color-scheme: dark)')` 推导，并监听其 `change` 事件。
- `App.tsx` 的 `ConfigProvider` 根据 `resolved` 选择 `theme.darkAlgorithm` 或 `defaultAlgorithm`，原有的 `colorPrimary` 保留。同时在 `<html>` 上设置 `data-theme`，并设置 `color-scheme`，让原生滚动条和表单控件跟着变色。
- 顶栏放一个三段切换（太阳 / 月亮 / 显示器图标）。在移动端抽屉里，它是一行 Segmented。
- **写死颜色的迁移**：
  - 组件内统一用 `theme.useToken()` 取 `colorBgContainer`、`colorBorderSecondary`、`colorText*`、`colorSuccessBg` 等 token。
  - 不属于 antd 组件的绘制（关系图、伏笔时间线、差异对比）新增 `src/theme/palette.ts`：`chartPalette(resolved)` 返回分类色、连线色和高亮色，分浅、暗两套；差异对比的增删底色用 `colorSuccessBg` 和 `colorErrorBg`。`graphFilter.ts` 中原本写死的分类颜色也改为从调色板取值。
  - `index.css` 中写死的颜色改为 CSS 变量，在 `[data-theme=dark]` 下覆盖。
- **防回归测试** `src/theme/noHardcodedColors.test.ts`：扫描 `src/**/*.tsx`，以及 `src/utils/` 下与配色相关的 ts 文件，匹配 `#[0-9a-f]{3,8}`、`rgb(`、`rgba(`。白名单只有 `palette.ts`、`App.tsx`（主色），以及显式加了 `// theme-allow` 注释的行。

## 3. 响应式（`src/hooks/useIsMobile.ts`）

- `useIsMobile()`：基于 `Grid.useBreakpoint()`，`!screens.md` 即为移动端（< 768px）。antd 内部已经监听窗口大小变化，所以布局会实时切换。
- **AppLayout**：小屏下顶栏只保留 logo、汉堡按钮和用户头像，菜单项和主题切换放进右侧抽屉。
- **ProjectLayout**：小屏下不渲染 `Sider`，改为顶部一个"☰ 项目菜单"按钮，点开左侧抽屉；选中菜单项后自动关闭抽屉。内容区内边距从 24 改为 12。
- **全局规则**：
  - `Table` 默认带 `scroll={{ x: 'max-content' }}`。不逐个修改，而是在 `ConfigProvider` 的 table 组件配置或一个统一封装中处理，实现时选其一。
  - 小屏下 `Modal` 的宽度改为 `calc(100vw - 24px)`，通过封装好的 `useModalWidth(desired)` 取值。
  - `index.css` 中设置 `body { overflow-x: hidden }`，作为页面级横向溢出的兜底。真正的溢出仍要修好，并用 Playwright 在 375px 宽度下逐页检查 `scrollWidth <= clientWidth`。
- **重点页面**：
  - 书架：每行两列，工具栏换行显示。
  - 概览：表单改为单列，统计卡片竖排。
  - 章节列表：小屏下用 `List` 渲染卡片，显示章号、标题、状态、字数、分析状态，操作收进菜单。批量生成的横幅保持可用。
  - 编辑器：标题和工具栏换行；正文区占满宽度；AI 助手面板（生成、润色、局部改写、分析、版本等标签页）改为底部抽屉或全屏抽屉，由一个浮动按钮打开。
  - 阅读器：天然适配，侧边栏在小屏下改为底部抽屉。
  - 灵感会话：小屏下三栏变为单栏，步骤条改为横向可滚动，立项草稿收进抽屉。

## 4. 书架（`pages/ProjectListPage.tsx` 重写为 Bookshelf）

- `BookCover`：2:3 比例。
  - 背景色由 `hash(title)` 映射到 12 种精选色相，浅、暗主题下的明度不同，并保证文字对比度达到 4.5:1；
  - 左侧一条书脊（加深色）；
  - 书名竖排或横排，超过 8 个字时换行，最多 3 行；
  - 题材显示在底部；
  - 预留 `coverUrl` 参数，有封面图时直接显示图片。
- 卡片下方依次显示：书名、状态标签、前 3 个类型标签、字数相对目标的进度条（没有目标时只显示字数）、"N 章"、相对更新时间。
- 按钮：
  - 「继续写作」：有 `last_edited_chapter_id` 时跳到 `/projects/:id/chapters/:cid`，否则跳到概览；
  - 「阅读」：有 `reading` 时显示"读到第 N 章"，否则显示"开始阅读"。没有已写章节时按钮禁用。
  - 「…」菜单：概览、导出（复用 ExportModal）、备份（复用 BackupModal）、删除（二次确认）。
- 第一张固定为「新建」卡，内含三个按钮：新建项目、灵感模式、导入备份。
- 工具栏：
  - 搜索：书名和标签，大小写不敏感，在前端过滤；
  - 排序：最近更新、创建时间、书名（`localeCompare zh`）、字数；
  - 状态筛选：全部、构思中、写作中、已完结。
  - 排序和筛选的选择存在 localStorage 的 `ainovel.shelf` 键。
- 顶部统计：作品数、创作中数（状态为写作中）、总字数。
- 纯函数放在 `utils/shelf.ts`：`filterAndSort`、`coverColor(title, resolved)`、`shelfStats`、`readingLabel`、`continueTarget`。

## 5. 阅读器（`pages/ReaderPage.tsx`，路由 `/projects/:pid/read/:cid`，挂在 ProjectLayout 之外，全屏显示）

- **顶部条**：返回（回到来源页，没有来源就回书架）、书名、章节标题、目录按钮、侧边栏按钮、设置按钮、"编辑本章"按钮。
- **正文**：按段落渲染为 `<p>`，首行缩进 2em。版心最大宽度可设为 560、680 或 800px。
- **设置**（存在 localStorage 的 `ainovel.reader` 键，全局生效）：
  - 字号：14–24，步长 1
  - 行距：1.5、1.8、2.0、2.2
  - 宽度
  - 背景：浅色 `#fff`、护眼 `#f5ecd7`、暗色 `#1b1b1d`，或跟随全局主题。这些具体颜色集中写在 `palette.ts` 中。
  - 字体：宋体栈或黑体栈
- **切章**：章末放"上一章 / 下一章"按钮；←、→ 键切章（焦点在输入框时不响应）；目录以抽屉形式展示，按卷分组，当前章高亮。
- **进度**：
  - 进入章节时，如果这一章就是上次读到的章节，滚动到 `ratio` 对应的位置；
  - 滚动时节流计算 `ratio = scrollTop / (scrollHeight - clientHeight)`；
  - 滚动停止 2 秒后上报；在 `visibilitychange` 变为隐藏，以及组件卸载时也上报一次（使用 fetch 的 `keepalive`）。
- **入口**：书架的「阅读」按钮会读取 `reading`，跳转到对应章节；章节列表每行加一个"阅读"按钮；编辑器顶部加"阅读模式"按钮。
- **侧边栏**：取本章最新一次分析，显示摘要、情节点、钩子和伏笔；没有分析时提示"本章还没有分析"，并提供"去分析"链接，跳到编辑器的分析标签页。

## 6. 取舍

- **进度存在项目的 JSON 列上，不另建表**：一个项目只有一个读者，实现最简单。代价是多人共享项目时会互相覆盖进度，但目前没有共享项目的功能。
- **用 antd 自带的暗色算法**：组件开箱即适配，只需要处理写死的颜色。
- **白名单测试防止颜色回归**：成本低，以后新增组件时也会被提醒使用主题 token。
- **只完整适配高频页面**：管理类页面靠横向滚动兜底，工作量可控。

## 7. 兼容与回滚

- 迁移只新增一个带默认值的列，downgrade 时删除。
- 主题默认值为"跟随系统"，系统是浅色时，界面与现在的视觉效果一致。
