# Research: 浏览器验收对象（自定义分类／随手笔记／Skill 输入表单）

- **Query**: TODOS 第 19 项中第 5、6、7 组验收对象的界面入口、预期表现、可行的故障制造方式与建议验收场景
- **Scope**: internal
- **Date**: 2026-11-18

路径均相对仓库根 `/Users/wenyuantc/IdeaProjects/my/aiNovel`。本文只描述现状，未改任何产品代码，未读取 `.env`／凭据类文件。

## 0. 三组共用的事实

### 0.1 只读态的统一来源

- 前端所有「只读」判断都来自 `useProjectAccess(projectId).canEdit`，即作品权限快照的 `can_edit_content`：`frontend/src/utils/projectAccess.ts:39`，hook 在 `frontend/src/hooks/useProjectAccess.ts:10-45`。
- 后端计算在 `backend/app/services/project_access.py:42-67`（`access_for`）：`can_read = owner or accepted or admin_read`（:63），`can_edit_content = owner or editor or admin_edit`（:64），`can_use_ai = owner or editor`（:65）。
- 因此能读但不能编辑的账号只有两类：
  1. 已接受邀请的**查看者**协作者（`member.role == "viewer"`，角色推导在 :54）；
  2. 只有 `projects.read`、没有 `projects.edit` 的**管理员**（:50-51）。
- 2026-11-06 的分类验收使用的是第 2 类：`fields_reader`，仅 `projects.read`（`.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/research/runtime-environment.md:12`）。
- 两类账号 `can_use_ai` 都为 false，章节编辑页的「AI 助手」标签不出现并回退到「场景规划」（`frontend/src/pages/project/ChapterEditorPage.tsx:176`、:1055-1058）。

### 0.2 移动端断点

- `MOBILE_BREAKPOINT = 768`，`useIsMobile()` 返回 `Grid.useBreakpoint().md === false`，即视口宽度 < 768px 为移动端；首次渲染断点未算出时按桌面处理（`frontend/src/hooks/useIsMobile.ts:4-13`）。
- `useModalWidth` 在移动端返回 `calc(100vw - 24px)`（同文件 :16-18）。

### 0.3 假模型

- 类定义：`backend/tests/helpers.py:36`（`FakeLLM`），注入方式是 `create_app(settings, provider_factory=fake_llm.factory)`（`backend/app/main.py:103`、:160；规范见 `.trellis/spec/backend/quality-guidelines.md:197`）。
- **支持脚本化工具调用**：`tool_script` 字段（`backend/tests/helpers.py:56`）与 `push_tools()`（:68-69）。每次 `chat_tools_stream` 弹出一项事件序列（:118），事件类型为 `("text" | "tool_call" | "usage" | "finish", dict)`（`backend/app/ai/tools.py:30`），没写 `finish` 时自动补 `stop`（:133-134）。**脚本队列为空时默认只回一段文本 `ok`**（:118），不会自行调用任何工具。
- 普通文本生成用 `push()`／`responses` 队列（:63-66）。
- 规范说明：`.trellis/spec/backend/agent.md:76-77`。
- 仓库内**没有**可直接启动的假模型服务脚本（`find . -name server.py` 无结果）。以往浏览器验收用的是系统临时目录里的 `server.py`（如 `/tmp/ainovel-planning-browser.J1ZUSm/server.py`，见 `.trellis/tasks/archive/2026-11/11-05-planning-matrix-subplots/progress.md:21`；分类验收的见 `runtime-environment.md:7-8`），不在仓库中。

---

## A. 自定义分类：只读分类页、读取失败重试（TODOS 第 4 项）

### A.1 文件

| 文件 | 说明 |
|---|---|
| `frontend/src/router.tsx:125` | 路由 `projects/:pid/setting-categories` |
| `frontend/src/components/StudioNavigation.tsx:45` | 侧栏入口「设定分类」 |
| `frontend/src/pages/project/SettingCategoriesPanel.tsx` | 页面与工作区 |
| `frontend/src/components/settingCategories/CategoryList.tsx` | 左栏分类列表 |
| `frontend/src/components/settingCategories/CategoryResults.tsx` | 右栏分类结果 |
| `frontend/src/components/settingCategories/CategoryEditorDrawer.tsx` | 规则编辑／只读预览抽屉 |
| `frontend/src/components/settingCategories/CategoryDraftFields.tsx`、`CategoryRuleEditor.tsx`、`CategoryReview.tsx` | 抽屉内表单、条件行、版本对照 |
| `frontend/src/components/settingCategories/categoryRequest.ts`、`useCategoryRequest.ts` | 读取请求控制器（重试语义） |
| `frontend/src/components/settingCategories/categoryEditor.ts` | 抽屉会话控制器 |
| `frontend/src/api/settingCategories.ts:9-44` | 接口路径 |

