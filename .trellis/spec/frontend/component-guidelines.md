# 组件规范

> 本项目中组件的构建方式。

---

## 概述

所有 UI 元素都是 Ant Design 5 组件。语言环境为中文：`App.tsx` 中使用 AntD `zh_CN`，`main.tsx` 中使用 dayjs `zh-cn`。反馈（`message`、`modal`）始终来自 `App.useApp()`，以继承 `ConfigProvider` 的主题/语言环境——绝不使用静态导入的 `message.success(...)`。

```tsx
// src/App.tsx
<ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
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
| `AppLayout` | 顶部 `Layout.Header` 带菜单（`我的项目` / `模型配置`；在 `/admin` 下不高亮任何菜单项），用户名下拉菜单：`用户管理`（仅当 `user.is_admin` 时显示）→ `/admin/users`，`修改密码` → `ChangePasswordModal`，退出登录调用 `useAuthStore().logout()` 后执行 `navigate('/login', { replace: true })`；渲染 `<Outlet/>`，并在底部挂载 `<TaskPanel/>`（仅登录区可见）。 |
| `RequireAuth` | 路由守卫：`status === 'unknown'` → `<FullPageSpin/>`；`'anonymous'` → `<Navigate to="/login" replace state={{ from: location.pathname + location.search }} />`。`LoginPage` 读取 `state.from`（默认 `/projects`）。 |
| `RequireAdmin` | 包裹 `/admin/users`（位于 `RequireAuth` 内部）。判定逻辑是 `utils/adminUsers.ts` 中的纯函数 `adminGuard(status, user)`：`'loading'` → `<FullPageSpin/>`，`'login'` → `/login`，`'home'`（非管理员）→ `/`，`'allow'` → children。`is_admin` 只从 `/auth/me` 读取一次；被降级的管理员在刷新前仍会看到菜单，但 API 调用会返回 403 并显示提示信息。 |
| `ChangePasswordModal` | `{ open; onClose }`。原密码 + 两次输入新密码（`utils/validation`）。成功 → `其他设备上的登录已失效`；后端会重新签发 cookie，因此当前标签页保持登录。400 `原密码不正确` 通过 `errorMessage(err)` 在表单内显示，不会触发全局登出。 |
| `AIConfigSelect` | `{ value?: number \| null; onChange?: (v: number \| null) => void; disabled?: boolean }`。挂载时调用 `useAIConfigStore().load()`。空选择 = `null` = "使用默认配置"（placeholder 为 `默认：{name}（{model}）`）。加载完成且配置数为零时，渲染一个链接到 `/settings/ai` 的警告 `Alert` 以代替下拉框。 |
| `StreamOutput` | `{ text; running; placeholder?; maxHeight? = 360 }`。`pre-wrap` 等宽文本框，`running` 时自动滚动到底部，运行中显示一个小的 `Spin`。 |
| `WizardGenerator<K extends WizardKind>` | `{ projectId; kind: K; existingCount?; showResult?; onDone?(items: WizardItemMap[K][]); onRunningChange?(running) }`。走**后台任务**：`runWizard(...)` 返回 202 `Task` → `useTasksStore.track(task)` → `useTaskStream().follow(task)`；完成时用 `parseWizardDone(kind, result)` 取 items。**挂载时**按 `GET /tasks?project_id&kind=wizard.{kind}&active=true&limit=1` 查活动任务并自动接上（关闭页面 / 弹窗后再打开可继续看实时输出）。`running = 订阅中 \|\| 创建请求中`，表单禁用；`useHasAIConfig() === false` 时开始按钮禁用；运行中显示危险样式按钮（排队时为 `取消排队`，否则 `停止`），调用 `task.cancel()`（`POST /tasks/{id}/cancel`）。排队中显示 `queueLabel(task)` 的 info `Alert`，运行中显示进度条 + `stage`，断线重连时显示警告。取消时显示 `已停止生成，原有数据未改变`；失败 / 中断时 `生成失败` Alert。`onRunningChange` 通过 ref + `[running]` effect 上报。`existingCount > 0` 时显示"将会替换"的 `Alert`。`WizardPage` 的步骤初始取 `?kind=`（没有时按 `resumeWizardStep(project.wizard_progress)` 定位），之后每次导航（`location.key` 变化，含跳到同一 `kind`）都在渲染期按 `kind` 切换步骤。大纲步骤在已有大纲时显示「覆盖现有大纲 / 追加到末尾」切换（`outline_mode`，覆盖提示会先自动存快照、章节只解除关联；追加提示从下一卷下一章继续），追加成功文案 `大纲已追加 N 条`。 |
| `WizardModal<K>` | 将 `WizardGenerator` 包在 `Modal` 中（`maskClosable={false}`、`destroyOnHidden`、`footer={null}`）。运行中拒绝关闭（`message.warning('正在生成，请等待完成或先停止')`）——任务本身不会因关闭而中断，但面板需要 `onDone` 回调来刷新列表；重新打开会自动接上。完成时：先 `onDone(items)` 再 `onClose()`。被 `WorldPanel`、`CharactersPanel`、`OutlinesPanel` 使用。 |
| `TaskPanel` | 无 props，挂在 `AppLayout`。挂载时 `useTasksStore.startPolling()`（卸载时停止）。右下角 `FloatButton`（`badge.count = active.length`，tooltip `后台任务`）打开 `Drawer`（宽 520）：Tab「进行中（N）」= store 的 `active`；Tab「最近」= `taskApi.list({ active: false, limit: 50 })`，依赖 store 的 `version`（任务结束 / 本地操作后自动重载）。每行：标题、`TASK_STATUS` 标签、活动任务的 `Progress`、`queueLabel` 或 `stage`、创建 / 结束时间（`formatTime`）、已结束任务的 `error`。操作：成功且 kind 在 `components/entities/taskResultViewers.ts` 注册了结果组件时显示 `查看结果`（`TaskResultModal`，见下方「设定类组件」）；`查看输出`（`TaskOutputModal`：活动任务用 `useTaskStream().follow` 实时显示，已结束读 `/tasks/{id}/output`）、`跳转`（`taskLink`：章节任务 → 编辑器（`chapter.analyze` / `chapter.revise` 带 `?panel=analysis` 打开分析抽屉）；向导任务 → `/projects/{pid}/wizard?kind=…`；`character.generate` / `relations.parse` → 角色页；`organization.generate` → 组织页；`career.generate` → 职业页；`outline.continue` / `outline.expand` → 大纲页；`wizard.careers` → 向导职业步骤）、活动任务 `取消`（Popconfirm → `track(返回的 TaskOut)`）、可重试状态 `重试`（→ `track(新任务)`）、已结束 `删除`（→ `bump()`）。抽屉顶部 `刷新`、`清理已结束`（Popconfirm → `DELETE /tasks?finished=true` → `已清理 N 个已结束任务`）。 |
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
- `ProjectLayout.NAV`：在「角色」后新增「组织」「关系」「职业」。`StoryPreview.CharacterPreview` 展示结构化字段（只显示非空行），`WorldPreview` 带分类标签。

### 职业等级体系（`10-23-career-system`）

| 组件 | 契约 |
|-----------|------------------------|
| `careers/StageListEditor` | `{ value: StageRow[]; onChange; disabled?; lockedIds?; error? }`。阶段阶梯受控编辑：顺序即 position，每行 名称（≤50）/ 描述 / 上移 / 下移 / 删除（`moveItem`，**不引入拖拽依赖**）；`lockedIds`（被持有人引用的阶段）与只剩 1 阶时删除禁用；底部「添加阶段」（`newStageRow`）与 `validateStages` 的错误文案。职业抽屉与 AI 草稿共用。 |
| `careers/CareerEditDrawer` | `{ projectId; open; career: Career \| null; onClose; onSaved(career) }`。`Drawer`（宽 760，`destroyOnHidden`），内部 `key = career.id ?? 'new'`：基础字段（名称、类别 主修/副修、描述、获得条件、特殊能力、体系规则）+ `StageListEditor` + 保存（`rowsToStages` 整体提交，新建默认一阶「第一阶」）+ 已有职业的持有人表（`GET /careers/{id}/holders`，按 `updated_at` 重新加载；持有人所在阶段即 `lockedIds`）。 |
| `careers/CareerDraftResult` | `career.generate` 结果组件（已在 `taskResultViewers` 注册）。每个草稿一张卡：勾选、名称、类别、阶数，折叠区编辑四个文本字段与阶段；同名（后端 `conflict` 且未改名，或与最新加载的职业名单相同）→ 勾选框禁用 + 红字 `DRAFT_NAME_CONFLICT`；其他已勾选但不合法（批内重名 / 阶段非法）阻止保存。保存 `careers/batch`（`draftsToBatch`，不含 `conflict`），成功后禁用。 |
| `careers/CharacterCareersTab` | `{ projectId; character; onChanged? }`。角色抽屉「职业」Tab：挂接表（身份标签、职业、阶段下拉——**变更即 PATCH 换阶**、状态备注、编辑 / 卸下 Popconfirm），每行展开为该职业的历史（`career-history` 按 `career_id` 过滤）；「查看全部职业历史」含已卸下职业。「挂接职业」弹窗：职业下拉按 `assignBlockReason` 禁用并标注原因、阶段（不选 = 第 1 阶）、状态备注、第几章 / 历史备注（只记入历史）。每次变更重新加载挂接与历史。 |
| `careers/CareerSuggestionList` | AI 生成角色的职业建议表：`resolved=false` 删除线 + 红字原因、不可勾选；可换阶段（用本项目职业列表）与改备注。 |
| `StoryPreview.CareerPreview` | 职业体系向导结果：类别标签、阶梯、非空的描述 / 条件 / 能力 / 规则。 |

- `CareersPanel`（`/projects/:pid/careers`，懒加载路由，NAV「职业」在「关系」之后）：按主修 / 副修分组的卡片（阶梯 `position·name` 标签串 + 阶数 + 持有人数），编辑 / 删除（提示会清掉挂接与历史）；工具栏 `AI 生成职业`（`EntityTaskModal kind="career.generate"`：数量 1–5、类别可选、要求）、`AI 重新生成全部`（`WizardModal kind="careers"`）、`新增职业`。
- `CharactersPanel`：抽屉新增「职业」Tab；卡片显示「主职业：剑修·筑基（2/9，瓶颈期）」与副职业（`careerBadges(assignments)`）。数据来自项目级挂接列表 `assignmentApi.listProject(pid)`（`GET /projects/{pid}/character-careers`，一次请求，不再按职业逐个取 `holders`），抽屉关闭 / 生成 / 导入 / 删除后 `reloadBadges()`。
- `CharacterDraftResult`：`career_suggestions` 非空时多一个「职业建议」Tab；`careerSuggestionLimitError` 超出主 1 副 3 时阻止保存；`with-relations` 带 `careers: careerSuggestionsToDrafts(...)`。
- 向导：`WizardPage.STEPS` 为 世界观 → **职业体系（可跳过）** → 角色 → 大纲；职业步骤 `career_count` 1–12（默认 4），已有职业时覆盖提示「会删除全部现有职业，并清除角色已挂的职业、阶段与阶段历史」；角色步骤在项目已有职业时提示会按名挂接。`WizardGenerator` 用 `parseWizardWarnings` 取 `done.result.warnings`：`showResult`（向导页）时以 warning `Alert` 列出，弹窗模式（完成即关闭）用 `modal.warning` 展示，不丢失。
- `CardImportModal`：预览有职业时显示职业表（新建 / 复用同名职业、复用时追加到阶梯末尾的阶段）；结果显示新建 / 复用职业数、追加阶段数与 `careers_assigned`。

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

### 章节编辑器（`src/pages/project/ChapterEditorPage.tsx`）

三层结构：`ChapterEditorPage`（解析 `:cid`，渲染 `<ChapterLoader key={chapterId} …/>`，使切换章节时重建全部状态）→ `ChapterLoader`（加载章节 + 同级章节列表）→ `ChapterEditor`。必须保留的行为：

1. **脏状态跟踪**：`const dirty = content !== chapter.content || title !== chapter.title`；`applyServerChapter(c)` 根据服务端响应重置 `chapter`、`title`、`content`。
2. **AI 生成/润色走后台任务，开始前自动保存**（`10-23-task-engine`）：
   ```ts
   const runAI = async (mode: 'generate' | 'polish') => {
     if (!(await ensureSaved())) return
     setStarting(true)
     const created = await (mode === 'generate' ? generateChapter(cid, body) : polishChapter(cid, body))  // 202 Task；失败 → message.error + reloadChapter
     setStarting(false)
     trackTask(created)                                   // useTasksStore.track：立即刷新、切到 3 秒轮询
     setChapter((p) => ({ ...p, status: 'generating' })) // 创建即抢占
     await followTask(created)                            // useTaskStream().follow
   }
   ```
   `followTask`：`succeeded` → `parseChapterDone(result)` → `replaceFromServer(chapter)`（解析失败时重新 GET 章节），`warning` 用 `message.warning`；`cancelled` → `已停止，正文保持原样` + `reloadChapter()`；`failed` / `interrupted` / `lost` → 错误 `Alert`（中断时标题为 `任务已中断`）+ `reloadChapter()`；`detached`（卸载 / 切换章节）→ 不做任何事。
   **挂载时自动接上**：仅当初始章节 `status === 'generating'` 时查询 `GET /tasks?chapter_id&active=true`，找到 `chapter.*` 任务就切到对应 Tab 并 `followTask`（通过 ref 调用最新函数）；查询完成前 `attachChecked = false`，此时不显示「卡住」提示、生成/润色按钮禁用。**其他入口新建的任务**（任务面板重试、另一个标签页）：订阅 `useTasksStore.active`，`attachChecked && !aiRunning` 时出现本章且未订阅过（`followedIdsRef`）的 `chapter.*` 活动任务即接上（有未保存修改时 `message.warning` 提示将被覆盖）。局部改写面板在 `chapter.status === 'generating'`（含查询完成前）时禁用。
3. **任务期间只读**：`aiRunning = aiTask.running || starting`（含排队）。排队中 / 尚无输出时文本域仍显示原正文，开始输出后显示 `aiTask.text`（`displayContent = aiStreaming ? aiTask.text : content`）；`readOnly={editorLocked}`；标题输入框、AI 表单、标签页和 `AIConfigSelect` 都按 `aiRunning`/`editorLocked` 禁用。排队中显示 `queueLabel(task)`（`排队中，前面还有 N 个任务`）的 info `Alert`；运行中显示 `Progress` + `stage`；断线重连时显示警告 `连接中断，正在重连…（任务仍在后台运行）`。停止按钮（排队时 `取消排队`，否则 `停止（已输出 N 字）`）调用 `aiTask.cancel()`，创建请求返回前禁用。
4. **Ctrl/Cmd+S**：只注册一次 window `keydown` 监听器；它通过 ref 调用最新的 `handleSave`（在无依赖的 effect 中执行 `saveShortcutRef.current = handleSave`），因此监听器永远不会捕获过期状态。`handleSave` 在运行/保存中不做任何事，无改动时显示 `没有需要保存的修改`。
5. **未保存离开守卫**：`const guard = dirty || rewriteLocked`——**AI 任务进行中可以直接离开**，任务在后台继续，返回编辑器自动接上（第 2 条），也可在任务面板查看；只有未保存修改或局部改写流式中 / 有待采纳结果（第 9 条）才拦截。浏览器卸载通过 `beforeunload` 监听器处理（仅在 `guard` 时挂载）；应用内导航通过 `useBlocker(({ currentLocation, nextLocation }) => guard && currentLocation.pathname !== nextLocation.pathname)` 处理，并弹出确认 `Modal`，其确定操作执行 `blocker.proceed?.()`（卸载时 `useTaskStream` 只断开订阅，不取消任务）。
6. **卡在 `generating` 的章节**：`const stuck = chapter.status === 'generating' && !aiRunning && attachChecked`（挂载时已查过且没有活动任务，例如另一窗口的任务刚创建、或后端异常）会显示一个带 `复位` 操作的警告 `Alert` → `chapterApi.update(pid, chapter.id, { status: 'draft' })`。卡住期间，保存、标记完成和 AI 按钮都被禁用，因为后端会以 409 拒绝对生成中章节的正文/标题 PATCH（`backend/app/services/chapter_service.py` 中的 `ConflictError`），且生成/润色会返回 409 `该章节正在生成中，请稍候再试`。
7. **覆盖已有正文的重新生成**通过 `modal.confirm` 询问（`重新生成正文？`）；正文为空时禁用润色。
8. 上一章/下一章导航按 `chapter_no` 再按 `id` 对同级章节排序。

出场角色（`10-23-story-entities`）：标题输入框下方的 `chapter/CastSelect`，独立保存，不影响 `dirty` 与离开守卫；AI 任务进行中禁用。

版本与局部改写（`10-23-chapter-versions`）在此基础上新增：

9. **编辑锁**：`const editorLocked = aiRunning || rewriteLocked`（`rewriteLocked` 由 `RewritePanel.onLockChange` 上报：改写流式中或有待采纳结果）。正文 `readOnly`、标题、保存、`AIConfigSelect`、生成/润色 Tab 都按 `editorLocked` 禁用；`handleSave` 在 `editorLocked` 时不做任何事。离开守卫包含 `rewriteLocked`：确认离开时 `RewritePanel` 卸载，其 `useStreamTask` 自动中止请求，预览结果丢弃（正文不受影响），弹窗文案区分「正在生成 / 有未保存的修改 / 局部改写结果未采纳」。
10. **选区跟踪**：文本域 `onSelect/onKeyUp/onMouseUp/onChange` 调 `syncSelection(el)`，状态保存 UTF-16 偏移；只有在发请求时才换算为码点（`utils/textOffset.ts`）。服务端改写正文的路径（生成完成、采纳改写、回滚）统一走 `replaceFromServer(c)`：`applyServerChapter` + 把选区记录移到正文末尾；普通保存仍用 `applyServerChapter`，不重置选区。
11. **自动保存**：`ensureSaved()`（无改动直接返回 true，否则 `save()` + `已自动保存当前修改`）被整章生成/润色、局部改写开始、回滚共用。
12. **整章生成**：请求体显式带 `auto_continue: genOptions.autoContinue`（后端默认 false，开关默认开，`utils/preferences.ts` 记住选择）；订阅收到 `continue` 事件（`aiTask.continueInfo`）时显示 `字数不足，自动续写第 n 轮` 的 info `Alert`；`done.warning` 存在时用 `message.warning` 提示（正文已保存）。重新生成确认文案提示「当前正文会自动存为历史版本」。
13. **版本历史**：标题栏「版本历史」按钮（任何时候都可打开查看）。`restoreDisabled = editorLocked || saving || stuck`。`handleRestore` = `ensureSaved` → `versionApi.restore` → `replaceFromServer` → `onChanged()` + `refreshProject()`。

章节分析与按建议重写（`10-23-analysis-memory`）在此基础上新增：

14. **分析状态**：`const analysis = useChapterAnalysis(initial.id)`（`hooks/useChapterAnalysis.ts`）。挂载时 `GET /chapters/{cid}/analysis-status`，`latest_analysis_id` 变化或每次 `refresh()` 后重新 `GET /analyses/{id}` 取最新详情（回写条目可能被其他入口修改）；`status` = 订阅中的分析任务 ? `analyzing` : 服务端状态。**自动接上**本章的 `chapter.analyze`：`useTasksStore.active` 里出现本章未订阅过的分析任务，或状态接口的 `active_task_id`（`taskApi.get` 后订阅）；结束后 `refresh()`，成功 `message.success('本章分析完成（回写：已写入 N · 待确认 M）')`、失败 `message.warning`。卸载只断开订阅。标题栏状态 `Tag`（`ANALYSIS_STATUS`，点击打开抽屉）+ 「分析」按钮（`Badge` = 最新分析中 `pending` 条目数）。以下路径之后调用 `analysis.refresh()`：保存（正文变化 → 可能 `stale`）、回滚、采纳局部改写、章节任务成功（后端 `on_success` 已在广播 `done` 前自动排队分析，刷新即可拿到 `active_task_id` 并接上）。手动分析 `runAnalyze` = `ensureSaved` → `analysis.start(aiConfigId)`（`POST /chapters/{cid}/analyze`，200/202 都返回 `Task` → `track` + 订阅）。
15. **按建议重写**与整章生成同样处理：`runRevise(body)` = `ensureSaved` → `reviseChapter(cid, {...body, ai_config_id: aiConfigId})`（202；失败 `message.error` + `reloadChapter`）→ `track` → 置 `generating` → `void followTask(created)`（创建成功即返回 true，抽屉不等待完成）。`chapter.revise` 属于 `CHAPTER_WRITE_KINDS`（`api/tasks.ts`），挂载接续 / 外部任务接续、只读、停止按钮、卡住判定全部与生成 / 润色共用；接上时 AI 助手切到「AI 生成」标签（重写没有独立标签），并显示 info `Alert`「正在按分析建议重写本章」；若当前在「局部改写」标签则切走以显示停止按钮。成功文案 `按建议重写完成，已保存`。**评分对比**：`followTask` **仅在成功**时用 `reviseCompareFor(outcome.task ?? t, 最新已知分析 id)`（`api/analysis.ts`）得出 `reviseCompare` 传给抽屉：`baseAnalysisId = TaskOut.analysis_id`（后端取自 `input.request.analysis_id`，仅 `chapter.revise` 有值），`sinceAnalysisId = max(base, 成功时已知的 latest_analysis_id)`（经 ref 读取，避免旧闭包；自动重新分析在任务成功后才入队，必然晚于它）。因此任务面板重试、其他标签页发起后被本页接上的重写同样能对比评分，不依赖本页创建时的本地记录。
16. **`?panel=analysis`**（任务面板跳转）：`open = analysisOpen || searchParams.get('panel') === 'analysis'`（派生，不用 effect 同步），关闭时 `setSearchParams(删除 panel, { replace: true })`。离开守卫只比较 pathname，不受查询串影响。


### AI 配置页（`src/pages/AIConfigPage.tsx`）

- 挂载时及每次变更后用 `load(true)`（强制）加载。
- 编辑表单的 `api_key` 始终为空；保存时仅在非空时才发送该 key（`key ? { ...body, api_key: key } : body`）——参见 [type-safety.md](./type-safety.md) 中的 PATCH 语义。
- 创建时 API key 为必填，`openai_compat`（本地 LLM）除外。
- 模型类型（`10-23-knowledge-base`）：文本 / 图片 / **向量模型**（`embedding`），选项由 `modelTypesFor(provider)` 给出——Anthropic 不显示向量模型，切换到 Anthropic 时若已选向量模型自动退回文本模型；选中向量模型时说明 `EMBEDDING_HELP`「用于知识库与剧情记忆的语义检索，不配置时使用关键词检索」。向量 / 图片模型不显示思考与上下文参数（保存时清空）；类型 Tag 颜色 `MODEL_TYPE_COLORS`（向量 cyan）。连通性测试对向量模型的 message 为「连接成功，向量维度 N」，列表以 `embeddingDimension(message)` 额外显示「维度 N」标签。
- `openai_compat` 的 Base URL 提示说明了 Docker + 宿主机 Ollama 的用法：`http://host.docker.internal:11434/v1（Docker 部署下的本机 Ollama，需部署方开启 ALLOW_PRIVATE_AI_ENDPOINTS）`。未开启该开关时，后端端点防护会拦截私有/回环地址；此时 `POST /ai-configs/{id}/test` 返回 HTTP 200，带 `ok: false` 和后端消息（`backend/app/services/ai_config_service.py` 将 `BadRequestError` 转换为 `AIConfigTestResult(ok=False, message=exc.detail, …)`）。页面显示 `message.error(\`连通失败：${r.message}\`)`，并在连通性列中显示一个带该消息的红色 `失败 · {latency_ms} ms` 标签。

