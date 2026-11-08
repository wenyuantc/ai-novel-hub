<!-- 自动摘录自 .trellis/spec/frontend/component-guidelines.md；逐行保留原文，使用时按行号回查完整规范。 -->

<!-- .trellis/spec/frontend/component-guidelines.md:1-56 -->
# 组件规范

## RBAC、角色与配额

管理路由统一RequirePermission(permission)/superOnly，菜单、移动导航、账户及面包屑逐域能力一致；旧RequireAdmin不用于新管理路由。实际字段、33动作、撤权、配额三态与错误见[RBAC跨层契约](../backend/rbac-quotas.md)。

AdminRolesPage仅超管，桌面Table/375px List卡片共享操作；名称100/说明500与后端一致，内置可复制不可改删，在用角色不可删；编辑影响全部成员，revision冲突保留草稿不重发。UserRolesButton在列表/详情整体替换多角色，清空仍有个人能力。UserQuotaPanel展示全部生命周期作品占用/真正执行并发、继承来源/有效上限/全站顶，limited0不可转为null；非超管不能改任何管理账号额度。

以下人工编辑组件契约均受projectAccess.canEdit约束，不只隐藏顶层按钮：Form外StageListEditor、挂接职业卸下/阶段变更、加入组织、伏笔及移动章节菜单、正文/标题/选角、Meta-S、版本恢复/删除均须禁用且提交前实时复核。本人功能不锁；canUseAI=false时不挂载模型/私人配置/生成日志/任务接续/阅读进度副作用。

## 管理员系统设置

`AdminSystemSettingsPage` 的六项表单/来源、逐项恢复待保存、版本冲突/dirty保护、StrictMode及本页确认框清理、公开配置30秒同步，遵守[系统设置跨层契约](../backend/system-settings.md)。不得用定期auth.init代替公开配置刷新，也不能让过期响应/离页确认框复活。

## 管理员全站任务监控

`/admin/tasks`在RequirePermission(tasks.read)下懒加载，取消另需tasks.cancel，使用页级AdminTasksController、安全详情Drawer和原因输入→再次核对/勾选确认的两阶段取消；不复用个人TaskPanel/输出/SSE。默认5秒可关轮询、hidden暂停、旧响应失效；桌面操作列固定右侧，375px页头操作上下排列、排序移入正文与任务卡片。安全标题/错误、UUID大小写、受理/真实终态及现存与累计入口差异见[任务监控跨层契约](../backend/admin-task-monitoring.md)。

## 管理员首页

`/admin/dashboard` 在RequirePermission(dashboard.read)下懒加载，三块请求独立加载/重试，保留旧值时标过期；7/30/90日、北京时间、60秒可关闭轮询及hidden暂停。趋势图必须按容器实测宽度计算SVG画布，375px轴文字不能随大viewBox缩到不可读，并提供历史数据表。入口、缺口/null、累计任务和工坊审核深链接见[管理员首页跨层契约](../backend/admin-dashboard.md)。

## 提示词工坊

- 用户级懒加载路由 `/workshop`（`pages/WorkshopPage.tsx`），`StudioNavigation` 的创作资源分组与用户菜单有入口。Tab 为「浏览」「我的分享」「审核」，审核入口需workshop.read，官方发布另需workshop.publish，审核通过/拒绝/上下架/元数据/删除分别授权；数量来自待审核列表的 `total`。
- `components/workshop/ShareToWorkshopModal` 在写作风格库的本人风格、模板页已保存的覆盖上预填分享数据，也用于条目更新与官方发布；分类、标签复用 `WorkshopMetadataFields`。模板 key 在分享已有模板和更新时锁定。
- `WorkshopItemDrawer` 展示署名、计数、正文与操作；以服务端 `is_mine` 判归属，使用 `author_name ?? '匿名'`，不得推导或显示匿名作者 id。`WorkshopItemControls` 依条目状态及 `hidden_by` 控制上架 / 下架操作，管理员可改分类标签，正文仅作者提交更新。
- 模板导入前复用 `PromptDiffModal`，`utils/workshop.ts::workshopImportPairs` 对照当前生效内容：覆盖启用才取覆盖，否则取系统默认；确认后调用导入。导入结果提示覆盖已启用且可从历史恢复；风格导入后强制刷新 styles store，刷新失败也需显示错误。
- `ReviewPanel` 展示审核快照和真实管理员署名，管理员只能调整分类和标签，通过审核不要求驳回原因；驳回必须填原因。作者可撤回 pending、修改被驳回 / 撤回提交并重交。
- 列表与详情加载沿用 `useLoader` 过期响应保护；异步写操作期间禁用冲突控件，错误以 Alert / `errorMessage` 呈现。详情渲染必须从 `item.status` 明确定义状态，不能依赖隐含全局变量。
- 契约、状态、404/403、导入版本和测试断言见 [工坊规范](../backend/workshop.md)。

> 本项目中组件的构建方式。

---

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


<!-- .trellis/spec/frontend/component-guidelines.md:161-205 -->
### 章节编辑器（`src/pages/project/ChapterEditorPage.tsx`）

