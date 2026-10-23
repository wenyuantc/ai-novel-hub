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
| `AppLayout` | 顶部 `Layout.Header` 带菜单（`我的项目` / `模型配置`；在 `/admin` 下不高亮任何菜单项），用户名下拉菜单：`用户管理`（仅当 `user.is_admin` 时显示）→ `/admin/users`，`修改密码` → `ChangePasswordModal`，退出登录调用 `useAuthStore().logout()` 后执行 `navigate('/login', { replace: true })`；渲染 `<Outlet/>`。 |
| `RequireAuth` | 路由守卫：`status === 'unknown'` → `<FullPageSpin/>`；`'anonymous'` → `<Navigate to="/login" replace state={{ from: location.pathname + location.search }} />`。`LoginPage` 读取 `state.from`（默认 `/projects`）。 |
| `RequireAdmin` | 包裹 `/admin/users`（位于 `RequireAuth` 内部）。判定逻辑是 `utils/adminUsers.ts` 中的纯函数 `adminGuard(status, user)`：`'loading'` → `<FullPageSpin/>`，`'login'` → `/login`，`'home'`（非管理员）→ `/`，`'allow'` → children。`is_admin` 只从 `/auth/me` 读取一次；被降级的管理员在刷新前仍会看到菜单，但 API 调用会返回 403 并显示提示信息。 |
| `ChangePasswordModal` | `{ open; onClose }`。原密码 + 两次输入新密码（`utils/validation`）。成功 → `其他设备上的登录已失效`；后端会重新签发 cookie，因此当前标签页保持登录。400 `原密码不正确` 通过 `errorMessage(err)` 在表单内显示，不会触发全局登出。 |
| `AIConfigSelect` | `{ value?: number \| null; onChange?: (v: number \| null) => void; disabled?: boolean }`。挂载时调用 `useAIConfigStore().load()`。空选择 = `null` = "使用默认配置"（placeholder 为 `默认：{name}（{model}）`）。加载完成且配置数为零时，渲染一个链接到 `/settings/ai` 的警告 `Alert` 以代替下拉框。 |
| `StreamOutput` | `{ text; running; placeholder?; maxHeight? = 360 }`。`pre-wrap` 等宽文本框，`running` 时自动滚动到底部，运行中显示一个小的 `Spin`。 |
| `WizardGenerator<K extends WizardKind>` | `{ projectId; kind: K; existingCount?; showResult?; onDone?(items: WizardItemMap[K][]); onRunningChange?(running) }`。运行中表单禁用；`useHasAIConfig() === false` 时开始按钮禁用；运行中显示接到 `task.stop` 的危险样式 `停止` 按钮。中止时显示 `已停止生成，原有数据未改变`。`existingCount > 0` 时显示"将会替换"的 `Alert`。 |
| `WizardModal<K>` | 将 `WizardGenerator` 包在 `Modal` 中（`maskClosable={false}`、`destroyOnHidden`、`footer={null}`）。运行中拒绝关闭（`message.warning('正在生成，请先停止')`）。完成时：先 `onDone(items)` 再 `onClose()`。被 `WorldPanel`、`CharactersPanel`、`OutlinesPanel` 使用。 |
| `ExportModal` | `{ projectId; open; onClose }`。仅在 `open` 时渲染 `ExportModalInner`（使其 `useLoader` 每次打开都获取最新章节）。格式单选 `txt`/`md`；行选择会禁用 `generating` 状态的章节；不选则导出全部。调用 `exportProject` 并显示 `已导出：{filename}`。被 `OverviewPanel` 使用。 |
| `StoryPreview` | 具名导出 `WorldPreview`、`CharacterPreview`、`OutlinePreview`（使用 `utils/outline.ts` 中的 `groupOutlines`/`volumeLabel`）。 |

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
2. **AI 生成/润色前自动保存**：
   ```ts
   const runAI = async (mode: AIMode) => {
     if (dirty) {
       if (!(await save())) return
       message.info('已自动保存当前修改')
     }
     const outcome = await task.start((onDelta, signal) => mode === 'generate' ? generateChapter(...) : polishChapter(...))
   ```
   `done` 时 → `applyServerChapter(outcome.value)`（服务端已保存，状态为 `done`）。中止/出错时 → `reloadChapter()` 只同步服务端状态并保留本地文本。
3. **流式期间只读**：运行中文本域显示 `task.text`（`displayContent = task.running ? task.text : content`），并设置 `readOnly={task.running}`；标题输入框、AI 表单、标签页和 `AIConfigSelect` 都设置 `disabled={task.running}`。
4. **Ctrl/Cmd+S**：只注册一次 window `keydown` 监听器；它通过 ref 调用最新的 `handleSave`（在无依赖的 effect 中执行 `saveShortcutRef.current = handleSave`），因此监听器永远不会捕获过期状态。`handleSave` 在运行/保存中不做任何事，无改动时显示 `没有需要保存的修改`。
5. **未保存离开守卫**：`const guard = dirty || task.running`。浏览器卸载通过 `beforeunload` 监听器处理（仅在 `guard` 时挂载）；应用内导航通过 `useBlocker(({ currentLocation, nextLocation }) => guard && currentLocation.pathname !== nextLocation.pathname)` 处理，并弹出确认 `Modal`，其确定操作执行 `task.stop(); blocker.proceed?.()`。
6. **卡在 `generating` 的章节**：`const stuck = chapter.status === 'generating' && !task.running` 会显示一个带 `复位` 操作的警告 `Alert` → `chapterApi.update(pid, chapter.id, { status: 'draft' })`。卡住期间，保存、标记完成和 AI 按钮都被禁用，因为后端会以 409 拒绝对生成中章节的正文/标题 PATCH（`backend/app/services/chapter_service.py` 中的 `ConflictError`），且生成/润色会返回 409 `该章节正在生成中，请稍候再试`。
7. **覆盖已有正文的重新生成**通过 `modal.confirm` 询问（`重新生成正文？`）；正文为空时禁用润色。
8. 上一章/下一章导航按 `chapter_no` 再按 `id` 对同级章节排序。

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