---

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
- 主题定制只通过 `ConfigProvider theme`（`algorithm` 随明暗切换 + `token.colorPrimary = BRAND_PRIMARY`）；**颜色一律取 `theme.useToken()` 或 `theme/palette.ts`，不得写死**，见下文「主题与配色」。
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

- 章节列表 `ChaptersPanel`：「分析」列 = `ANALYSIS_STATUS` Tag + 整体评分（`formatScore`）；状态来自 `GET /projects/{pid}/analysis/statuses`，loader 依赖章节列表的 `data`（列表每次重载后跟着刷新；**不要**依赖 `data = []` 默认值产生的新数组，否则会无限重载），失败只影响该列。
- 项目概览 `OverviewPanel`：`AnalysisSettingsCard` 渲染在以 `updated_at` 为 key 的表单**之外**（切换开关会改 `updated_at`，放在里面会重置未保存的基本信息表单）。`自动分析` `Switch` → `PATCH /projects/{pid} {auto_analyze}` → `setProject`，旁注「每章生成后会多一次 AI 调用」；`分析全部未分析 / 已过期章节` → `POST /analysis/analyze-pending` → `已加入 N 章分析…` / `没有需要分析的章节…` + `useTasksStore.refresh()`。

### 大纲进阶（`10-23-outline-pro`）