三层结构：`ChapterEditorPage`（解析 `:cid`，渲染 `<ChapterLoader key={chapterId} …/>`，使切换章节时重建全部状态）→ `ChapterLoader`（加载章节 + 同级章节列表）→ `ChapterEditor`。必须保留的行为：

1. **场景草稿**：`dirty = sceneEditor.dirty || title !== chapter.title`，结构拟排序另有`structureDirty`。正文／作者摘要由`SceneDraftController`按稳定Scene身份隔离，章响应只更新章元数据，不把整章稿写入当前景；完整合同见[独立场景](../backend/scenes.md)。
2. **AI 生成/润色走后台任务，开始前自动保存**（`10-23-task-engine`）：
   ```ts
   const runAI = async (mode: 'generate' | 'polish') => {
     if (!(await ensureSaved())) return
     setStarting(true)
     const created = aiScope === 'scene'
       ? await sceneApi.generate(pid, chapter.id, current.id, body)
       : await generateChapter(chapter.id, body)  // 示例为generate；polish同样按scope分派，body含两版本
     setStarting(false)
     trackTask(created)                                   // useTasksStore.track：立即刷新、切到 3 秒轮询
     setChapter((p) => ({ ...p, status: 'generating' })) // 创建即抢占
     await followTask(created)                            // useTaskStream().follow
   }
   ```
   `followTask`成功后解析章结果并刷新场景目录，单景另核验result.scene的身份；不覆盖不同Scene或后来输入的草稿，warning单独提示。cancelled显示原稿保持，failed/interrupted/lost可见且重读状态，detached只断订阅不取消任务。新请求按目标调用sceneApi或原章API，均携当前内容／规划版本。
   **挂载与外部任务接续**：沿用活动任务查询／store订阅与attachChecked，不提前误报stuck。Task.scene_id决定稳定目标，另一景作业不能把其流接进当前景；脏稿保持并提示核对，不警告后直接覆盖。任务标题／链接携真实Scene，缺scene_id按兼容整章处理；局部预览期间仍保持编辑锁。
3. **任务期间只读**：`aiRunning = aiTask.running || starting`（含排队）。Scene文本域始终显示自己的正文草稿；`aiTask.text`只在AI助手独立只读StreamOutput展示，绝不能复用旧`displayContent=aiTask.text`。标题、AI表单和模型选择按锁禁用，保留排队／进度／stage／重连提示及停止按钮。任务按TaskOut.scene_id接回真实景，另一景任务不能替当前景接入流。
4. **Ctrl/Cmd+S**：只注册一次 window `keydown` 监听器；它通过 ref 调用最新的 `handleSave`（在无依赖的 effect 中执行 `saveShortcutRef.current = handleSave`），因此监听器永远不会捕获过期状态。`handleSave` 在运行/保存中不做任何事，无改动时显示 `没有需要保存的修改`。
5. **未保存离开守卫**：`guard = dirty || rewriteLocked || structureDirty`。后台AI可直接离开且继续运行；场景正文／摘要草稿、拟排序、局部流或待采纳结果需离开确认。beforeunload与应用内useBlocker保留；卸载仅断任务订阅，不取消后台作业。确认框随actor／项目／章／授权代次失效关闭。
6. **卡在 `generating` 的章节**：`const stuck = chapter.status === 'generating' && !aiRunning && attachChecked`（挂载时已查过且没有活动任务，例如另一窗口的任务刚创建、或后端异常）会显示一个带 `复位` 操作的警告 `Alert` → `chapterApi.update(pid, chapter.id, { status: 'draft' })`。卡住期间，保存、标记完成和 AI 按钮都被禁用，因为后端会以 409 拒绝对生成中章节的正文/标题 PATCH（`backend/app/services/chapter_service.py` 中的 `ConflictError`），且生成/润色会返回 409 `该章节正在生成中，请稍候再试`。
7. **覆盖已有正文的重新生成**通过 `modal.confirm` 询问（`重新生成正文？`）；正文为空时禁用润色。
8. 上一章/下一章导航按 `chapter_no` 再按 `id` 对同级章节排序。

出场角色（`10-23-story-entities`）：标题输入框下方的 `chapter/CastSelect`，独立保存，不影响 `dirty` 与离开守卫；AI 任务进行中禁用。

版本与局部改写（`10-23-chapter-versions`）在此基础上新增：