### A.2 只读态的触发条件与表现

触发条件：`canRead && !canEdit`（见 0.1）。`canRead` 为 false 时整页只显示「当前账号无权查看设定分类」（`SettingCategoriesPanel.tsx:25`）。

只读态下的界面差异：

| 位置 | 可编辑时 | 只读时 | 代码 |
|---|---|---|---|
| 页头 | 有「新建分类」按钮 | 按钮不渲染 | `SettingCategoriesPanel.tsx:56` |
| 页头提示 | 无 | 信息条「当前为只读，可查看完整规则、匹配结果和草稿预览。」 | `SettingCategoriesPanel.tsx:59` |
| 列表行第二个按钮 | 「编辑规则」 | 「规则与预览」（aria-label 前缀由「编辑」变「查看」） | `CategoryList.tsx:27` |
| 结果栏按钮 | 「编辑规则 / 草稿预览」 | 「查看规则 / 预览」 | `CategoryResults.tsx:27` |
| 抽屉标题 | 「编辑自动分类」 | 「分类规则与只读预览」 | `CategoryEditorDrawer.tsx:40` |
| 抽屉内提示 | 无 | 「当前只读，可查看完整规则与预览匹配结果。」 | `CategoryEditorDrawer.tsx:49` |
| 抽屉页脚 | 「删除分类」「保存」 | 两者都不渲染；保留「关闭并保留草稿」 | `CategoryEditorDrawer.tsx:43`、:45 |
| 名称、适用类型、匹配方式、显示顺序 | 可编辑 | `disabled`（`disabled = !canEdit \|\| busy`） | `CategoryEditorDrawer.tsx:24`；`CategoryDraftFields.tsx:18`、:20、:23、:26 |
| 条件行 | 可改，带上移／下移／删除 | 控件 `disabled`，上移／下移／删除不渲染 | `CategoryRuleEditor.tsx:25-28`、:32、:37、:41、:46、:51 |
| 「添加条件」 | 有 | 不渲染 | `CategoryDraftFields.tsx:34` |
| 版本对照区 | 「确认保留草稿，采用最新保存依据」＋「放弃草稿，改用最新分类」 | 只有「采用最新分类查看」 | `CategoryReview.tsx:30-31` |

只读态下**仍可用**：列表筛选与搜索、「查看结果」、结果筛选（求值位置、类型、名称搜索）、分页、「刷新」，以及抽屉里的「预览草稿匹配」（`CategoryEditorDrawer.tsx:58-60`；该按钮只按 `busy`、请求合法性和 `reviewRequired` 禁用，不看 `canEdit`）。后端把 `POST /setting-categories/preview` 登记为读取权限（`.trellis/tasks/archive/2026-11/11-05-setting-auto-categories/design.md:57`）。

写入的二次防线：`controller.edit` 要求 `writable()`（`categoryEditor.ts:106`），保存前置条件同样要求（:134）；页面层 `canEditNow()` 复查在 `SettingCategoriesPanel.tsx:56`、`CategoryEditorDrawer.tsx:34`、:38。

### A.3 读取失败的错误态与重试按钮

| 读取对象 | 接口 | 错误提示与按钮 | 代码 |
|---|---|---|---|
| 分类列表 | `GET /projects/{pid}/setting-categories` 与字段定义列表并行 | 错误条＋「重试」 | `CategoryList.tsx:19-22`、:37 |
| 分类结果 | `GET /projects/{pid}/setting-categories/{id}/entries` 与字段定义列表并行 | 错误条＋「重试读取」 | `CategoryResults.tsx:17-22`、:32 |
| 已有章节 | `chapterApi.list` | 「已有章节读取失败：…」＋「重试章节」（页面与抽屉各一处） | `SettingCategoriesPanel.tsx:38-39`、:62；`CategoryEditorDrawer.tsx:57` |
| 抽屉完整配置 | `settingCategoriesApi.get` 等 | 「尚未取得完整分类与字段配置」＋「重试读取」；会话错误另显示在上方错误条 | `CategoryEditorDrawer.tsx:50`、:53；`categoryEditor.ts:90-95` |
| 草稿预览 | `POST /setting-categories/preview` | 错误条＋「重试预览」 | `CategoryEditorDrawer.tsx:62`；`categoryEditor.ts:182` |