| 组件 | 契约 |
|-----------|------------------------|
| `outlines/OutlineTaskModal` | `{ projectId; kind: 'outline.continue' \| 'outline.expand'; open; onClose; outline?（展开目标）; onSaved? }`。与 `EntityTaskModal` 同一模式（仅 `open` 时挂载、运行中允许关闭、结果组件取自 `TASK_RESULT_VIEWERS` 并包 `<Suspense>`）。续写：条数 1–20、开新卷开关 + 新卷名、发展方向；展开：总章数（最小值 = max(2, 有正文细纲数 + 1)，>10 时禁用开始）、要求，顶部说明保留与替换规则。挂载接续：续写取 `project + kind` 最新活动任务，展开在 `active` 列表里按 `TaskOut.outline_id` 匹配。 |
| `outlines/ContinueDraftResult` | `outline.continue` 结果组件（已注册）。每条草稿：勾选、`预计第 N 章`（`continuePreviewNos`，未勾选不占号）、卷号 / 卷名、标题、梗概、删除；`continueDraftError` 阻止保存；`POST /outlines/append`（`continueAppendItems`），成功后禁用。 |
| `outlines/ExpandDraftResult` | `outline.expand` 结果组件（已注册）。**重新拉取** `GET /outlines/{oid}/plans` 与角色列表：有正文的细纲置灰标「已有正文，保留」（不可删、提交带 id 全字段）；没有正文的旧细纲以 info `Alert` 列出「将被替换」（含已建空章会被删除、章节号前移的提示）；草稿可勾选 / 上下移（`moveItem`）/ 删除，字段经 `PlanFieldsEditor` 编辑，未匹配的角色名只提示。保存 `PUT /plans`（`buildExpandSync`），成功后可直接「为新细纲建章」（`POST /plans/build`）。 |
| `outlines/PlanFieldsEditor` | 细纲字段受控编辑（标题、梗概、关键事件 `Select mode=tags` ≤20、出场角色多选、情感基调 ≤100、叙事目标、预估字数 300–20000）。展开预览与大纲页行内编辑共用。 |
| `outlines/PlanList` | 大纲页展开行：细纲列表（seq、标题、`第 N 章 · 已有正文 / 空章` 可跳转或 `未建章`、字数、情感、梗概、关键事件、出场角色、目标）；**行内编辑**（同一时刻只编辑一条，`PATCH /plans/{id}`）；删除 = `removePlanSync` 整体同步（有正文的禁用；已建空章提示会连章删并前移章节号）。 |
| `outlines/OutlineImportModal` | 仅打开时挂载；`Upload.Dragger` 读文件 → `parseOutlineFileText` → `POST /outlines/import/preview` → 卷 / 章纲 / 细纲计数、无法匹配的角色、warnings → 已有大纲时选「追加到末尾 / 整体替换」（替换提示先存快照）→ 导入，结果显示计数、`snapshot_id` 与 warnings。 |
| `outlines/OutlineSnapshotDrawer` | `Drawer`（宽 640，`destroyOnHidden`）：手动保存（备注 ≤200）、列表（`#id`、`SNAPSHOT_REASON_LABELS`、时间、大纲 / 细纲 / 关联数、备注）、`详情`（章纲表 + warnings）、`恢复`（先取详情的 warnings 放进 `modal.confirm`；成功后 `modal.success` 提示恢复计数与「恢复前已存为快照 #backup.id，恢复它即可撤销」）、`删除`。 |
| `chapter/ChapterPlanCard` | 章节编辑器「AI 生成」Tab 顶部的本章细纲要点（标题、字数、情感、梗概、关键事件序号列表、叙事目标）。编辑器在 `initial.plan_id` 非空时 `GET /projects/{pid}/chapters/{cid}/plan`，拿到后显示并把目标字数预填为 `est_words`——**仅当用户尚未手动改过目标字数**（`targetWordsTouchedRef`，经 `changeGenOptions` 记录），细纲加载完成前的修改不被覆盖（读取失败静默，不影响编辑）。 |

