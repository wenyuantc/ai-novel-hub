# 场景任务规范摘录：权限、Ant Design、章节编辑器与组件生命周期

来源：.trellis/spec/frontend/component-guidelines.md

来源 SHA-256：b19616bd315b82c211b8a770c26ddd6ec28c15194ecfa189c5210f4d547a8a72

摘录日期：2026-11-07。用于避免 native context 单文件 32768 字节截断，不替代原规范。开工先核对来源是否变化；超出摘录职责时读取原文。原文相对链接以来源文件所在目录为基准。新场景行为以本任务已审阅的 PRD／design 为准，未改动的原合同仍适用。

## 原文 3–10 行

## RBAC、角色与配额

管理路由统一RequirePermission(permission)/superOnly，菜单、移动导航、账户及面包屑逐域能力一致；旧RequireAdmin不用于新管理路由。实际字段、33动作、撤权、配额三态与错误见[RBAC跨层契约](../backend/rbac-quotas.md)。

AdminRolesPage仅超管，桌面Table/375px List卡片共享操作；名称100/说明500与后端一致，内置可复制不可改删，在用角色不可删；编辑影响全部成员，revision冲突保留草稿不重发。UserRolesButton在列表/详情整体替换多角色，清空仍有个人能力。UserQuotaPanel展示全部生命周期作品占用/真正执行并发、继承来源/有效上限/全站顶，limited0不可转为null；非超管不能改任何管理账号额度。

以下人工编辑组件契约均受projectAccess.canEdit约束，不只隐藏顶层按钮：Form外StageListEditor、挂接职业卸下/阶段变更、加入组织、伏笔及移动章节菜单、正文/标题/选角、Meta-S、版本恢复/删除均须禁用且提交前实时复核。本人功能不锁；canUseAI=false时不挂载模型/私人配置/生成日志/任务接续/阅读进度副作用。



## 原文 37–60 行

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

## 组件结构

函数组件使用 default export、本地 `interface Props`，以及一行描述用途的中文 JSDoc。数据加载使用 `useLoader` 配合经 `useCallback` memo 的 loader；变更操作是带 `try/catch/finally` 的 `async` 处理函数，出错时显示 `message.error(errorMessage(err))`。



## 原文 133–139 行

### 页面模式

- **面板 CRUD**（`WorldPanel`、`CharactersPanel`、`OutlinesPanel`、`ChaptersPanel`）：`const pid = useProjectId()`；`const loader = useCallback(() => worldApi.list(pid), [pid])`；`const { data: items = [], loading, reload } = useLoader(loader)`；在 `Modal` 中使用 AntD `Form.useForm` 进行创建/编辑；用 `Popconfirm` 删除；每次变更成功后调用 `reload()`。
- **项目级数值**：在会改变字数/章节数的变更之后，调用 `useProjectStore((s) => s.refresh)`（`ChaptersPanel`、`OutlinesPanel`、章节编辑器）。`OverviewPanel` 通过 `setProject(updated)` 回写 PATCH 结果。
- **破坏性确认**使用 `modal.confirm`（`ProjectListPage` 中的项目删除、编辑器中覆盖已有正文的重新生成）或 `Popconfirm`（行删除）。
- **路由参数**：`:pid` 使用 `useProjectId()`；用 `Number.isFinite` 拦截无效 id，并渲染 `<Result status="404" …/>`。



## 原文 161–205 行

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




## 原文 216–257 行

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

### 章节分析（`10-23-analysis-memory`）

- 章节列表 `ChaptersPanel`：「分析」列 = `ANALYSIS_STATUS` Tag + 整体评分（`formatScore`）；状态来自 `GET /projects/{pid}/analysis/statuses`，loader 依赖章节列表的 `data`（列表每次重载后跟着刷新；**不要**依赖 `data = []` 默认值产生的新数组，否则会无限重载），失败只影响该列。
- 项目概览`AnalysisSettingsCard`仍在以updated_at为key的表单之外，切换不清外层草稿；整章成功后原流程分析一次，单Scene改稿只使旧分析过期。Switch仍PATCH auto_analyze；分析全部未分析／过期仍走原任务流程，不把单景稿自动分析成全章。



## 原文 569–571 行

## 设定剧情变化

角色、世界观、关系、职业和成员维护区分基础、所选章节／真实场景有效值与剧情变化；API与锚点见[演变合同](../backend/setting-progressions.md)。编辑器`SceneSettingPreview`独立展示本景开始时的设定，不依赖AI配置；ChapterPositionInput的Scene模式传真实scene_id，不退回章初。切换Scene／章／身份／作品必须废弃旧请求。


<!-- excerpt-end -->