重试语义：`CategoryRequest.refresh()` 每次先 `invalidate()`（清空数据、中止在途请求、世代号加一），再发新请求，同一 revision 也会重新读取（`categoryRequest.ts:18-36`；注释在 `useCategoryRequest.ts:13`）。失败时状态为 `{ data: null, loading: false, error }`（`categoryRequest.ts:33`），所以失败后列表／结果区不保留旧数据，只剩错误条和筛选控件。列表与结果栏右上角的「刷新」按钮走同一个 `refresh`（`CategoryList.tsx:33`、`CategoryResults.tsx:24`）。

### A.4 2026-11-06 未验的原因

`.trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/research/category-browser-verification.md:44-49`：

- 只读页：`fields_reader` 登录后的控件状态「尚未执行」（:46）。
- 读取失败重试：当时停掉了临时后端 8014 来制造故障，但「浏览器动作在请求发出前被站点权限拒绝」（:47）。
- 阻挡来自**浏览器驱动工具的权限**，不是产品代码：先是「用户拒绝访问 5179」，用户重新授权后工具仍返回「已保存权限或管理策略拒绝」（:48）。当时用的是 Codex 的 IAB／CUA（:10），前端端口 5179。
- 记录明确没有换端口、换浏览器、raw CDP 或脚本抓页面来绕过（:48）。

### A.5 在浏览器里制造读取失败的可行方式

依据代码，下列任一方式都能让上表的读取进入错误态（`errorMessage(error)` 的文案随失败类型而变）：

1. **浏览器层拦截请求**：阻断或改写 `**/projects/*/setting-categories*` 的响应（例如返回 500 或中止）。优点是可只打掉某一个读取（列表、结果、预览），页面其余部分和登录态不受影响；恢复后点「重试」即可验证同 revision 重读。
2. **DevTools 离线／网络节流为 Offline**：所有读取同时失败，恢复在线后逐个重试。
3. **停后端进程**：与 2026-11-06 的做法一致。注意前端开发服务器只是代理后端（`runtime-environment.md:9` 记作「仅代理 8014」），停后端后页面本身仍能加载；重启后端后点重试。停后端期间其他轮询请求也会失败，页面上可能出现分类之外的报错。

方式 1 的故障范围最小，最贴近「同 revision 重试」这一验收点。

### A.6 建议验收场景（7 个）

前置：一个作者账号建好的作品，至少 1 个带规则且有匹配结果的分类、若干章节；一个只读账号（查看者协作者或仅 `projects.read` 的管理员）。

| # | 场景 | 步骤 | 预期可见结果 |
|---|---|---|---|
| A1 | 只读页头与列表 | 只读账号打开 `/projects/{pid}/setting-categories` | 无「新建分类」；有「当前为只读，可查看完整规则、匹配结果和草稿预览。」；列表行按钮为「查看结果」「规则与预览」 |
| A2 | 只读查看结果 | 点「查看结果」，切换求值位置、类型筛选、分页 | 结果与作者账号同条件下一致；结果栏按钮为「查看规则 / 预览」 |
| A3 | 只读抽屉 | 点「规则与预览」 | 标题「分类规则与只读预览」；提示「当前只读…」；名称／类型／匹配方式／顺序与各条件均禁用；无上移、下移、删除、添加条件、删除分类、保存；有「关闭并保留草稿」 |
| A4 | 只读草稿预览 | 在 A3 抽屉里点「预览草稿匹配」 | 返回匹配计数与样本，不报权限错误；关闭抽屉后页面无「未保存的分类草稿」警告 |
| A5 | 列表读取失败后重试 | 作者账号，拦截列表请求使其失败后进入页面或点「刷新」；解除拦截后点「重试」 | 失败时出现错误条＋「重试」，列表区无旧数据；重试后列表恢复，内容与失败前一致 |
| A6 | 结果读取失败后重试 | 选中一个分类，拦截 `…/entries` 请求后点结果栏「刷新」；解除后点「重试读取」 | 失败时出现错误条＋「重试读取」，筛选控件仍在；重试后结果与总数恢复，URL 中的 `id` 与筛选参数不变 |
| A7 | 抽屉配置或预览读取失败后重试 | 拦截单个分类的读取后点「编辑规则」，或拦截 `preview` 后点「预览草稿匹配」；解除后点对应重试 | 前者显示「尚未取得完整分类与字段配置」＋「重试读取」，重试后表单出现；后者显示错误条＋「重试预览」，重试后预览出现且已填草稿未丢 |

A1–A4 也可在 375px 视口复查一遍：列表在移动端改用卡片（`CategoryList.tsx:39-41`）。

---