- `OutlinesPanel`（`/projects/:pid/outlines`，**懒加载路由**，大纲相关组件都在这个 chunk 或结果组件 chunk 中；`api/outlines.ts` 不得被入口 chunk 引用）：数据 `GET /outlines/tree` + 角色列表。工具栏 `AI 续写`、`导入`、`导出`（`downloadJson(GET /outlines/export, outlineFilename(项目名))`）、`快照`、`AI 重新生成`（`WizardModal kind="outline"`，含追加 / 覆盖）、`新增大纲`；下方 1 纲 1 章批量建章（只能勾选 `canCreateSingleChapter`：未展开且未关联章节）。按卷分组（`groupOutlines`：`(volume_no, sort, chapter_no, id)`，与后端一致），列：章（`chapterRange`）、标题、梗概、状态（`expandStatus`；1 纲 1 章已建章可点击跳转）、操作（编辑、展开 / 重新展开、`canBuild` 时建章、上移 / 下移（卷内边界禁用，只改顺序不改章节号）、删除）。有细纲的行可展开显示 `PlanList`，展开状态受控保留（建章、展开采纳后自动展开该行）。会改变章节的操作后 `reload()` + `refreshProject()`。
- `WizardPage` 进度：`goTo(step)`（上一步 / 点击步骤条）只 PATCH `current`；`advance('done' | 'skip')`（下一步 / 跳过）用 `markWizardStep` 写 done / skipped + 下一步的 `current`（完成页为 `finish`）；生成成功只记 done、不改 current。回调里经 `useProjectStore.getState()` 读取最新进度（避免旧闭包覆盖），PATCH 结果回写 store；失败 `message.error`。

### 伏笔管理（`10-26-foreshadows`）

| 组件 | 契约 |
|-----------|------------------------|
| `foreshadows/ForeshadowEditDrawer` | `{ projectId; open; foreshadow: Foreshadow \| null; characters; onClose; onSaved(f) }`。`Drawer`（宽 720，`destroyOnHidden`，内部 `key = id ?? 'new'`）：标题、状态、内容、埋法 / 回收要点、四个章节号（`InputNumber`，可填尚未创建的章节；不填计划回收章 = 长线）、重要度 `Rate`、注入上下文 `Switch`、关联角色多选、备注；保存前 `foreshadowFormError`（文案与后端 400 一致），提交 `formToForeshadowInput`（章节字段一律显式给出，null = 清空）。已有伏笔下方「变更历史」（`ForeshadowHistoryList`，按 `updated_at` 重新加载，`formatForeshadowValue` 展示前 → 后）。 |
| `foreshadows/ForeshadowTimeline` | `{ items; currentChapter; onSelect? }`。**纯 SVG**（不引入图表库）：左侧 180px 标题列（点击 = `onSelect`），横轴章节（`timelineRange` / `timelineTicks` / `chapterX`，每章 16–48px，横向滚动），当前章橙色竖虚线。线段与标记只用 `timelineBars` 的结果：实际章节实线 / 实心点、计划章节虚线 / 空心点、超期段与标题红色、长线箭头延伸到当前章、已废弃灰色、已回收绿色。 |
| `foreshadows/ForeshadowConfirmModal` | `{ change: AnalysisChange \| null; onClose; onConfirmed(updated) }`。确认 `foreshadow_new`：表单初值 `newForeshadowDraft(change.after)`（标题、内容、回收要点、计划回收章、重要度、关联角色——角色列表经 `useProjectId` 加载），提交 `buildForeshadowOverrides(initial, values, { charactersTouched: form.isFieldTouched('character_ids') })`（只发改动的字段，清空计划回收章显式 null；关联角色动过即提交，含清空后的空列表），没有改动时不带请求体；409「本章已有同名伏笔」原样提示。 |
| `foreshadows/ChapterForeshadowButton` | `{ projectId; chapterId }`。编辑器工具栏「本章伏笔」（位于「分析」之后）：`Badge` = `reminderBadgeCount`（计划埋设 + 必须回收），点击 `Popover` 时重新拉 `GET /projects/{pid}/chapters/{cid}/foreshadow-reminders`，按 `reminderSections` 五档（颜色：埋设蓝 / 回收绿 / 超期红 / 即将到期橙 / 长线紫）列出，每条可跳转 `/foreshadows?id=`。 |
| `foreshadows/CharacterForeshadowsTab` | `{ projectId; character }`。角色抽屉「伏笔」Tab（职业之后，仅已有角色；`CharactersPanel` 用 `React.lazy` + `<Suspense fallback={<Spin/>}>` 按需加载，不进入口 chunk）：`GET /foreshadows?character_id=` 只读列表（状态、超期、长线、章节说明），标题跳转 `/foreshadows?id=`，底部「在伏笔页查看全部」→ `/foreshadows?character_id=`。 |

- `ForeshadowsPanel`（`/projects/:pid/foreshadows`，懒加载路由，NAV「伏笔」在「章节」之后）：顶部 `Statistic` 统计（待埋 / 已埋 / 部分回收 / 已回收 / 已废弃 / **超期（>0 时红色）** / 长线 / 当前章）；筛选栏（状态 `Select`、仅超期、仅长线、关联角色、排序 `Segmented` 按计划回收章 / 按重要度）全部作为查询参数交给后端；Tabs「列表」（`Table`：`#id 标题` + 超期 N 章红 Tag / 长线 / 不注入 / 分析来源、状态、`foreshadowChapterSummary`（超期时「收」红色）、重要度星、关联角色、操作）/「时间线」（同一份筛选结果）。操作下拉：`标记已埋…`（待埋 / 已废弃）、`标记回收…`（已埋 / 部分回收，可勾选部分回收）弹小表单选章节（默认值 `defaultPlantChapter` / `defaultResolveChapter`），`废弃`，`删除`（`Modal` 确认——Dropdown 菜单项里挂 Popconfirm 会随菜单关闭消失）。每次变更后同时 `reload` 列表与统计。URL `?character_id=` 预置角色筛选，`?id=` 打开该伏笔的编辑抽屉并从 URL 去掉 `id`。

### 批量顺序生成（`10-26-batch-generate`）

| 组件 | 契约 |
|-----------|------------------------|
| `batch/BatchGenerateModal` | `{ projectId; chapters: ChapterBrief[]; onClose; onStarted(task) }`，`ChaptersPanel` 用 `React.lazy` 且只在打开时挂载。范围：起止章 `Select`（默认 `defaultBatchRange`：第一个未写章节 → 最后一章）+「生成到第 N 章」（`endIdUpTo`）；选项：统一字数 300–20000、审核间隔 0–50、使用细纲字数、包含已写、自动续写、失败时跳过继续、附加要求 ≤2000、`AIConfigSelect`。范围 / 字数相关字段变化后 400ms 防抖 `POST /batch-generate/preview`（结果按请求键保存，键变化即失效，**不在 effect 里同步 setState**）：将生成 N 章、跳过已写数、`formatEta(estimated_minutes)`、是否分析、warnings、章节清单（目标字数与来源、`重写` 标签）。起始晚于结束 / 预览 0 章 / 没有模型配置时禁用确定。成功 → `track(task)`。 |
| `batch/BatchBanner` | `{ task; onChaptersChanged; onDismiss }`，`ChaptersPanel` 懒加载、`key = task.id`。`batchBannerState`：running（排队 / 运行中）→ `useTaskStream().follow(task, {onSnapshot, onChapterStart, onChapterEnd})`，进度条、阶段 / 排队文案、当前章、`取消`（Popconfirm）；`chapter_start` / `chapter_end` 与结束时调用 `onChaptersChanged`（刷新章节列表与项目字数），结束后 `track(outcome.task)`；本地清单 `chapter_end` 时替换对应项，缺断点时 `GET /tasks/{id}` 补齐。paused → 「已处理 x/y」+ `继续`（resume）/ `结束`（finish）；failed / interrupted → `task.error` + `从第 N 章续跑`（retry；N 由 `batchResumeIndex` 推导）+ `关闭`（仅本页隐藏）。`展开章节清单` 懒加载 `BatchProgressView`。卸载只断开订阅。 |
| `batch/BatchProgressView` | `{ items; current?: {index, phase}; text?; running?; summary? }`：汇总（`batchSummaryText` + warnings）或「已处理 x/y」、章节清单（`batchRowView`：状态 Tag、字数 / 目标、分析结果、失败原因或警告）、当前章实时输出（`StreamOutput`；分析阶段不输出）。 |
| `batch/BatchResultView` | `chapter.batch` 结果组件（已在 `taskResultViewers` 注册）：`parseBatchSummary(result)` + `batchCheckpoint(task.checkpoint)?.items`（`Task.checkpoint` 按 kind 区分，读批量断点一律经 `batchCheckpoint` 收窄）。`TaskResultViewerProps` 新增可选 `task`（`TaskResultModal` 传 `stream.task ?? task`），`TaskResultModal` 对 `paused` 也展示结果。 |

