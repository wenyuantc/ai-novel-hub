# 状态管理

## 私有权限快照与撤权

User的required is_super_admin/roles/effective_permissions/authorization_revision来自服务端；禁止is_admin授权fallback。refreshAuthorization与init/公开config分离，可见轮询/回前台/管理路由进入/权限403刷新，single-flight+epoch+AbortSignal隔离旧账号。权限403不登出、不重发业务；401才clear。详情见[RBAC跨层契约](../backend/rbac-quotas.md)。

账号动作controller与useAuthorizationScope/cleanup拥有本页目标及弹窗；失写保留合法只读、失读立即移除内容。工坊Browse按actor ID+workshop.read布尔重挂，否则reload失败会留下匿名真名/隐藏条目；个人区只按actor重建，无关revision不清草稿。项目store/私人AI、风格、Skill缓存均须阻止切账号后迟到响应回填；同actor撤权还须使管理保存回调失效。

> 本项目中状态的管理方式。

---

## 概述

Zustand 5（`create<State>((set, get) => ({ … }))`）只用于最小集合的跨页面状态；其余一律使用 React `useState`；服务端数据由各页面通过 `useLoader` 获取（没有查询缓存）。不使用 Redux，除 AntD 的 `ConfigProvider`/`App` 外不使用任何 Context provider。

---

## 状态分类

| 类别 | 位置 | 示例 |
|----------|-------|----------|
| 会话（全局） | `stores/auth.ts` → `useAuthStore` | `status: 'unknown' \| 'authenticated' \| 'anonymous'`、`user`、`registrationEnabled`、`mcpEnabled`（`/auth/config`，读取失败按 true）；actions `init`、`login`、`register`、`logout`、`clear` |
| 用户级缓存（全局） | `stores/aiConfigs.ts` → `useAIConfigStore` | `configs`、`loaded`、`loading`；`load(force = false)`、`reset()` |
| 可见 Skill（全局，按需加载） | `stores/skills.ts` → `useSkillStore` / `useSkills()` | `skills`（我的 + 全局的 `SkillBrief`）、`loaded`、`loading`、`error`；`load(force = false)`、`reset()` |
| 活动任务（全局） | `stores/tasks.ts` → `useTasksStore` | `active`（排队 + 运行中/待审核，id 倒序）、`loaded`、`version`；`refresh()`、`track(task)`、`bump()`、`discardProject(id)`、`startPolling()`、`reset()` |
| 当前项目（全局） | `stores/project.ts` → `useProjectStore` | `project`、`loading`、`error`；`load(id)`、`refresh()`、`setProject(p)`、`discard(id)`、`reset()` |
| 主题（全局，设备级） | `theme/themeStore.ts` → `useThemeStore` | `mode`（`light` / `dark` / `system`，localStorage `ainovel.theme`）、`systemDark`；`setMode`、`setSystemDark`；`useResolvedTheme()`、`watchSystemTheme()` |
| 本地偏好（localStorage，非 store） | `utils/preferences.ts`、`utils/shelf.ts`、`utils/reader.ts` | `ainovel.autoContinue`；书库 `ainovel.shelf`（`{query, sort, status, view: 'shelf' \| 'list'}`；旧值缺 view 默认 shelf）；阅读设置 `ainovel.reader`（`{fontSize, lineHeight, width, background, font}`）。读写都包 try/catch，读出后经 `parse*` / `normalize*` 校验 |
| 页面数据（本地） | 页面中的 `useLoader` | 世界观/角色/大纲/章节列表、当前打开的章节、导出用章节列表；组织 / 成员 / 关系 / 关系类型 / 图谱 / 章节出场角色（各面板与抽屉 Tab 各自加载，未做全局缓存，保存后各自 `reload()`） |
| 管理员任务监控（页面实例） | `components/adminTasks/adminTasksController.ts` | 按actor ID拥有列表/详情/取消目标请求代次；5秒可见性轮询、慢请求去重，失读/401/账号/卸载清空，单独失cancel只关取消目标并保留列表，不把他人任务注入个人useTasksStore；见[跨层契约](../backend/admin-task-monitoring.md) |
| 表单 / UI 状态（本地） | `useState`、`Form.useForm` | 弹窗打开标志、`editing`、`saving`、编辑器 `title`/`content`、`aiConfigId`、AI 配置页上的测试结果 |
| 流式状态（本地） | `useStreamTask`（局部改写）/ `useTaskStream`（任务订阅） | `text`、`running`、`error`、abort controller；任务另有 `task`、`continueInfo`、`reconnecting` |

---

## 何时使用全局状态

仅当两个或以上不相关的页面需要同一份数据，**并且**该数据是用户级的：