## B. 随手笔记：只读账号界面、移动端抽屉（TODOS 第 9 项）

### B.1 文件

| 文件 | 说明 |
|---|---|
| `frontend/src/router.tsx:128` | 路由 `projects/:pid/notes` |
| `frontend/src/components/StudioNavigation.tsx:56` | 侧栏入口「笔记」 |
| `frontend/src/pages/project/NotesPanel.tsx` | 笔记列表页（列表＋URL 驱动的编辑抽屉） |
| `frontend/src/components/notes/NoteEditor.tsx` | 笔记编辑器（列表抽屉与笔记栏共用） |
| `frontend/src/components/notes/NoteFlagButtons.tsx` | 收藏／固定标记 |
| `frontend/src/components/notes/NoteSidePanel.tsx` | 章节编辑页「笔记」栏 |
| `frontend/src/pages/project/ChapterEditorPage.tsx` | 笔记栏的宿主：桌面侧栏与移动端底部抽屉 |
| `frontend/src/hooks/useIsMobile.ts` | 断点 |

### B.2 只读账号下的表现

触发条件同 0.1。`canRead` 为 false 时列表页与笔记栏都只显示「当前账号无权查看作品笔记」（`NotesPanel.tsx:23`、`NoteSidePanel.tsx:29`）。

笔记列表页：

- 「新建笔记」按钮不渲染（`NotesPanel.tsx:47`）。
- 抽屉标题为「查看笔记」而非「编辑笔记」（:59）。
- 手动访问 `?new=1` 时抽屉内显示「当前账号无权创建笔记」（:61-62）。
- 搜索、「全部／收藏」筛选、分页、刷新照常可用（:50-54、:103）。

标记（列表行、编辑器头部共用）：`NoteFlagButtons.tsx:46-52`。只读时没有可点击的星标／图钉按钮；笔记已收藏或已固定时只显示对应的静态图标（aria-label「已收藏」「已固定」），两者都没有时什么都不渲染。列表行另有「已固定」标签，不受权限影响（`NotesPanel.tsx:94`）。

编辑器（`NoteEditor.tsx`）：

- 「编辑／查看原文」切换不出现，改为文字「只读查看」（:140-142）。
- 「保存」按钮不渲染（:147）。
- 不渲染表单，直接显示标题与正文的只读视图（:155、:167）。
- 「删除笔记」不渲染（:170-172）。

章节编辑页笔记栏（`NoteSidePanel.tsx`）：

- 「新建」按钮不渲染（:105）。
- 空态文案不含「或新建笔记」（:114）。
- 固定笔记列表、刷新、搜索并打开其他笔记、「关闭笔记」照常可用（:87-104、:109）。
- 打开的笔记用同一个 `NoteEditor`，表现同上。

设计依据：`.trellis/tasks/archive/2026-11/11-08-snippet-favorite-pin-sidebyside/prd.md:35`、:45（AC6），`design.md:45`、:65。

### B.3 移动端抽屉

章节编辑页（TODOS 所说的「移动端抽屉」，依据 `11-08-snippet-favorite-pin-sidebyside/prd.md:16`、:31 与 `implement.md:42`）：

- 桌面端：右侧 360px 检视面板，`!isMobile` 时渲染（`ChapterEditorPage.tsx:1299`）；头部有「收起侧栏／辅助面板」按钮（:767-777）。
- 移动端（< 768px）：右下角主色悬浮按钮，提示「辅助面板」（:1303-1310），点击打开底部抽屉 `Drawer placement="bottom" height="85vh" forceRender`（:1311-1332）。
- 抽屉标题随当前标签变化，笔记标签为「📝 笔记」（:163、:1312）；标题右侧的 `Segmented` 切换标签，选项与桌面端标签页相同（:1313-1320）。标签项为「AI 助手」（仅 `canUseAI`）、「场景规划」「章节设置」「笔记」（:1055-1066）。
- 笔记内容 `notesContent` 始终渲染，非当前标签时 `display: none`（:1069-1073、:1331）；抽屉 `forceRender`，所以关闭抽屉、切换标签都不卸载笔记栏。

抽屉「笔记」标签内可做的操作（即 `NoteSidePanel` 的全部功能）：

- 查看固定笔记列表并点击打开，刷新列表，读取失败时重试（`NoteSidePanel.tsx:86-98`）。
- 搜索并打开任意笔记（:104、:130-161）。
- 新建笔记（需 `canEdit`，:105）。
- 编辑标题与正文、保存、删除、切换收藏／固定、「编辑／查看原文」切换（`NoteEditor.tsx:140-172`）。
- 有未保存修改时切换或关闭笔记，弹出「笔记有未保存修改」确认（`NoteSidePanel.tsx:55-72`）。
- 笔记栏内 Ctrl/⌘+S 不冒泡到正文保存（:81-84）。
- 笔记有未保存修改时离开页面被拦截（`ChapterEditorPage.tsx:395`、:408、:1402、:1418-1419）。

