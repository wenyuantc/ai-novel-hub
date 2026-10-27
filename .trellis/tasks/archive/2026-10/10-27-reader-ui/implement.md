# 执行计划：阅读器、书架、暗色主题、移动端

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号。目前最新是 `0015_knowledge_base`，本任务计划用 `0016_reading_progress`。
- 阅读这些 spec：
  - `backend/project-transfer.md`：新增的 JSON 列要登记到注册表
  - `backend/database-guidelines.md`
  - `frontend/component-guidelines.md`
  - `frontend/state-management.md`
  - `frontend/quality-guidelines.md`
  - `frontend/type-safety.md`

## 有序清单

### 后端

1. [ ] 迁移 `0016_reading_progress`：新增 `projects.reading_progress`（JSON，默认 `{}`），补迁移测试。在 project_transfer 注册表里为 `reading_progress.chapter_id` 登记重映射规则，确认反射测试和往返测试都能通过。
2. [ ] 新增 `PUT /projects/{pid}/reading-progress`：校验章节属于该项目，`ratio` 在 [0,1] 内。`ProjectOut` 增加 `last_edited_chapter_id` 和 `reading`，用批量查询取值；`reading` 指向的章节已被删除时，退回前面最近一个已写章节。新增 `GET /projects/{pid}/reader/toc`。
3. [ ] 测试：
   - 进度的写入与读取，以及各种校验；
   - 删除章节后的退回逻辑；
   - `last_edited_chapter_id` 只考虑正文非空的章节；
   - 目录只包含已写章节，并带卷信息；
   - 跨用户访问返回 404；
   - 项目列表不产生 N+1 查询（用 SQL 计数断言）；
   - transfer 往返时进度能正确映射。

### 前端

4. [ ] 主题：
   - `theme/themeStore`、`ConfigProvider` 切换算法，设置 `data-theme` 和 `color-scheme`；
   - 顶栏加三段切换开关；
   - 新增 `palette.ts`。
5. [ ] 迁移写死的颜色：把 60 处改为 token 或 palette，范围包括伏笔时间线、分析历史、TextDiff、graphFilter、关系图、提示词预览、灵感选项、StreamOutput、AppLayout、ProjectLayout、ForeshadowsPanel 等；`index.css` 改用 CSS 变量。新增 `noHardcodedColors.test.ts`。
6. [ ] 响应式：
   - `useIsMobile`、`useModalWidth`；
   - AppLayout 和 ProjectLayout 在小屏下改为抽屉；
   - 所有表格统一支持横向滚动；
   - 用 body 的 overflow 兜底。
7. [ ] 书架：重写 ProjectListPage，包括 BookCover、卡片、新建卡、工具栏（存到 localStorage）、统计，以及「继续写作」和「阅读」入口；复用导出和备份弹窗；纯函数放在 `utils/shelf.ts`。
8. [ ] 阅读器：
   - `ReaderPage` 路由；
   - 目录抽屉、设置面板（存到 localStorage）、方向键切章；
   - 进度节流上报，离开页面时用 keepalive 上报；
   - 分析侧边栏、编辑入口；
   - 在章节列表和编辑器里加入口。
9. [ ] 移动端重点页：
   - 章节列表改为卡片 List；
   - 编辑器的 AI 面板放进抽屉；
   - 概览改为单列；
   - 灵感会话改为单栏，草稿放进抽屉；
   - 阅读器的侧边栏改为底部抽屉。
10. [ ] 纯函数测试：shelf 的过滤、排序、统计、颜色稳定性与对比度，阅读进度 ratio 的计算与恢复，主题解析（system 模式与 matchMedia），阅读设置的默认值与合法范围。
11. [ ] spec：更新 `component-guidelines.md`，新增「主题与配色」「响应式」两节，写清 useIsMobile、token 和 palette 的规则，以及禁止写死颜色；同时更新 `state-management.md`（新增两个 store 与 localStorage 键）和 `type-safety.md`。确认包体积没有超过上限，阅读器页面懒加载。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收（Playwright）：
- 用桌面宽度和 375px 两种视口，分别在浅色和暗色下截图：书架、概览、章节列表、编辑器、阅读器、灵感会话、关系图、伏笔时间线、版本对比。另外对所有一级页面断言 `document.documentElement.scrollWidth <= clientWidth`。
- 阅读器：读到第 2 章的中间位置，刷新页面后回到同一位置；换一个浏览器上下文（同一账号重新登录）后，从书架点「阅读」能回到第 2 章。
- 把系统主题切换为暗色（Playwright `emulateMedia`），在"跟随系统"模式下界面实时变暗。

## 审查关卡与回滚点

- 第 5 步完成后，先跑颜色测试，并截一组暗色截图自查。
- 回滚：迁移 0016 执行 downgrade；前端改动可以按功能模块单独回退。
