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
| `ExportModal` | `{ projectId; open; onClose }`。仅在 `open` 时渲染 `ExportModalInner`（使其 `useLoader` 每次打开都获取最新章节）。格式单选 `txt`/`md`；行选择会禁用 `generating` 状态的章节；不选则导出全部。调用 `exportProject` 并显示 `已导出：{filename}`。被 `OverviewPanel` 使用。 |
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
- 共享的内联样式常量定义在模块级：`const pre: CSSProperties = { whiteSpace: 'pre-wrap', marginBottom: 0 }`（`StoryPreview.tsx`）。
- 主题定制只通过 `ConfigProvider theme.token`（`colorPrimary: '#1677ff'`）。
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
| `batch/BatchResultView` | `chapter.batch` 结果组件（已在 `taskResultViewers` 注册）：`parseBatchSummary(result)` + `task.checkpoint.items`。`TaskResultViewerProps` 新增可选 `task`（`TaskResultModal` 传 `stream.task ?? task`），`TaskResultModal` 对 `paused` 也展示结果。 |

- `ChaptersPanel`：工具栏 `批量生成`（无章节或已有未结束批量时禁用）；横幅任务 = store `active` 中本项目的 `chapter.batch`（含 paused），否则取 `GET /tasks?project_id&kind=chapter.batch&limit=1` 中 failed / interrupted 的那个（依赖 store `version` 重新加载）；状态列在 `queuedBatchChapterIds(openBatch)` 中且未生成中的行加 `排队批量` Tag。`ChaptersPanel` 在入口 chunk 中：只能静态 import `utils/batchState.ts`，横幅 / 弹窗 / 清单 / `utils/batch.ts` 都按需加载。
- `TaskPanel`：paused 行显示 `待审核` 标签与 `继续` / `结束` / `取消`（不显示删除 / 重试），`查看结果` 对 succeeded 或 paused 且已注册结果组件的任务显示；批量取消的确认文案为「已完成的章节保留，正在写的章节恢复原状态」。
- 章节编辑器：批量任务没有 `chapter_id`，以 store 中活动批量的 `checkpoint.current.chapter_id === 本章` 识别「批量正在写本章」——显示提示、不显示复位（不算 stuck），批量离开本章后 `GET` 章节并 `replaceFromServer`。