笔记列表页的抽屉在移动端宽度为 `100%`，桌面为 760（`NotesPanel.tsx:59-60`）；搜索框在移动端占满宽度（:51）。

### B.4 已知问题（TODOS 第 23 项）在代码里的成因

现象：窗口宽度跨越 768px 时，章节编辑页笔记栏重建，未保存笔记不提示即丢失。

成因位置：

1. `notesContent` 这同一段 JSX 被放在两个互斥的分支里：桌面分支 `{!isMobile && <div …>{inspectorPanel}</div>}`（`ChapterEditorPage.tsx:1299`，经 `inspectorPanel` 的 :1095）与移动分支 `{isMobile && (<>…<Drawer>…{notesContent}</Drawer></>)}`（:1301-1333，:1331）。两处在 React 树中的位置不同，`isMobile` 翻转时旧分支整体卸载、新分支重新挂载，`NoteSidePanel` 是一个全新实例。代码注释也写明「桌面与移动端互斥只渲染一份」（:1068），设计文档同（`design.md:72`）。
2. 笔记栏的选中项只存在组件内存里：`const [selection, setSelection] = useState(null)`（`NoteSidePanel.tsx:37`），组件注释写明「选中项只保存在内存；宿主须保持本组件挂载（隐藏而非卸载）以免丢失草稿」（:21-24）。草稿本身在 `NoteEditor` 的本地状态里，同样随卸载消失。
3. 不提示的原因：卸载时清理函数直接上报 `onDirtyChange(false)`（`NoteSidePanel.tsx:53`），宿主的 `noteDirty` 被清零（`ChapterEditorPage.tsx:178`）；未保存确认只挂在笔记栏内的 `open()` 切换／关闭路径上（`NoteSidePanel.tsx:55-72`）和路由离开拦截上，断点切换不经过任何一条。
4. 触发条件是 `Grid.useBreakpoint()` 的 `md` 翻转（`useIsMobile.ts:10-13`），拖动窗口、旋转设备、开关 DevTools 侧栏或在设备模拟里改宽度都会触发。

验收时的识别特征：在同一页面会话内改变视口宽度并跨过 768px 后，笔记栏回到「未选中任何笔记」的空态，固定列表重新加载，没有任何确认弹窗；正文编辑区不受影响。这是第 23 项，不是新缺陷。**验收移动端抽屉时应先设好窄视口再加载页面，中途不要改宽度。**

### B.5 建议验收场景（9 个）

前置：作者账号的作品里至少 3 篇笔记（一篇已固定且已收藏、一篇仅收藏、一篇无标记）、至少 1 个章节；一个只读账号。

| # | 场景 | 步骤 | 预期可见结果 |
|---|---|---|---|
| B1 | 只读列表页 | 只读账号打开 `/projects/{pid}/notes` | 无「新建笔记」；已标记的笔记只显示静态星标／图钉，无法点击；无标记的笔记行不出现标记控件；搜索与「收藏」筛选可用 |
| B2 | 只读查看笔记 | 点一篇笔记 | 抽屉标题「查看笔记」；显示「只读查看」；无保存、删除、编辑切换；标题正文为只读文本 |
| B3 | 只读直达新建链接 | 地址栏改为 `…/notes?new=1` | 抽屉内显示「当前账号无权创建笔记」 |
| B4 | 只读笔记栏（桌面） | 只读账号打开章节编辑页，切到「笔记」标签 | 标签里无「AI 助手」；笔记栏无「新建」；空态文案不含「或新建笔记」；可打开固定笔记与搜索结果，内容只读 |
| B5 | 移动端打开抽屉 | 作者账号，视口先设为 375px 宽再加载章节编辑页；点右下「辅助面板」悬浮按钮，用标题栏分段控件切到「笔记」 | 无右侧侧栏；底部抽屉高约 85vh，标题「📝 笔记」；页面无横向溢出 |
| B6 | 抽屉内编辑保存 | 在 B5 抽屉里打开固定笔记，改正文后点保存；再新建一篇并保存 | 状态由「有未保存修改」变为「已保存 · 时间」；新笔记保存后可继续编辑；正文编辑区内容未被改动 |
| B7 | 抽屉内草稿保留 | 改笔记不保存，切到「场景规划」再切回；关闭抽屉再打开 | 两种操作后草稿都还在，仍显示「有未保存修改」 |
| B8 | 抽屉内切换与离开拦截 | 有未保存修改时点另一篇固定笔记；取消后点侧栏其他页面 | 先弹「笔记有未保存修改」（放弃修改并切换／继续编辑）；离开页面时弹拦截框，文案提到笔记栏未保存修改 |
| B9 | 只读账号的移动端抽屉 | 只读账号、375px 视口打开章节编辑页并打开抽屉 | 分段控件无「AI 助手」；笔记标签表现与 B4 相同 |