- `ChaptersPanel`：工具栏 `批量生成`（无章节或已有未结束批量时禁用）；横幅任务 = store `active` 中本项目的 `chapter.batch`（含 paused），否则取 `GET /tasks?project_id&kind=chapter.batch&limit=1` 中 failed / interrupted 的那个（依赖 store `version` 重新加载）；状态列在 `queuedBatchChapterIds(openBatch)` 中且未生成中的行加 `排队批量` Tag。`ChaptersPanel` 在入口 chunk 中：只能静态 import `utils/batchState.ts`，横幅 / 弹窗 / 清单 / `utils/batch.ts` 都按需加载。
- `TaskPanel`：paused 行显示 `待审核` 标签与 `继续` / `结束` / `取消`（不显示删除 / 重试），`查看结果` 对 succeeded 或 paused 且已注册结果组件的任务显示；批量取消的确认文案为「已完成的章节保留，正在写的章节恢复原状态」。
- 章节编辑器：批量任务没有 `chapter_id`，以 store 中活动批量的 `checkpoint.current.chapter_id === 本章` 识别「批量正在写本章」——显示提示、不显示复位（不算 stuck），批量离开本章后 `GET` 章节并 `replaceFromServer`。


### 提示词模板与写作风格（`10-23-prompt-styles`）

后端契约见 [../backend/prompts-styles.md](../backend/prompts-styles.md)。入口：顶栏菜单「提示词模板」「写作风格」（在「模型配置」之后）+ 用户下拉菜单同名两项；两页都是 `router.tsx` 的 `lazyPage` 懒加载路由。

| 组件 | 契约 |
|-----------|------------------------|
| `pages/PromptTemplatesPage` | `/prompt-templates[?key=]`。左侧 `Menu`（`groupTemplates` 按分类分组，`templateBadges`：已覆盖 / 已停用 / 默认已更新），右侧 `PromptEditor key={key}`；编辑器有未保存修改时切换模板先 `modal.confirm`。工具栏 `导入`（`JsonImportButton`，1MB 紧凑口径）→ `PromptImportResultModal` 逐项结果（`PROMPT_IMPORT_STATUS` + `formatIssue` 出错位置）、`导出我的覆盖`（`downloadJson` + `datedFilename`）。 |
| `prompts/PromptEditor` | `{ templateKey; onChanged; onDirtyChange }`。系统段 / 用户段两个等宽 `TextArea`（`id=prompt-part-{part}`；默认系统段为空且未填写时只显示用户段）；光标位置用 `onSelect/onKeyUp/onMouseUp` 记入 state（**不用 ref**：react-hooks/refs 规则禁止在渲染期函数里传 ref），变量表点击变量名插入 `{{ name }}` 到最近聚焦段的光标处。600ms 防抖 `POST /preview`（AbortController），返回的 `errors` 即校验问题；保存 400 用 `issuesFromError(err)` 读 `ApiError.body.errors`。问题按段列在编辑框下（`issuesForPart`），点击 → `lineRange` 选中该行。顶部：覆盖启停 `Switch`、`与系统默认对比`（`PromptDiffModal`）、`历史版本`（`PromptVersionDrawer`）、`恢复默认`（Popconfirm：删除覆盖及历史）。`outdated` 时 warning `Alert`：`查看三方对比`（`PromptThreeWayModal`：旧默认 = `override.base_*` / 新默认 / 编辑器内容，外加「默认的变化」「我的改动」两份 diff）+ `确认已阅`（`PATCH {ack_default}`）。保存 / 启停 / 确认不重置编辑框；回滚 / 恢复默认用服务端内容替换。 |
| `prompts/PromptPreviewPane` | `{ preview; loading; error }`。逐条 message（角色 + 字数），内容经 `splitLocked(content, preview.locked)` 切分，锁定段灰底、`锁定：{label}` 标签，只读展示。 |
| `prompts/PromptDiffModal` / `PromptThreeWayModal` / `PromptVersionDrawer` | diff 一律复用 `TextDiff`（jsdiff 逐字，按需加载）。版本抽屉：时间 / 备注 / `对比当前` / `回滚`（`modal.confirm`，回滚会重新校验）；`key = override.updated_at`，保存或回滚后重新拉取。 |
| `pages/StylesPage` | `/styles`。`useStyles()`（`stores/styles.ts`）读列表，`groupStyles` 分「我的风格」「预设风格」两组卡片（描述 + 前 3 个非空维度、`使用中` 标签 tooltip 引用数）。预设：查看 / 复制 / 试写；我的：编辑 / 复制 / 试写 / 删除。删除：确认框显示 `referencesText(style.references)`，有引用时以 `force=true` 删除；仍收到 409 时用 `readStyleReferences` 按最新引用数再确认。工具栏：从样文提取、导入（`summarizeStyleImport` + 改名明细）、导出我的风格、新建。任何变更后 `useStyleStore.load(true)`。 |
| `styles/StyleFormFields` | Form 内的字段组：名称、一句话描述（tooltip：向导与设定生成只带这一句）、6 个维度用 `AutoComplete`（`STYLE_DIMENSIONS` 常用选项 + 自由填写）、禁用表达、示例段落（`sampleLengthHint` 码点字数 / 2000）、自由补充；长度规则与 `STYLE_LIMITS` 一致。 |
| `styles/StyleEditDrawer` | `{ open; style \| null; initialDraft?; onClose; onSaved; onCopy?; onTrial? }`。预设只读 + 复制按钮；被引用时提示影响范围；`试写一段` 用表单当前值（未保存也生效）打开试写。提交只用 `draftToStyleBody`。 |
| `styles/StyleExtractModal` | 样文 200–10000 码点（本地预检）+ `AIConfigSelect` → `extractStyle`（同步 SSE，`useStreamTask`，可停止）→ warnings + 可编辑草稿 → `保存为新风格`（`styleApi.create`）。提取本身不落库。 |
| `styles/StyleTrialDrawer` | `{ target: {kind:'saved', id, name} \| {kind:'draft', draft} \| null }`。场景 ≤500、字数 200–1000、模型 → `trialStyle` 流式输出（`StreamOutput`），停止按钮；不写入章节。 |
| `styles/StyleSelect` | 设定项下拉（项目默认 / 本章风格）：`styleAssignOptions` 分「我的 / 预设」，清空 = `null`，`placeholder` 说明空值含义。 |
| `styles/StyleChoiceSelect` | 生成时的临时选择：`styleChoiceOptions(styles, context, inheritLabel?)` = 继承（文案 `describeInherit`：本章风格「X」/ 项目默认「X」/ 不用风格）+ 不用风格 + 具体风格。 |
| `chapter/ChapterStyleSetting` | 编辑器「出场角色」下方「本章风格」：`StyleSelect`（placeholder `继承项目默认（X）`），变更即 `PATCH {style_id}`，只把响应的 `style_id` 合并进章节状态（不回写正文、不影响 dirty）；AI 任务进行中禁用。 |

- **接入**：`OverviewPanel` / 新建项目弹窗的「写作风格」文本框改为「默认写作风格」`StyleSelect`（发送 `default_style_id`，显式 null = 取消）+「去风格库管理」链接；PATCH 响应带 `notice` 时 `message.info`。前端**不再读写 `project.style`**。
- 章节编辑器 AI 助手在「AI 配置」下方新增「本次写作风格」`StyleChoiceSelect`（一个 `styleChoice` 状态，`context = {chapterStyleId: chapter.style_id, projectDefault: project.default_style}`），生成 / 润色 / 局部改写（`RewritePanel.styleChoice`）/ 按建议重写（`AnalysisDrawer.reviseStyle` 渲染在重写表单里，同一状态）请求都带 `style`。
- `BatchGenerateModal`：`style` 字段（默认 `inherit`，文案「按章节或项目默认（项目默认「X」）」/ 不用风格 / 指定）；不参与预览键。

### 灵感模式与立项结构化字段（`10-23-inspiration`）

后端契约见 [../backend/inspiration.md](../backend/inspiration.md)。入口：项目列表页「新建项目」左侧「灵感模式」按钮（新建项目弹窗底部也有链接）、项目概览「用灵感模式补全」（`POST /inspiration/sessions/from-project/{pid}`，同项目已有进行中的会话时打开它；`api/inspiration.ts` 在点击时 `import()`，不进入口 chunk）。两个页面都是 `router.tsx` 的 `lazyPage` 懒加载路由；`components/inspiration/*` 只被会话页引用。