- `useAuthStore`——由 `RequireAuth`、`LoginPage`、`AppLayout`、`App`、公开 `HomePage` 和 `HomeEntryDialog` 读取。首页只依据认证状态与 `registrationEnabled` 选择入口，不请求私有作品；能力/创作方向/FAQ及欢迎弹窗mode均为页面本地 state，次入口关闭后的焦点意图为组件ref。登录/注册标签由查询串派生，关闭注册时强制登录；切标签保留路由 `state.from`，提交时禁止模式切换。
- `useAIConfigStore`——由 `AIConfigSelect`（所有生成类 UI）、`useHasAIConfig`、`AIConfigPage` 读取。`load()` 是幂等的：`if (get().loading || (get().loaded && !force)) return`。`AIConfigPage` 在每次变更后调用 `load(true)`，让其他地方的下拉框能看到变化。
- `useProjectStore`——由 `ProjectLayout` 加载（`load(pid)`），显示在面包屑/概览中；各面板在修改章节后调用 `refresh()`，以更新 `chapter_count`/`total_words`；临时网络失败仍静默保留，明确404则清空目标/项目并使旧响应失效，避免继续挂载不可访问作品的编辑和任务副作用。`OverviewPanel` 在 PATCH 后调用 `setProject(updated)`。`load(id)` 只有在 `get().project?.id === id` 时才保留当前项目，因此切换项目时绝不会闪现旧项目（`ProjectLayout` 在 `project.id !== pid` 时也会渲染加载动画）。
  - `requestSeq` / `refreshSeq` 只接受当前请求；`reset()` 同时使旧请求失效并清空 `targetId`。保存回调 `setProject` 必须同时匹配目标 ID 和**实际已加载**的 `project.id`，不能仅凭 targetId：退出后新会话可能再次加载同一 pid，旧保存响应不得在新加载完成前回填内容或关闭 loading。回归见 `stores/project.test.ts`。
  - 项目 `owner` 与路由 pid、真实账户派生管理员人工管理能力，禁止另存一个可切换认证身份的“当前所有者”。自动副作用和阅读进度边界见[管理员作品管理](../backend/admin-projects.md)。
  - 删除/恢复/purge 成功调用 `discard(projectId)`，仅匹配当前目标或已加载 ID 时递增请求代次并清空；不能重置另一个作品的未保存内容。明确404/同ID迟到保存与目标级失效回归见 `stores/project.test.ts`。

- `useTasksStore`——由 `TaskPanel`（徽标 + 进行中列表）读取；章节编辑器 / `WizardGenerator` / 任务面板在创建、重试、取消任务后调用 `track(task)`。
  - **轮询节奏**（纯函数 `pollInterval(activeCount)`）：有活动任务 `POLL_ACTIVE_MS = 3000`，否则 `POLL_IDLE_MS = 15000`。`refresh()` 拉 `GET /tasks?active=true&limit=200`，完成后按新数量安排下一次（单个模块级 timer）；`track()` 先用 `upsertActive` 合并（已结束则移除，保持 id 倒序）再立即 `refresh()`，从而马上切到 3 秒节奏。
  - `startPolling()` 引用计数（`TaskPanel` 在 effect 中调用并返回停止函数），计数归零时清掉 timer；未登录区没有 `AppLayout`，所以不会轮询。
  - `version`：本轮有任务从活动列表消失（`countFinished(prev, next) > 0`）、`track`、`bump`（本地删除 / 清理）时递增，「最近」列表的 loader 依赖它重载。
  - 过期响应防护：模块级 `requestSeq`（只采用最新一次请求）+ `epoch`（`reset()` 递增，丢弃上一个用户的响应）。`reset()` 时若仍有轮询者（登录后 `AppLayout` 的 effect 先于 `App` 的重置 effect 执行）会立即用新会话重新 `refresh()`。
  - 轮询失败静默（401 由全局处理器登出）。
  - 删除/回收站操作用 `discardProject(id)` 使旧轮询失效并移除目标任务；本地迟到 `track` 不能重新挂回旧任务，恢复后真正的新任务以服务器新列表为准。`reset()` 同时清空丢弃集合，不能把一个账户的失效状态带给另一个账户。
  - 单个任务的实时输出 / 进度不进 store，由 `useTaskStream` 本地订阅；store 只是列表 + 徽标。

- `useSkillStore`（`10-23-skills-mcp`）——由 `SkillSelect`（章节编辑器 AI 助手、批量弹窗、智能体输入区）、智能体的 `/` 补全、`SkillsPage` 读取。
  - **按需加载**：store 本身因 `App` 的重置 effect 在入口 chunk 里，但 `load()` 内部 `await import('../api/skills')`，接口模块和常量不随入口加载；
    挂载 `useSkills()` 的组件才触发请求，未打开相关界面时不发请求。
  - `load()` 与 `useAIConfigStore` 一样幂等（`loading || (loaded && !force)` 直接返回）；`SkillsPage` 每次增删改、复制、导入、恢复后 `load(true)`，其他地方的下拉框随之更新。
  - 只缓存列表（`SkillBrief`，不含正文）；编辑抽屉的详情用 `skillApi.get` 本地加载。随 `userId` 重置。