补充：笔记列表页在 375px 下抽屉占满宽度（`NotesPanel.tsx:60`），可在 B1／B2 顺带观察。

---

## C. Skill 输入表单：批量、改写、场景区块入口与智能体代填（TODOS 第 10 项）

### C.1 共用组件

| 文件 | 说明 |
|---|---|
| `frontend/src/hooks/useSkillChoice.ts:25-35` | 选中的 Skill＋输入值；切换 Skill 时用新 Skill 默认值重置（:28-31） |
| `frontend/src/components/skills/SkillChoiceField.tsx:15-22` | Skill 选择器＋输入表单 |
| `frontend/src/components/skills/SkillInputsForm.tsx:17-108` | 表单本体；Skill 无输入项时不渲染（:20）；必填标记（:47-49）、错误文案「必填项，请填写」（:79）、「预览填入后的 Skill 正文」按钮与弹窗（:83-105） |
| `frontend/src/utils/skillInputs.ts` | `skillChoiceProblemText` 等校验 |

### C.2 六个手动入口

六个入口的清单出自 `.trellis/tasks/archive/2026-11/11-08-prompt-components-inputs/prd.md:22` 与 `11-08-prompt-inputs/implement.md:13`、`prd.md:13`。实测状态依据 TODOS 第 10 项验收记录（`TODOS.md` 第 10 项「验收记录」段）。

| # | 入口 | 界面位置 | 组件与代码 | 表单在哪里填 | 实测状态 |
|---|---|---|---|---|---|
| 1 | 章节生成／续写（含润色） | 章节编辑页 → 检视面板「AI 助手」→「Skill（可选）」 | `ChapterEditorPage.tsx:214`（状态）、:338、:795（渲染）；请求 :527、:530；必填拦截 :516、:603、:875、:887、:897 | 本入口自己的表单 | 已实测（「章节编辑页表单默认值、必填拦截、预览与实际出站…一致」） |
| 2 | 局部改写 | 章节编辑页「AI 助手」→ 模式标签「局部改写」 | `components/chapter/RewritePanel.tsx:41`、:143-144、:187、:356；宿主传参 `ChapterEditorPage.tsx:827-838` | 复用入口 1 的表单（同一个 `skillChoice`） | 未实测 |
| 3 | 批量生成 | 章节列表页 `/projects/{pid}/chapters` →「批量生成」按钮 → 弹窗内「Skill（可选）」 | `components/batch/BatchGenerateModal.tsx:98-102`、:138-141、:176、:266-270；入口按钮 `pages/project/ChaptersPanel.tsx:113-119`，挂载 :274-276 | 弹窗内独立表单，整批共用一份 | 未实测 |
| 4 | 场景 AI | 章节编辑页「AI 助手」→ AI 处理范围选「当前场景」后生成／润色 | 范围切换 `ChapterEditorPage.tsx:783`；`aiScope === 'scene'` 时走 `sceneApi.generate`／`polish`（:528、:531） | 复用入口 1 的表单 | 未实测（见 C.6 第 3 点） |
| 5 | 区块改写 | 章节编辑页正文中的分区块 → 区块改写弹窗 | `components/scenes/RichSectionRewriteModal.tsx:23`、:81、:148、:165、:270、:277；由 `SceneWritingPanel.tsx:237`、:524、:630-634 打开并传入 `skill`；宿主传参 `ChapterEditorPage.tsx:1246-1249` | 复用入口 1 的表单；必填未填时弹窗内警告「请在 AI 助手的「Skill（可选）」下方填写。」（:270） | 未实测 |
| 6 | 智能体对话手动指定 Skill | 项目智能体面板 → 输入框上方「Skill（可选，或输入 / 选择触发词）」 | `components/agent/AgentConversationView.tsx:38-40`、:59-60、:105、:118；面板 `AgentPanel.tsx:70`、`AgentDock.tsx:19` | 面板内独立表单；发送成功后清空（:60） | 已实测（「智能体手动指定 Skill 的必填校验与注入顺序」） |