| 组件 | 契约 |
|-----------|------------------------|
| `pages/InspirationListPage` | `/inspiration`。`Table`：标题、状态（`SESSION_STATUS`）、进度（`done_steps/total_steps` + 当前步骤 + `N 步可能需要更新`）、项目链接（转为的项目 / 补全自项目）、更新时间、继续 / 删除（`modal.confirm`）。「新建」弹窗输入想法（≤2000）→ `POST /sessions` → 跳到会话页。 |
| `pages/InspirationSessionPage` | `/inspiration/:sid`。加载后渲染 `SessionView key={id}`（会话放组件 state，所有子组件用响应里的最新会话 `onSession` 覆盖）。**当前步骤以本地 state 为准**，切换时 fire-and-forget `PATCH {current}`（避免其他请求的响应把它改回旧值）。左侧 `Steps`（`INSPIRATION_FLOW` 9 步，`stepDisplayStatus` → `STEP_DISPLAY` 标签：已定 / 已跳过 / 可能需要更新 / 待定）、中间当前步骤、右侧 `DraftSidebar`。生成中禁止切换步骤（`onRunningChange`）。 |
| `inspiration/OptionStep` | 6 个 AI 步骤共用，`key={step}`（切换即卸载，`useStreamTask` 卸载时中止，后端不保存半截选项）。`generateOptions`（`runGeneration`，同步 SSE）：生成中 `StreamOutput` 显示原文，`retry` 事件 → `stream.reset()` + `输出格式不对，正在重试（第 n 次）`；done 用 `session` 覆盖本地并清空原文。按钮：`shouldSuggestGenerate`（待定且没生成过）时为主按钮「让 AI 给出选项」，否则「换一批」/「生成选项」——**补全场景已定的步骤不主动建议生成**，但手动换一批始终可用（后端不拦截）。意见输入（≤500，有轮次或已定时显示）。最新一轮为选项卡片（`OptionCard`：选这个 / 编辑后选，编辑后与原文不同时 `source=edited`），历史轮次 `Collapse`。类型标签：`CheckableTag` 多选 + `Select mode=tags`（自定义，`normalizeTags` / `tagsError`），`tagsSource` 推导来源。「自己填」（`source=manual`，长度 `STEP_VALUE_LIMITS`）、「跳过这一步」、已定时「改回待定」（clear）、stale 时 `staleText` +「保持不变」（keep）。decide / skip / keep 后 `nextFlowStep`（之后第一个待定或可能需要更新的步骤，都处理过进入规模）。 |
| `inspiration/IdeaStep` | 想法（≤2000）；修改且已有已定步骤时 `modal.confirm`「已定的 N 个步骤会标为可能需要更新」→ `PATCH {idea}`。 |
| `inspiration/ScaleFields` | 规模一步与确认一步共用的 Form 字段：目标总字数变化（400ms 防抖，挂载时的初始值不触发）→ `GET /scale-suggest` 覆盖卷数 / 每卷章数 / 角色数（失败退回本地 `suggestScale`）；`extra` 即时显示本地建议。范围同 `WizardRequest`（1–20 / 1–100 / 3–30）。初始值 `initialScaleValues(state.scale)`（没有时按 30 万字建议）。 |
| `inspiration/ScaleStep` | `PATCH {scale: toScale(values)}` → 确认。 |
| `inspiration/ConfirmStep` | 汇总（`draftSummary`，stale 带标签 + 顶部 `staleSummary` 警告，不阻止确认）；没定书名时显示书名输入（`title_override`）；向导参数 `ScaleFields`。「创建项目并进入向导」→ `POST /convert {title_override, scale}` → `navigate(/projects/{id}/wizard)`（已转换且项目仍在时后端 409，页面显示「已转为项目」提示与链接）。「应用到已有项目」先把确认页规模 `PATCH` 回会话（对比里的目标总字数取自会话）再打开 `ApplyToProjectModal`。 |
| `inspiration/ApplyToProjectModal` | 仅 `open` 时挂载。选项目（默认会话来源项目 / 已转为的项目）→ `GET /apply-preview` → 表格：勾选、字段（值相同标「相同」并禁用勾选）、项目当前（`applyValueText`，空显示「（空）」）、灵感会话；默认勾选 `defaultApplyFields`（后端 `default_checked`，即项目该字段为空）→ `POST /apply {project_id, fields}`。 |
| `inspiration/DraftSidebar` | 立项草稿（题材、6 个步骤的状态与值，点击标题跳到对应步骤，规模 `scaleText`）+ 补充想法列表 / 删除 / 添加（≤200、最多 20 条，不触发 stale）。 |
| `ProjectInfoFields` | 6 个结构化字段的 Form.Item 组（项目概览「基本信息」与新建项目弹窗「更多信息」`Collapse` 共用）：类型标签 `Select mode=tags`（`open={false}`、`,，、` 分隔，`normalize` = `normalizeTags`，validator = `tagsError`）、主题 ≤200、简介 ≤2000、核心卖点 ≤500、目标读者 ≤100、目标总字数 `InputNumber` 1000–10,000,000。提交统一经 `projectInfoBody`（trim、标签规范化、目标字数缺省发 `null` = 清空）。新建弹窗里「更多信息」未展开时字段未挂载，按空值发送。 |

- 项目概览「总字数」卡片：有 `target_words` 时后缀 `/ 30 万字`（`formatWordCount`）+ `Progress`（`wordsProgress`，取整、≤100），否则提示「设置目标总字数后显示写作进度」。项目列表卡片在题材后显示前 3 个类型标签。
- `WizardGenerator`：表单初始值优先取 `project.wizard_progress.params`（store 中的当前项目，`useState` 初始化一次），没有时仍为 2 卷 × 10 章、5 个角色。
- 纯函数在 `utils/inspiration.ts`（步骤顺序 / 标签、状态推导、stale 文案、`nextFlowStep`、`suggestScale`（`roundHalfEven` 对齐 Python `round`）、应用对比默认勾选与值展示）与 `utils/projectFields.ts`（入口可用：标签规范化、进度、请求体），测试 `utils/inspiration.test.ts`（规模建议与后端 `test_suggest_scale_boundaries` 同一组用例）、`utils/projectFields.test.ts`、`api/inspiration.test.ts`（路径、retry → done 事件顺序、error → `StreamError`）。
- 包体积（本任务后）：入口 ≈ 143 kB（+≈4 kB：`ProjectInfoFields` 与 `utils/projectFields.ts`）；`InspirationSessionPage-*.js` ≈ 23 kB、`InspirationListPage-*.js` ≈ 4 kB、`inspiration-*.js`（`utils/inspiration.ts`，两页共享）≈ 4 kB、`api/inspiration.ts` ≈ 1.5 kB 按需加载。**规则**：入口 chunk 中的模块不得静态 import `api/inspiration.ts`、`utils/inspiration.ts` 或 `components/inspiration/*`。

### 知识库与剧情记忆（`10-23-knowledge-base`）

后端契约见 [../backend/retrieval.md](../backend/retrieval.md)。入口：项目侧边栏 NAV「知识库」「剧情记忆」（在「伏笔」之后），两页都是 `router.tsx` 的 `lazyPage` 懒加载路由（`/projects/:pid/knowledge`、`/projects/:pid/memories`）；`components/kb/*`、`api/kb.ts`、`api/memories.ts`、`utils/kb.ts`、`hooks/useIndexStatus.ts` 只被这两页（与编辑器的注入预览）引用，不进入口 chunk。

| 组件 | 契约 |
|-----------|------------------------|
| `pages/project/KnowledgePanel` | 左：`Upload.Dragger`（`multiple`、`accept = KB_ACCEPT`、`beforeUpload` 返回 false）逐个 `precheckUpload(file, 已有数 + 本批已接受数)`（扩展名 / ≤20MB / 非空 / <200 个，文案与后端一致）→ `kbApi.upload`（multipart）→ `track(result.task)`；**解析在上传请求内同步完成**，格式错误 / 扫描版 PDF 直接 400 `message.error`；重复上传 409 用 `message.warning`（`duplicateDocumentId(err)` 可读出已有文档 id）。文档表：文件名 + 解析 warnings 图标（tooltip）、大小 / 字数、状态 Tag（`KB_DOC_STATUS`：排队中 / 解析中（未使用）/ 建索引中 / 就绪 / **仅关键词** / 失败，tooltip `docStatusHint`，失败显示后端 error）、块数、「参与注入」`Switch`（`PATCH {inject}`，用响应替换该行）、上传时间、`查看分块`（`KbChunksDrawer`）/ `重建索引`（Popconfirm → `reindex` → `track`）/ `删除`（Popconfirm）——处理中的文档禁用重建与删除（后端也会 409）。右：`RetrievalSettingsCard scope="kb"` + `KbSearchDebug`。顶部 `IndexStatusBanner scope="kb"`；关闭自动注入时另有 info 提示。**轮询**：`useIndexStatus(pid, {busy: 有处理中的文档, onTick: 静默刷新文档})`；文档列表在 store `version` 变化（任务结束）后静默刷新（`setData`，不闪 loading）。 |
| `pages/project/MemoriesPanel` | 筛选（章号起止 `normalizeChapterRange`、类型、来源、钉住 / 屏蔽三态、关键字 `Input.Search`）全部作为查询参数交给后端，改筛选回到第 1 页；服务端分页 50 条。列：章、类型 Tag（`MEMORY_KIND_LABELS` / `MEMORY_KIND_COLORS`）+ 来源、内容（屏蔽的删除线）+ 相关角色、重要度、钉住图标按钮（`aria-label`）、屏蔽 `Switch`（都是 `PATCH` 后替换该行）、操作（`memoryEditable`：手动记忆编辑 / 删除；自动记忆只显示「自动生成」说明）。`新增手动记忆` → `MemoryEditModal`。右：`RetrievalSettingsCard scope="memory"` + `MemorySearchDebug`；顶部 `IndexStatusBanner scope="memory"`。 |
| `kb/IndexStatusBanner` | `{ projectId; status; scope; onChanged }`。`indexBanner(status, scope)`：**建索引中**（`indexing`，info，不显示按钮）优先于**需要重建**（`needs_rebuild`，warning，带模型名与本类 / 另一类缺向量数量，按钮「重建索引」→ `kbApi.rebuild` → `track`）；没有向量模型时 info「只用关键词检索」+ 去模型配置链接；都正常时不渲染。 |
| `kb/RetrievalSettingsCard` | `{ projectId; settings; scope: 'kb' \| 'memory'; onSaved }`。同一份项目设置（`/kb/settings`），按 scope **只提交本组字段**：知识库 = 自动注入 / 召回条数 1–20 / 最低分数 0–1 / 注入字数上限 500–8000；记忆 = 召回条数 1–20 / 最低分数 / 注入字数上限 300–6000。附打分与召回范围说明。 |
| `kb/KbSearchDebug` / `kb/MemorySearchDebug` | 查询 ≤2000 + 可选临时条数 / 最低分（留空 = 按设置）；记忆另有「按第 N 章召回」（`before_chapter_no`）与「包含已屏蔽」（`include_muted`）。结果顶部 `SearchResultSummary`（`searchSummary`：混合 / 仅关键词 + 候选与命中数，后端 warnings 逐条 warning `Alert`——**候选里有块缺少当前模型向量时整次查询只用关键词**，后端以 warning 说明）；每条命中：文件名 + 标题路径（记忆为章号 + 类型 + 钉住 / 屏蔽标签）+ `ScoreTags`（总分着色、向量分（null 显示「—」）、关键词分，`formatScore` 两位小数）+ 内容。 |
| `kb/KbChunksDrawer` | `{ projectId; document \| null; onClose }`（宽 720，`destroyOnHidden`，内部 `key = document.id`）。文档 warnings、分块列表（`#seq`、标题路径、偏移、字数、内容可展开），服务端分页 20 条。 |
| `kb/MemoryEditModal` | 新增 / 编辑手动记忆：章号、内容 ≤1000、相关角色 `Select mode=tags`（项目角色名作选项，≤20）、重要度 `Rate`、钉住。自动记忆不进入此弹窗。 |
| `hooks/useIndexStatus` | `useIndexStatus(pid, {busy?, onTick?}) → {status, refresh, polling, version}`。读取放在 effect 的 promise 回调里（`refresh` 只递增 tick，满足 `react-hooks/set-state-in-effect`）；store `version` 变化时重新读取；本项目有活动 `kb.index` 任务、`status.indexing` 或 `busy` 时每 3 秒刷新并调用 `onTick`。读取失败静默（只影响横幅）。 |
| `chapter/ContextPreviewPanel` | 章节编辑器「AI 生成」Tab 内（`GenerateForm` 下方）折叠面板「本次注入的旧情节和参考资料」：**首次展开时**才 `GET /chapters/{cid}/context-preview`，标题栏刷新按钮重新读取；两块原样等宽展示（已带【相关旧情节】/【参考资料…】标题行），都为空时说明不会注入。 |

