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
| 当前项目（全局） | `stores/project.ts` → `useProjectStore` | `project`、`loading`、`error`；`load(id)`、`refresh()`、`setProject(p)`、`reset()` |
| 页面数据（本地） | 页面中的 `useLoader` | 世界观/角色/大纲/章节列表、当前打开的章节、导出用章节列表 |
| 表单 / UI 状态（本地） | `useState`、`Form.useForm` | 弹窗打开标志、`editing`、`saving`、编辑器 `title`/`content`、`aiConfigId`、AI 配置页上的测试结果 |
| 流式状态（本地） | `useStreamTask` | `text`、`running`、`error`、abort controller |

---

## 何时使用全局状态

仅当两个或以上不相关的页面需要同一份数据，**并且**该数据是用户级的：

- `useAuthStore`——由 `RequireAuth`、`LoginPage`、`AppLayout`、`App` 读取。
- `useAIConfigStore`——由 `AIConfigSelect`（所有生成类 UI）、`useHasAIConfig`、`AIConfigPage` 读取。`load()` 是幂等的：`if (get().loading || (get().loaded && !force)) return`。`AIConfigPage` 在每次变更后调用 `load(true)`，让其他地方的下拉框能看到变化。
- `useProjectStore`——由 `ProjectLayout` 加载（`load(pid)`），显示在面包屑/概览中；各面板在修改章节后调用 `refresh()`（静默，吞掉错误），以更新 `chapter_count`/`total_words`；`OverviewPanel` 在 PATCH 后调用 `setProject(updated)`。`load(id)` 只有在 `get().project?.id === id` 时才保留当前项目，因此切换项目时绝不会闪现旧项目（`ProjectLayout` 在 `project.id !== pid` 时也会渲染加载动画）。

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

---

## 常见错误

- 新增用户级 store 却没有在 `userId` 变化时重置——下一个用户会看到上一个用户的数据。
- 调用 `useAIConfigStore().load()` 期望重新获取——加载完成后它是空操作；应使用 `load(true)`。
- 在频繁重渲染的组件中只需一个字段却订阅整个 store；优先使用 selector（`useAuthStore((s) => s.user)`、`useProjectStore((s) => s.refresh)`），`App`、`AppLayout` 和编辑器都是这样做的。
- 在各个页面中单独处理 401——它已通过 `setUnauthorizedHandler` 集中处理；页面只需显示 `errorMessage(err)`。