9. **编辑锁**：`const editorLocked = aiRunning || rewriteLocked`（`rewriteLocked` 由 `RewritePanel.onLockChange` 上报：改写流式中或有待采纳结果）。正文 `readOnly`、标题、保存、`AIConfigSelect`、生成/润色 Tab 都按 `editorLocked` 禁用；`handleSave` 在 `editorLocked` 时不做任何事。离开守卫包含 `rewriteLocked`：确认离开时 `RewritePanel` 卸载，其 `useStreamTask` 自动中止请求，预览结果丢弃（正文不受影响），弹窗文案区分「正在生成 / 有未保存的修改 / 局部改写结果未采纳」。
10. **选区跟踪**：Scene文本域的onSelect/onKeyUp/onMouseUp/onChange保存本景UTF-16位置，发请求才转码点；拆分也用同一转换。局部done绑定Scene／章／两版本／log，按预览范围采纳，不拿最新版本自动重签。切换Scene重置选区；后续局部操作重新核对可见选区，不假定替换后旧范围自动扩成新全文。
11. **自动保存**：`ensureSaved()`（无改动直接返回 true，否则 `save()` + `已自动保存当前修改`）被整章生成/润色、局部改写开始、回滚共用。
12. **整章生成**：请求体显式带 `auto_continue: genOptions.autoContinue`（后端默认 false，开关默认开，`utils/preferences.ts` 记住选择）；订阅收到 `continue` 事件（`aiTask.continueInfo`）时显示 `字数不足，自动续写第 n 轮` 的 info `Alert`；`done.warning` 存在时用 `message.warning` 提示（正文已保存）。重新生成确认文案提示「当前正文会自动存为历史版本」。
13. **版本历史**：标题栏可查看历史，写操作仍受editorLocked/saving/stuck限制。`SceneVersionActions`先预检两稿／结构／保留引用；新结构恢复带内容版本，多景旧纯文本只提供签名差异预览与明确提取到单景，不伪装整章恢复。409保留输入／选择，核对最新后显式重试。

章节分析与按建议重写（`10-23-analysis-memory`）在此基础上新增：

14. **分析状态**：`const analysis = useChapterAnalysis(initial.id)`（`hooks/useChapterAnalysis.ts`）。挂载时 `GET /chapters/{cid}/analysis-status`，`latest_analysis_id` 变化或每次 `refresh()` 后重新 `GET /analyses/{id}` 取最新详情（回写条目可能被其他入口修改）；`status` = 订阅中的分析任务 ? `analyzing` : 服务端状态。**自动接上**本章的 `chapter.analyze`：`useTasksStore.active` 里出现本章未订阅过的分析任务，或状态接口的 `active_task_id`（`taskApi.get` 后订阅）；结束后 `refresh()`，成功 `message.success('本章分析完成（回写：已写入 N · 待确认 M）')`、失败 `message.warning`。卸载只断开订阅。标题栏状态 `Tag`（`ANALYSIS_STATUS`，点击打开抽屉）+ 「分析」按钮（`Badge` = 最新分析中 `pending` 条目数）。以下路径之后调用 `analysis.refresh()`：保存（正文变化 → 可能 `stale`）、回滚、采纳局部改写、章节任务成功（仅整章成功的 `on_success` 在done前按原开关排队一次分析；单Scene成功只刷新过期状态，不自动分析）。手动分析 `runAnalyze` = `ensureSaved` → `analysis.start(aiConfigId)`（`POST /chapters/{cid}/analyze`，200/202 都返回 `Task` → `track` + 订阅）。
15. **按建议重写**与整章生成同样处理：`runRevise(body)` = `ensureSaved` → 按aiScope调用`sceneApi.revise(pid,cid,sid,{...body,...两版本})`或原`reviseChapter(cid,body)`（202；失败 `message.error` + `reloadChapter`）→ `track` → 置 `generating` → `void followTask(created)`（创建成功即返回 true，抽屉不等待完成）。`chapter.revise` 属于 `CHAPTER_WRITE_KINDS`（`api/tasks.ts`），挂载接续 / 外部任务接续、只读、停止按钮、卡住判定全部与生成 / 润色共用；接上时 AI 助手切到「AI 生成」标签（重写没有独立标签），并显示按建议重写提示；若当前在「局部改写」标签则切走以显示停止按钮。抽屉必须展示当前选择的稳定Scene／整章范围，运行提示只取真实Task.scene_id，不能借表单当前范围扩大目标。字数明确为可选总量：单Scene／多景章留空按各景原字数，旧章目标仅一景留空下限300，显式总量按有正文景分配、保留空景。**评分对比**：`followTask`仅在成功时用`reviseCompareFor(outcome.task ?? t, 最新已知分析id)`（`api/analysis.ts`）；显式Scene任务返回null并清旧compare，只刷新章分析过期状态，不虚等全章自动分析。整章或旧缺省scene_id才派生`baseAnalysisId=TaskOut.analysis_id`及`sinceAnalysisId=max(base, 成功时已知latest_analysis_id)`，保留任务重试／其他页接续的评分合同。只有真实分析任务在运行时写“正在重新分析”，尚未取得新分析则提示等待或手动重跑；另一景任务完成同样刷新章分析／作品状态但保留当前景草稿。
16. **`?panel=analysis`**（任务面板跳转）：`open = analysisOpen || searchParams.get('panel') === 'analysis'`（派生，不用 effect 同步），关闭时 `setSearchParams(删除 panel, { replace: true })`。离开守卫只比较 pathname，不受查询串影响。



<!-- .trellis/spec/frontend/component-guidelines.md:216-252 -->
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


<!-- .trellis/spec/frontend/component-guidelines.md:286-299 -->
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