- `useAuthStore.mcpEnabled` 是部署配置而不是用户数据：`init()` 时与 `registrationEnabled` 一起读取，用于隐藏 `StudioNavigation` 共用侧栏/移动导航中的「MCP 插件」入口与智能体面板的插件选择。
- `useThemeStore`——由 `App`（`ConfigProvider` 算法、`ThemeSync`）、`ThemeSwitch`、`usePalette`、书封、阅读器读取。是**设备偏好**而非用户数据，因此**不**加入 `userId` 变化时的重置 effect；系统主题监听在 `App` 挂载时 `watchSystemTheme()` 注册、卸载时移除。
- 书架偏好与阅读设置只在各自页面里 `useState(read*)` + `useEffect(write*)`，不进 store（只有一个页面使用）。阅读进度在服务端（`ProjectOut.reading`），阅读器内存里只保留最近一次上报的位置。

其余状态一律保持本地。不要把章节或大纲之类的列表放进 store。

### 清空用户级状态（登出 / 401 / 切换账户）

```ts
// src/App.tsx
useEffect(() => {
  // 任意接口返回 401：清空会话，RequireAuth 会跳转到登录页
  setUnauthorizedHandler(() => useAuthStore.getState().clear())
  void init()
  return () => setUnauthorizedHandler(null)
}, [init])

// 切换/退出账户时清空与用户相关的缓存
useEffect(() => {
  useAIConfigStore.getState().reset()
  useProjectStore.getState().reset()
  useTasksStore.getState().reset()
  useStyleStore.getState().reset()
  useSkillStore.getState().reset()
}, [userId])   // userId = useAuthStore((s) => s.user?.id)
```

- 当前请求代次的API返回401（`/auth/login*` 与 `/auth/me*` 除外，见 `client.ts` 中的 `shouldRedirectOn401`）→ `clear()` → `status: 'anonymous'` → `RequireAuth` 携带 `state.from` 重定向到 `/login`。
- `logout()` 调用API，finally仅在自身epoch仍有效时clear；旧logout不能清新login。AppLayout菜单await后也仅在当前status仍anonymous时navigate，防旧UI回调干扰新账号页面。
- `user?.id` 的任何变化（登出、401、以其他用户登录）都会重置 AI 配置和项目 store。新增的用户级 store **必须**把它的 `reset()` 加入这个 effect。
- `init()` 使用 `Promise.allSettled` 执行 `/auth/me` 与 `/auth/config`；`/auth/me` 失败即视为匿名（401 与网络错误不记录日志）。

---

## 服务端状态

没有客户端缓存失效框架。目前采用的规则：

- 变更成功后，重新获取受影响的列表（`reload()`），或把服务端响应写回（`applyServerChapter(updated)`、`setProject(updated)`）。绝不在服务端响应之前乐观地修改本地状态。
- 服务端响应是派生字段（`word_count`、`status`、`updated_at`）的唯一可信来源；编辑器仅在实时输入/流式输出时显示客户端的 `countWords`，保存后用服务端返回的章节替换。
- 生成失败或被中止后，`reloadChapter()` 只同步服务端的 `status`（后端会将其回退），并保留本地文本。
- **跨组件的项目数据失效**（`10-23-project-agent`）：`utils/projectRefresh.ts` 是唯一的广播点，`invalidateProject(projectId, resources)`；
  `ProjectResource` = `project | characters | organizations | relationships | careers | world | outlines | chapters | foreshadows | memories | knowledge`。
  它只通知「哪些资源变了」，不缓存数据；订阅方用 `useLoader(fn, { projectId, resources })`（命中即 `reload()`）或 `useProjectRefresh(pid, resources, refresh)`。
  目前由智能体面板在修改执行 / 撤销后触发（见 component-guidelines「项目智能体面板」）；以后其他会在别处改动项目数据的功能（后台任务结束等）也用它，而不是在各页面之间互相传回调。
  有本地未保存编辑的页面（章节编辑器）收到失效时不能直接覆盖，先比较并提示。

---

## 常见错误

- 新增用户级 store 却没有在 `userId` 变化时重置——下一个用户会看到上一个用户的数据。
- 调用 `useAIConfigStore().load()` 期望重新获取——加载完成后它是空操作；应使用 `load(true)`。
- 在频繁重渲染的组件中只需一个字段却订阅整个 store；优先使用 selector（`useAuthStore((s) => s.user)`、`useProjectStore((s) => s.refresh)`），`App`、`AppLayout` 和编辑器都是这样做的。
- 在各个页面中单独处理 401——它已通过 `setUnauthorizedHandler` 集中处理；页面只需显示 `errorMessage(err)`。