说明：

- 入口 2、4、5 没有自己的表单，都读取章节编辑页 AI 助手里那一份 `skillChoice`。验收这三处时要看的是「在 AI 助手里填的值是否进入了该入口的请求」，以及必填未填时该入口自己的拦截提示。
- 入口 2 的拦截提示在 `RewritePanel.tsx:356`（改写模式下 `ChapterEditorPage.tsx:875` 那条通用警告不显示，条件为 `aiMode !== 'rewrite'`）。
- 同一个 `skillSelect` 还渲染在分析抽屉的「按建议改写」处（`ChapterEditorPage.tsx:1398`，请求 :561-562）。它不在归档文档的六个入口清单里。
- 后端自动化覆盖：`backend/tests/test_skill_inputs.py:254`（润色与改写）、:279（批量逐章同值）、:459（场景生成与区块改写）。

### C.3 智能体代填路径

三条路径（`11-08-prompt-inputs/prd.md:16-19`、`design.md:40-42`）：

1. **触发词启用**：消息以 `/触发词` 开头且没有手动选 Skill。
   - 前端：输入 `/` 时出现触发词候选（`AgentConversationView.tsx:43`、:109）；命中后显示提示「本轮将启用 Skill「…」；它有 N 个输入项，将由智能体根据对话代填，必填项不确定时会先问你」（:44、:117）。此时不出现表单（:37 注释）。
   - 后端：`skill_service.match_trigger`（`backend/app/services/skill_service.py:586-594`）；快照以 `fill=False` 保存未填充正文（`backend/app/services/task_engine/handlers/agent.py:371-385`）；`forced_skill_message` 在系统消息后追加「【该 Skill 的输入项】」清单与代填说明（`backend/app/services/agent/context.py:614-623`）。
2. **智能体自行 `load_skill`**：模型调用工具 `load_skill`，返回未填充正文＋`inputs` 定义＋`instructions`（`backend/app/services/agent/tools/skills.py:63-84`）。代填说明原文在 `backend/app/ai/skill_inputs.py:121-124`。
3. **`start_task` 传输入值**：`params.skill_inputs` 为 `{name, value}` 对象数组（`backend/app/services/agent/tools/tasks.py:110-120`），执行前转回映射，标识重复报错（:154-162），之后按任务请求 schema 校验（:163）；工具描述写明「缺必填项会返回错误」（:133）。

界面触发方式：路径 1 由作者在智能体输入框键入触发词直接触发；路径 2、3 取决于模型的工具调用，界面上无法由作者直接指定。

### C.4 用假模型走到代填路径

- 假模型默认行为是回文本 `ok`（`backend/tests/helpers.py:118`），所以路径 2、3 必须**预先向 `tool_script` 压入脚本**。自动化测试的写法：
  - `load_skill`：`fake_llm.push_tools([("tool_call", {"id": "a", "name": "load_skill", "arguments": json.dumps({"slug": "tone"})})])`，下一轮再压一段文本收尾（`backend/tests/test_skill_inputs.py:329-332`）。
  - `start_task`：`backend/tests/test_skill_inputs.py:360-402` 直接调用工具对象而非经假模型；经假模型时同样是压入 `name: "start_task"` 的 `tool_call` 事件。
- 脚本队列是进程内的 Python 列表，按调用顺序全局共享。归档记录过一次因多轮共享 `tool_script` 导致脚本被错轮次消费的情况（`.trellis/tasks/archive/2026-11/11-09-novel-collaborators/implement.md:2097-2098`）：确认 `start_task` 后轮次会自动续跑，续跑也消费队列。浏览器验收时每一步操作前要保证队列里恰好是这一步需要的脚本。
- 路径 1 不需要脚本也能在界面上走通到「系统消息已附输入项定义」这一步，但附加的内容只进入发给模型的消息，界面上看不到；要核对需读取假模型记录的 `tool_calls[*].messages`（`backend/tests/helpers.py:104-117`）。
- 假模型不理解对话，「代填得对不对」「必填不确定时追问」这类语义行为无法用假模型验证，只能验证定义是否送达、工具调用是否被正确受理或拒绝。

### C.5 建议验收场景（9 个）

前置：作者账号；一个带输入项的 Skill（参照 `11-08-prompt-inputs/prd.md:24`：「基调」下拉，选项轻松／沉重，默认轻松；「重点人物」单行必填），正文含 `{{ 标识 }}` 占位，并配一个触发词；一个作品含至少 3 个章节、其中一章有多个场景和一个分区块；假模型配置。