- `TaskPanel`：`kb.index` 标签「建索引」，`taskLink` → `/projects/{pid}/knowledge`（文档 / 重建 / 记忆向量三种模式都跳知识库页）。
- 包体积（本任务后）：入口 ≈ 138 kB（只多了 `kb.index` 的任务标签 / 跳转与 `api/aiConfigs.ts` 的向量模型标签）；`KnowledgePanel-*.js` ≈ 8 kB、`MemoriesPanel-*.js` ≈ 11 kB、两页共享的 `useIndexStatus-*.js`（横幅、设置卡、分数标签、`api/kb.ts`、`utils/kb.ts`）≈ 9 kB、`memories-*.js`（`api/memories.ts`，记忆页与编辑器共享）≈ 0.5 kB；`ChapterEditorPage-*.js` ≈ 56 kB。**规则**：入口 chunk 中的模块不得静态 import `api/kb.ts`、`api/memories.ts`、`utils/kb.ts` 或 `components/kb/*`。
- 纯函数在 `utils/kb.ts`（状态文案与轮询判定、上传前检查、分数 / 大小 / 检索说明、索引横幅、记忆类型标签与可编辑判定、章号范围），测试 `utils/kb.test.ts`；接口路径与 multipart 上传、409 `document_id`、记忆查询参数、`modelTypesFor` / `embeddingDimension` 在 `api/kb.test.ts`；`client.ts` 契约（含 FormData）在 `api/client.test.ts`。

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
- `App.tsx`：`ConfigProvider theme={{ algorithm: resolved === 'dark' ? darkAlgorithm : defaultAlgorithm, token: { colorPrimary: BRAND_PRIMARY } }}`；
  `theme/ThemeSync` 把 `data-theme`、`color-scheme`（原生滚动条 / 表单控件）以及 `--app-bg` / `--app-text`（取自 token）写到 `<html>`，`index.css` 只用这些变量。
- 切换：`theme/ThemeSwitch`（浅色 / 暗色 / 跟随系统三段 `Segmented`）。顶栏用 `compact onDarkBar`（只显示图标、局部暗色算法）；移动端在顶栏抽屉里显示带文字的一行。不做切换动画。
- **取色规则**：
  - antd 组件与普通 DOM：`const { token } = theme.useToken()`，常用 `colorBgContainer`、`colorFillQuaternary`（浅灰底）、`colorBorderSecondary`（分隔线）、`colorText*`、`colorPrimary` / `colorSuccess` / `colorError` / `colorWarning`、`colorSuccessBg` / `colorErrorBg`（TextDiff 增删底色）、`colorTextLightSolid`（深色顶栏上的白字）。
  - 非 antd 绘制（关系图、伏笔时间线、评分走势）：`theme/usePalette()` → `chartPalette(resolved)`，浅 / 暗两套（`roles` 节点色、`series` 评分线、`grid`、`muted` 等）。`utils/graphFilter.nodeColor(n, palette)` 第二个参数缺省为浅色调色板；`RelationGraph` 另传 `colorMode={resolved}` 给 React Flow。`api/analysis.deltaColor(delta, token)` 由调用方传入 `colorSuccess` / `colorError`。
  - 书封（`utils/shelf.coverColor`）与阅读背景（`READER_TONES`）的具体色值也集中在 `palette.ts`。
- **防回归**：`theme/noHardcodedColors.test.ts` 用 `import.meta.glob(?raw)` 扫描 `src/**/*.{ts,tsx,css}`（不含测试），匹配 `#hex`（3–8 位）、`rgb(a)(`、`hsl(a)(`。白名单只有 `theme/palette.ts`，以及行内带 `// theme-allow` 注释的行（需说明理由）。新增颜色先加到 palette 或改用 token。

### 响应式（`10-27-reader-ui`）

- 文档流里的元素不要用 `100vw` 算宽度：`100vw` 包含竖向滚动条，在有占位滚动条的浏览器（Windows、macOS 设为「始终显示滚动条」）上会横向溢出约 15px；用 `100%` 或让元素独占一行。只有 fixed 定位的 Modal / Drawer 可以用 `100vw`（打开时页面滚动已锁定）。

- 断点 768px：`hooks/useIsMobile()`（`Grid.useBreakpoint()`，`screens.md === false` 即移动端；首帧断点未算出时按桌面），窗口缩放实时更新。**禁止**直接读 `window.innerWidth`。`useModalWidth(desired)`：移动端返回 `calc(100vw - 24px)`。
- 全局兜底（`index.css`）：`body { overflow-x: hidden }`；`.ant-table-content { overflow-x: auto }`（所有表格内容超宽时表内横向滚动）；≤767px 时表头 `white-space: nowrap`（列被挤窄时整表横向滚动而不是逐字换行）、`.ant-drawer-content-wrapper { max-width: 100vw }`、非 confirm 的 `.ant-modal` 宽 `calc(100vw - 24px)`。因此一般弹窗 / 抽屉无需逐个改宽度；新写的需要精确宽度的弹窗可用 `useModalWidth`。
- `AppLayout`：移动端顶栏只留 logo、用户菜单、汉堡按钮（`aria-label="打开菜单"`），导航菜单与主题切换在右侧 `Drawer`。`ProjectLayout`：移动端不渲染 `Sider`，内容区顶部「项目菜单 · 当前页」按钮打开左侧抽屉，选中菜单项后关闭；内边距 12。
- 完整适配的页面：书架（每行两列）、概览（表单 / 统计单列，`Descriptions column={{ xs: 1, sm: 2 }}`）、章节列表（移动端改为 `List` 卡片：章号 + 标题、状态 / 排队 / 分析标签、字数与时间，「⋮」菜单内编辑 / 阅读 / 删除（`modal.confirm`））、章节编辑器（工具栏只在正文卡片宽度 ≥ `TOOLBAR_IN_HEADER_MIN_WIDTH`（920px，`useElementWidth` 实测，≈ 原 `xxl` 且停靠面板关闭）时放进卡片标题栏，其余宽度放正文上方；移动端 AI 助手卡片收进底部 `Drawer`（85vh，`forceRender`，关闭不丢局部改写状态），由 `FloatButton`（`RobotOutlined`，在任务面板按钮上方，任务运行中带红点）打开）、阅读器、灵感会话（移动端单栏：步骤条横向滚动 `responsive={false}`，「立项草稿与补充想法」按钮打开底部抽屉，抽屉内跳步骤会关闭抽屉）。其余页面只保证无页面级横向溢出。
- **项目页内容区宽度 ≠ 视口宽度**：右侧停靠面板（默认 400px，可调 320–720）打开时内容区会变窄，所以 `ProjectLayout` 下的页面**不要**用 `<Col xs md xl>` / `Grid.useBreakpoint()` 这类按视口断点的分列或判断：
  - 卡片 / 统计网格用 `AutoGrid`。
  - 两栏 / 多栏用 `utils/layout.ts`：`splitRow`（flex 换行、`gap: 16`）+ `splitPane(basis)`（`flex: basis 1 basis px`，`minWidth: 0`，剩余空间按基准宽度等比分配，比例与原 `Col` 一致），各栏基准之和放不下就上下堆叠。现用：组织树 / 详情 240 / 480，记忆与知识库 600 / 300，章节编辑器正文 / AI 助手 640 / 264，概览统计 200 / 200 / 400；概览表单行用 `{ ...splitRow, rowGap: 0 }`，题材 + 状态作为一组（360，内含 172 / 172），窄时书名独占一行、二者并排。
  - 需要按宽度切换内容（而不只是换行）时用 `hooks/useElementWidth()`（ResizeObserver）量容器实际宽度。
  - 窄卡片的标题用 `styles={{ title: { whiteSpace: 'normal' } }}` 允许换行，名字和标签不被省略号截断。
  - 弹窗 / 抽屉内的表单分栏宽度固定，不受停靠面板影响，可继续用 `Col span`。
