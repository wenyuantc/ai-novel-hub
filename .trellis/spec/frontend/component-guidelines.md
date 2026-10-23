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
| `WizardGenerator<K extends WizardKind>` | `{ projectId; kind: K; existingCount?; showResult?; onDone?(items: WizardItemMap[K][]); onRunningChange?(running) }`。走**后台任务**：`runWizard(...)` 返回 202 `Task` → `useTasksStore.track(task)` → `useTaskStream().follow(task)`；完成时用 `parseWizardDone(kind, result)` 取 items。**挂载时**按 `GET /tasks?project_id&kind=wizard.{kind}&active=true&limit=1` 查活动任务并自动接上（关闭页面 / 弹窗后再打开可继续看实时输出）。`running = 订阅中 \|\| 创建请求中`，表单禁用；`useHasAIConfig() === false` 时开始按钮禁用；运行中显示危险样式按钮（排队时为 `取消排队`，否则 `停止`），调用 `task.cancel()`（`POST /tasks/{id}/cancel`）。排队中显示 `queueLabel(task)` 的 info `Alert`，运行中显示进度条 + `stage`，断线重连时显示警告。取消时显示 `已停止生成，原有数据未改变`；失败 / 中断时 `生成失败` Alert。`onRunningChange` 通过 ref + `[running]` effect 上报。`existingCount > 0` 时显示"将会替换"的 `Alert`。`WizardPage` 的步骤初始取 `?kind=`，之后每次导航（`location.key` 变化，含跳到同一 `kind`）都在渲染期按 `kind` 切换步骤。 |
| `WizardModal<K>` | 将 `WizardGenerator` 包在 `Modal` 中（`maskClosable={false}`、`destroyOnHidden`、`footer={null}`）。运行中拒绝关闭（`message.warning('正在生成，请等待完成或先停止')`）——任务本身不会因关闭而中断，但面板需要 `onDone` 回调来刷新列表；重新打开会自动接上。完成时：先 `onDone(items)` 再 `onClose()`。被 `WorldPanel`、`CharactersPanel`、`OutlinesPanel` 使用。 |
| `TaskPanel` | 无 props，挂在 `AppLayout`。挂载时 `useTasksStore.startPolling()`（卸载时停止）。右下角 `FloatButton`（`badge.count = active.length`，tooltip `后台任务`）打开 `Drawer`（宽 520）：Tab「进行中（N）」= store 的 `active`；Tab「最近」= `taskApi.list({ active: false, limit: 50 })`，依赖 store 的 `version`（任务结束 / 本地操作后自动重载）。每行：标题、`TASK_STATUS` 标签、活动任务的 `Progress`、`queueLabel` 或 `stage`、创建 / 结束时间（`formatTime`）、已结束任务的 `error`。操作：成功且 kind 在 `components/entities/taskResultViewers.ts` 注册了结果组件时显示 `查看结果`（`TaskResultModal`，见下方「设定类组件」）；`查看输出`（`TaskOutputModal`：活动任务用 `useTaskStream().follow` 实时显示，已结束读 `/tasks/{id}/output`）、`跳转`（`taskLink`：章节任务 → 编辑器；向导任务 → `/projects/{pid}/wizard?kind=…`；`character.generate` / `relations.parse` → 角色页；`organization.generate` → 组织页）、活动任务 `取消`（Popconfirm → `track(返回的 TaskOut)`）、可重试状态 `重试`（→ `track(新任务)`）、已结束 `删除`（→ `bump()`）。抽屉顶部 `刷新`、`清理已结束`（Popconfirm → `DELETE /tasks?finished=true` → `已清理 N 个已结束任务`）。 |
| `ExportModal` | `{ projectId; open; onClose }`。仅在 `open` 时渲染 `ExportModalInner`（使其 `useLoader` 每次打开都获取最新章节）。格式单选 `txt`/`md`；行选择会禁用 `generating` 状态的章节；不选则导出全部。调用 `exportProject` 并显示 `已导出：{filename}`。被 `OverviewPanel` 使用。 |
| `StoryPreview` | 具名导出 `WorldPreview`、`CharacterPreview`、`OutlinePreview`（使用 `utils/outline.ts` 中的 `groupOutlines`/`volumeLabel`）。 |
| `TextDiff` | `{ a; b; maxHeight? = 480 }`。逐字（按码点，`utils/textDiff.ts` 的 `computeDiff` → jsdiff `diffChars`，`timeout` 3 秒）对比 a → b：删除 `<del>` 标红加删除线，新增 `<ins>` 标绿；顶部显示 `+N 字 / -M 字`（非空白码点数）。超时返回 null 时显示警告 `两份文本差异过大，无法逐字对比`。 |
| `chapter/VersionDrawer` | `{ pid; chapterId; open; onClose; currentContent; currentDirty; restoreDisabled; onRestore(v) => Promise<boolean> }`。`Drawer destroyOnHidden`，每次打开重新拉取。Tab「版本」：表格（星标按钮、`v{seq}` + 最新标签、`VERSION_SOURCE` 来源标签、`updated_at`、字数、可编辑备注 ≤200、查看 / 对比当前 / 恢复 / 删除）；勾选最多 2 个（只保留最近勾选的两个）后「对比所选」按 seq 从旧到新对比；最新版本的删除按钮禁用（后端也会 400）。版本正文按 `id + updated_at` 缓存。恢复走 `modal.confirm`，确定后调用编辑器的 `onRestore`。Tab「生成记录」：`GenerationLogList`。 |
| `chapter/GenerationLogList` | `{ pid; chapterId }`。列表（时间、类型 `GENERATION_KIND_LABELS`、模型、状态 Tag（有 error 时 tooltip）、字数、耗时）；「详情」弹窗只读展示参数 JSON 与全部提示词消息；token 恒为 `-`。 |
| `chapter/RewritePanel` | `{ pid; chapterId; content; selection（UTF-16）; aiConfigId; disabled; ensureSaved; onLockChange(locked); onApplied(chapter) }`。有选区可选改写/扩写/缩写/去 AI 味/续写，无选区只能续写（光标处）；选区 >5000 码点、全空白、光标前无正文时禁用开始。开始前 `ensureSaved()`，偏移经 `utf16ToCodePoint` 换算。自带 `useStreamTask`；`locked = 流式中 \|\| 有待采纳结果`，通过 `onLockChange` 通知编辑器设为只读。完成后 `TextDiff(original, text)` + 采纳（把 `done` 的 `start/end/original/before` 原样传回，不再换算）/ 重新生成（沿用上次请求的码点偏移）/ 放弃。采纳 409 → 提示并作废预览。 |
| `chapter/CastSelect` | `{ pid; chapterId; disabled? }`。编辑器正文标题下的「出场角色」多选（`Select mode=multiple`）。`useLoader` 读 `GET /chapters/{cid}/cast`，**变更即** `PUT /cast` 整体替换（乐观更新，失败回滚并提示），与正文保存分开、不参与 `dirty`；tooltip 说明主角始终注入、梗概与上一章结尾自动匹配。AI 任务进行中禁用。 |
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
| `entities/EntityTaskModal` | `{ projectId; kind: 'character.generate' \| 'organization.generate' \| 'relations.parse'; open; onClose; characterId?; characterName?; onSaved? }`。仅 `open` 时挂载。表单（AI 配置、要求、角色定位）→ 创建 202 任务 → `track` + `useTaskStream().follow` → 成功后渲染注册表中的结果组件。挂载时按 `project_id + kind + active` 接上活动任务：生成类取最新一个；解析任务再按 `TaskOut.character_id === characterId` 匹配本角色（同一角色同时只有一个解析任务，`resource_key = relparse:{cid}`）。**运行中允许关闭**（只断开订阅，提示到任务面板查看结果）——与 `WizardModal` 不同，因为结果只是草稿、不需要刷新列表。 |
| `entities/taskResultViewers.ts` | **任务面板扩展点**：`TASK_RESULT_VIEWERS: Partial<Record<TaskKind, ComponentType<TaskResultViewerProps>>>`（`{ projectId; result: unknown; onSaved? }`）与 `hasResultViewer(kind)`。新增「只产出草稿」的任务类型只需在此注册，`TaskPanel` 不改。 |
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
- `ProjectLayout.NAV`：在「角色」后新增「组织」「关系」。`StoryPreview.CharacterPreview` 展示结构化字段（只显示非空行），`WorldPreview` 带分类标签。

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
