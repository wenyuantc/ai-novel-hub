# 状态管理

> 本项目中状态的管理方式。

---

## 概述

Zustand 5（`create<State>((set, get) => ({ … }))`）只用于最小集合的跨页面状态；其余一律使用 React `useState`；服务端数据由各页面通过 `useLoader` 获取（没有查询缓存）。不使用 Redux，除 AntD 的 `ConfigProvider`/`App` 外不使用任何 Context provider。

---

## 状态分类

| 类别 | 位置 | 示例 |
|----------|-------|----------|
| 会话（全局） | `stores/auth.ts` → `useAuthStore` | `status: 'unknown' \| 'authenticated' \| 'anonymous'`、`user`、`registrationEnabled`；actions `init`、`login`、`register`、`logout`、`clear` |
| 用户级缓存（全局） | `stores/aiConfigs.ts` → `useAIConfigStore` | `configs`、`loaded`、`loading`；`load(force = false)`、`reset()` |
| 活动任务（全局） | `stores/tasks.ts` → `useTasksStore` | `active`（排队 + 运行中，id 倒序）、`loaded`、`version`；`refresh()`、`track(task)`、`bump()`、`startPolling()`、`reset()` |
| 当前项目（全局） | `stores/project.ts` → `useProjectStore` | `project`、`loading`、`error`；`load(id)`、`refresh()`、`setProject(p)`、`reset()` |
| 主题（全局，设备级） | `theme/themeStore.ts` → `useThemeStore` | `mode`（`light` / `dark` / `system`，localStorage `ainovel.theme`）、`systemDark`；`setMode`、`setSystemDark`；`useResolvedTheme()`、`watchSystemTheme()` |
| 本地偏好（localStorage，非 store） | `utils/preferences.ts`、`utils/shelf.ts`、`utils/reader.ts` | `ainovel.autoContinue`；书架 `ainovel.shelf`（`{query, sort, status}`）；阅读设置 `ainovel.reader`（`{fontSize, lineHeight, width, background, font}`）。读写都包 try/catch，读出后经 `parse*` / `normalize*` 校验 |
| 页面数据（本地） | 页面中的 `useLoader` | 世界观/角色/大纲/章节列表、当前打开的章节、导出用章节列表；组织 / 成员 / 关系 / 关系类型 / 图谱 / 章节出场角色（各面板与抽屉 Tab 各自加载，未做全局缓存，保存后各自 `reload()`） |
| 表单 / UI 状态（本地） | `useState`、`Form.useForm` | 弹窗打开标志、`editing`、`saving`、编辑器 `title`/`content`、`aiConfigId`、AI 配置页上的测试结果 |
| 流式状态（本地） | `useStreamTask`（局部改写）/ `useTaskStream`（任务订阅） | `text`、`running`、`error`、abort controller；任务另有 `task`、`continueInfo`、`reconnecting` |

---

## 何时使用全局状态

仅当两个或以上不相关的页面需要同一份数据，**并且**该数据是用户级的：

- `useAuthStore`——由 `RequireAuth`、`LoginPage`、`AppLayout`、`App` 读取。
- `useAIConfigStore`——由 `AIConfigSelect`（所有生成类 UI）、`useHasAIConfig`、`AIConfigPage` 读取。`load()` 是幂等的：`if (get().loading || (get().loaded && !force)) return`。`AIConfigPage` 在每次变更后调用 `load(true)`，让其他地方的下拉框能看到变化。
- `useProjectStore`——由 `ProjectLayout` 加载（`load(pid)`），显示在面包屑/概览中；各面板在修改章节后调用 `refresh()`（静默，吞掉错误），以更新 `chapter_count`/`total_words`；`OverviewPanel` 在 PATCH 后调用 `setProject(updated)`。`load(id)` 只有在 `get().project?.id === id` 时才保留当前项目，因此切换项目时绝不会闪现旧项目（`ProjectLayout` 在 `project.id !== pid` 时也会渲染加载动画）。

- `useTasksStore`——由 `TaskPanel`（徽标 + 进行中列表）读取；章节编辑器 / `WizardGenerator` / 任务面板在创建、重试、取消任务后调用 `track(task)`。
  - **轮询节奏**（纯函数 `pollInterval(activeCount)`）：有活动任务 `POLL_ACTIVE_MS = 3000`，否则 `POLL_IDLE_MS = 15000`。`refresh()` 拉 `GET /tasks?active=true&limit=200`，完成后按新数量安排下一次（单个模块级 timer）；`track()` 先用 `upsertActive` 合并（已结束则移除，保持 id 倒序）再立即 `refresh()`，从而马上切到 3 秒节奏。
  - `startPolling()` 引用计数（`TaskPanel` 在 effect 中调用并返回停止函数），计数归零时清掉 timer；未登录区没有 `AppLayout`，所以不会轮询。
  - `version`：本轮有任务从活动列表消失（`countFinished(prev, next) > 0`）、`track`、`bump`（本地删除 / 清理）时递增，「最近」列表的 loader 依赖它重载。
  - 过期响应防护：模块级 `requestSeq`（只采用最新一次请求）+ `epoch`（`reset()` 递增，丢弃上一个用户的响应）。`reset()` 时若仍有轮询者（登录后 `AppLayout` 的 effect 先于 `App` 的重置 effect 执行）会立即用新会话重新 `refresh()`。
  - 轮询失败静默（401 由全局处理器登出）。
  - 单个任务的实时输出 / 进度不进 store，由 `useTaskStream` 本地订阅；store 只是列表 + 徽标。

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
}, [userId])   // userId = useAuthStore((s) => s.user?.id)
```

- 任意 API 调用返回 401（`/auth/login*` 与 `/auth/me*` 除外，见 `client.ts` 中的 `shouldRedirectOn401`）→ `clear()` → `status: 'anonymous'` → `RequireAuth` 携带 `state.from` 重定向到 `/login`。
- `logout()` 调用 API，并在 `finally` 中设置为 `anonymous`，即使请求失败也是如此。
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