- 验收：Playwright 在 375px 下对一级页面断言 `document.documentElement.scrollWidth <= clientWidth`。

### 拆书导入（`pages/BookImportPage.tsx`，`10-23-book-import`）

- 路由 `/book-import`（`AppLayout` 下，`lazyPage` 懒加载，不进入口 chunk）；入口是书架「新建」卡的「拆书导入」按钮。当前解析 id 存在查询串 `?id=`，刷新后自动加载并进入校正；记录不存在 / 他人 → 错误 Alert 并清掉 id。
- 四步（AntD `Steps`，`responsive={false}`，外层 `overflowX: auto` + 步骤条 `minWidth: 360`，手机上横向滚动而不是撑出页面）：上传 → 校正 → 选项与预估 → 开始。能否进入某一步只用 `utils/bookImport.ts` 的 `canEnterStep` / `resolveStep`（前置条件不满足时退回能到的一步，不在 effect 里同步 setState）。
- `bookImport/UploadStep`：`Upload.Dragger`（`beforeUpload` 返回 false，`precheckBookFile` 先查扩展名 / 空文件 / 20MB / 5 个上限，后端仍是最终权威）+「未完成的解析」列表（继续 / 删除 Popconfirm / 剩余天数 `remainingDays`）。
- `bookImport/CorrectionStep`：notices `Alert`、超过 3000 章的 warning、提示汇总 Tag（点 Tag 即按该类筛选）+ 筛选 `Select`（`filterOptions`，数量为 0 的类别禁用）、「自定义正则重切」折叠面板（示例 Tag 点选、`maxLength = REGEX_MAX_LEN`、`Popconfirm` 提示会清空手工校正；「恢复自动切章」同样确认）。
  - **章节列表是虚拟列表**：AntD `Table virtual`，`scroll.y` 必须是数字（桌面 560 / 移动 480），`scroll.x` 不传（传非数字会告警），`pagination={false}`，`rowKey="index"`；单元格文字一律单行 `ellipsis`，保证行高稳定。3000 行不分页、不用 `List` 全量渲染。
    **虚拟表格里不要用 `Typography` 的 `ellipsis` + `editable` 组合**：行重渲染时省略测量会把宽度量成 0，标题只剩「...」；
    可编辑的单元格用 CSS 省略（`overflow: hidden; text-overflow: ellipsis; white-space: nowrap`，`title` 属性给全文）+ 编辑图标按钮打开弹窗。
  - 桌面：列 序号 / 分卷（卷首章蓝色 Tag，其余次要文字）/ 标题（`Typography.Text editable`，`onChange` 时去空白、与原标题不同才发 `rename`；外层 `stopPropagation`，编辑时不打开抽屉）/ 字数 / 开头一行 / 提示 Tag / 操作（`ChapterOpsMenu`）；点击行打开全文抽屉。
  - 移动端：`showHeader={false}`，单列渲染 `bookImport/ChapterCard`（序号 + 标题、卷首章卷名 Tag、字数 · 开头一行、提示 Tag），操作收进「⋮」菜单（含查看全文）；卡片不写固定宽度、`minWidth: 0` + 单行省略。
  - 操作可用性只用 `availableOps(ch, total)`（与后端 400 条件一致）；需要名称的操作（改标题 / 新开一卷 / 改卷名）走 `Modal` + `Form`（1–200 字）；删除走 `modal.confirm`。
  - `bookImport/ChapterTextDrawer`：`width = isMobile ? '100%' : 640`、`destroyOnHidden`，按 `importId + index + revision` 重新取 `chapters/{index}` 的 `lines`，带行号（`tabular-nums`）；可拆分行（`canSplitAt`：非第 1 行、非空行）悬停显示剪刀，点击后 `modal.confirm` 再发 `split`。悬停背景 / 行号颜色取 `theme.useToken()`，不写死颜色。
- **修订号**：所有带 `revision` 的写操作（ops / resplit / start）经 `withStaleRefresh` 执行；只在 `isStaleRevision(err)`（409 且 `err.body.code === 'stale_revision'`）时重新 `GET` 记录、替换本地数据并 `message.warning`，开始导入遇到它还会退回校正步骤；其他 409（已有拆书任务、解析满 5 个）按普通错误 `message.error`。**不要**匹配 detail 文案。
- `bookImport/OptionsStep`：书名（默认 `defaultOptions`：解析出的书名 → 去扩展名文件名）、主题材、导入范围 / 反推范围（`Slider range` + 两个 `InputNumber`，宽 90；`withImportRange` 让反推范围跟随 / 收进导入范围；用户没动过深度分析时按 `defaultDeepAnalyze`（≤ 100 章勾选）重算）、提取风格、深度分析、`AIConfigSelect`；`optionsError` 与后端 400 一致。预估卡片：选项合法且在第 3 步时 500ms 防抖调 `estimate`（`AbortController`，被中止的请求不写错误），说明「预估输入字数」，不换算费用。
- 开始：确认卡片（`Descriptions column={{ xs: 1, sm: 2 }}`）→ `start` → `useTasksStore.track(task)` → 跳到 `/projects/:pid/overview`。
- 任务面板：`book.import` 暂停时 `canSkipBookImport(task)`（kind 为 `book.import`、`status === 'paused'`、断点摘要 `pause_reason.reason === 'failed'`）才显示「跳过本批」（Popconfirm → `taskApi.skipBookImport` → `track`），与「继续」「结束」并列，并在行内显示 `bookImportPauseText`；取消确认文案说明已写入的内容保留。`TaskOutputModal` 对 `book.import` 显示「通读 x/y 批」（初值 `bookImportBatches(checkpoint)`，`batch_end` 事件推进）。`TaskPanel` 在入口 chunk：只能静态 import `utils/bookImportState.ts`（轻量读取），完整收窄 `bookImportSummary` 与结果组件 `bookImport/BookImportResultView`（已在 `taskResultViewers` 懒加载注册：写入统计 `statRows`、跳过项 `skippedText`、警告）都在懒加载 chunk 里。
- 测试（node 环境）：`utils/bookImport.test.ts`（上传前检查、提示筛选、操作可用性、步骤流转、深度分析默认值按 100 章、范围校验、409 只认 `code`、摘要收窄与「跳过本批」条件含 kind）、`api/bookImport.test.ts`（路径 / 方法 / 请求体、stale 409 后重新拉取、无标记的 409 原样抛出）、`components/bookImport/mobile.test.ts`（`react-dom/server` 渲染：移动端卡片、无表头、操作在菜单里）。

### 书架（`pages/ProjectListPage.tsx`，`10-27-reader-ui`）

- 顶部「我的书架」+ 统计（作品数 / 连载中（`status = writing`）/ 总字数，`shelfStats`）；工具栏：搜索（书名 + 标签，大小写不敏感）、排序（最近更新 / 创建时间 / 书名 `localeCompare zh` / 字数）、状态筛选，三者存 localStorage `ainovel.shelf`（`readShelfPrefs` / `writeShelfPrefs`，`parseShelfPrefs` 容错）。纯函数都在 `utils/shelf.ts`。
- 网格 `Col xs=12 sm=8 md=6 xl=4`（手机两列）；第一张固定「新建」卡（新建项目 / 灵感模式 / 拆书导入 / 导入备份）。
- `components/shelf/BookCover`：2:3 文字封面，`coverColor(title, resolved)` = FNV-1a 哈希 → 12 色相，浅 / 暗主题明度不同，浅色文字，对比度 < 4.5 时逐步压暗底色；左侧加深书脊；书名衬线体最多 3 行、题材在底部；`coverUrl` 有值时直接显示图片（封面任务接入点）。
- 卡片：书名、状态标签、前 3 个类型标签、`wordsProgress` 进度条（没有目标只显示字数）、「N 章 · 相对时间更新」（`utils/time.formatRelative`）。按钮：「继续写作」→ `continueTarget`（`last_edited_chapter_id` 的编辑器，否则概览）；「阅读」→ `readTarget`（有 `reading` 到该章，否则 `/projects/:id/read` 自动定位），文案 `readingLabel`（「读到第 N 章」/「开始阅读」），`canRead` 为假（没有已写章节）时禁用；「⋮」菜单：概览 / 导出（`ExportModal`）/ 备份（`BackupModal`，lazy）/ 删除（`modal.confirm`）。

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
  - 「自动执行小修改」`Switch` 通过 PATCH `auto_execute` 存在服务端，本轮未结束时禁用。下方文案说明规则：每轮首次不超过 3 项小修改直接执行，删除、后续修改和启动任务仍需确认。
  - 输入区：`AIConfigSelect`；`TextArea` 最多 8000 字（按码点计）。Enter 发送，Shift+Enter 换行，输入法组合中（`isComposing`）不发送。`busy`、有未结束轮次或流在运行时禁止发送。输入框上方显示当前附带的页面上下文（章节、大纲、角色，以及选区字数）。
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
  - 「撤销」只在 `canUndoChange` 为真时显示，即 `status === 'executed' && kind !== 'task'`；点击前先 Popconfirm，说明之后被修改过会提示冲突。
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
  - `components/agent/agentUI.test.ts`：用 `react-dom/server` 渲染，覆盖各状态下的按钮、安全的 markdown、移动端全宽抽屉、桌面键盘调宽，以及折叠时不挂载面板。