| # | 场景 | 步骤 | 预期可见结果 |
|---|---|---|---|
| C1 | 批量：表单与必填拦截 | 章节列表页点「批量生成」，在弹窗选该 Skill | 表单出现，「基调」预填默认值；「重点人物」为空时显示必填错误，确定按钮禁用 |
| C2 | 批量：预览与逐章同值 | 填写后点「预览填入后的 Skill 正文」，再提交 | 预览正文已代入两项值；任务启动；假模型记录中每一章的出站消息都含同一段填充后的正文 |
| C3 | 局部改写：沿用 AI 助手表单 | 章节编辑页在 AI 助手选 Skill 并留空必填，切到「局部改写」选中一段文字 | 改写面板出现必填警告，无法开始；填好后可开始，出站消息含填充后的正文 |
| C4 | 场景 AI | AI 处理范围选「当前场景」，选 Skill 并填写后生成 | 仅当前场景正文更新；出站消息含填充后的正文；必填留空时生成按钮禁用并有警告 |
| C5 | 区块改写 | 对正文分区块打开区块改写弹窗；先留空必填，再回 AI 助手填写后重开 | 留空时弹窗内警告「请在 AI 助手的「Skill（可选）」下方填写。」且发送按钮禁用；填好后可发送，出站消息含填充后的正文 |
| C6 | 切换 Skill 重置 | 在任一未实测入口填值后换成另一个 Skill 再换回 | 表单回到默认值，上次填写内容不保留 |
| C7 | 智能体：触发词启用 | 智能体输入框键入 `/` 选触发词并附一句话后发送 | 发送前出现「…它有 2 个输入项，将由智能体根据对话代填…」；不出现表单；发送后输入框清空；假模型收到的系统消息含「【该 Skill 的输入项】」清单 |
| C8 | 智能体：自行 `load_skill` | 预先压入 `load_skill` 工具调用脚本，发送一条普通消息 | 时间线出现 `load_skill` 工具步骤并成功；假模型下一次调用的消息里，工具结果含 `inputs` 与 `instructions` |
| C9 | 智能体：`start_task` 传输入值 | 先压入缺必填项的 `start_task` 脚本，再压入带齐 `skill_inputs` 的脚本 | 第一次工具步骤返回错误，未创建任务；第二次出现任务提案／预览，确认后任务启动，出站消息含填充后的正文 |

---

## 相关规范

- `.trellis/spec/backend/setting-categories.md` — 分类后端契约
- `.trellis/spec/backend/agent.md:76-77` — FakeLLM `tool_script`／`push_tools` 说明
- `.trellis/spec/backend/quality-guidelines.md:192-197` — FakeLLM 注入方式与自动分析默认关闭
- `.trellis/spec/frontend/` — 未逐篇核对是否有与这三组对象对应的专门条目

## Caveats / 未查实

1. **「浏览器站点策略」现在是否仍会阻挡**：未查实。归档只说明阻挡来自当时浏览器驱动工具对 `5179` 端口的已保存权限／管理策略（`category-browser-verification.md:48`），与产品代码无关；本次将使用哪种浏览器驱动、是否受同一策略约束，无法从仓库判断。
2. **可复用的假模型服务脚本**：未查实。仓库内没有；以往的 `server.py` 与 seed 脚本在系统临时目录，是否仍存在未检查。
3. **浏览器会话期间如何向运行中的后端压入 `tool_script`**：未查实。`tool_script` 是进程内列表，需要宿主脚本提供压入手段；以往智能体浏览器验收具体怎么做的，归档中没有找到记录。
4. **入口 1 的已实测范围是否覆盖「当前场景」**：未查实。TODOS 只写「章节编辑页」，没有说明当时的 AI 处理范围；按 TODOS 把「场景区块入口」列为未实测，本文将入口 4 记为未实测。
5. **TODOS「场景区块入口」的断句**：TODOS 原文是「批量、改写、场景区块入口」，本文按归档六入口清单理解为场景 AI 与区块改写两个入口；这是依据清单的对应，不是 TODOS 的明文。
6. **智能体时间线对 `load_skill`／`start_task` 步骤与错误的具体展示文案**：未查实，未阅读 `AgentTimeline.tsx` 的工具步骤渲染；C8、C9 的「预期可见结果」中界面部分只写到步骤出现与成败。
7. **各读取失败时错误条的具体文案**：未查实，取决于 `frontend/src/api/client.ts` 的 `errorMessage`，未阅读。
8. **只读账号进入章节编辑页的其余表现**（正文是否可编辑等）不在本次调研范围，未核对。
