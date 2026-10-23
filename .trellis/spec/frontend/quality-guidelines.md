# 质量规范

> 前端开发的代码质量标准。

---

## 概述

质量门禁是在 `frontend/` 下运行的三个 npm 脚本：

```bash
npm run lint && npm run build && npm test
```

- `lint` → `eslint .`（flat config `eslint.config.js`：`@eslint/js` recommended、`typescript-eslint` recommended、`eslint-plugin-react-hooks` flat recommended、`react-refresh/only-export-components` 设为 warn 并启用 `allowConstantExport`、`@typescript-eslint/no-explicit-any: error`）。
- `build` → `tsc -b && vite build`（类型检查是构建的一部分；严格的 tsconfig 将未使用的局部变量/参数视为错误）。
- `test` → `vitest run`（`environment: 'node'`、`include: ['src/**/*.test.ts']`）。

### 开发服务器代理（`vite.config.ts`）

`/api` 被代理到 `process.env.VITE_BACKEND_URL ?? 'http://localhost:8000'`，并设置 `changeOrigin: true`。对于 `text/event-stream` 响应，代理会改写响应头，确保流不会被缓冲或压缩：

```ts
proxy.on('proxyRes', (proxyRes) => {
  if (String(proxyRes.headers['content-type'] ?? '').startsWith('text/event-stream')) {
    proxyRes.headers['cache-control'] = 'no-cache, no-transform'
    proxyRes.headers['x-accel-buffering'] = 'no'
  }
})
```
后端发送同样的响应头（`backend/app/api/sse.py` 中的 `SSE_HEADERS`）。如果修改了该代理或反向代理，务必保持两端一致；否则生成的文本会在最后一次性全部到达。

### 包体积

`vite build` 目前产出一个约 1.37 MB 的单一 JS chunk（`dist/assets/index-*.js` ≈ 1,369 kB，gzip 后 ≈ 425 kB；含 `10-23-chapter-versions` 引入的 `diff` 与 `10-23-task-engine` 的任务面板），因为 AntD 和所有页面被打包在一起，且没有任何路由使用 `React.lazy` / 动态 `import()`。`build.chunkSizeWarningLimit` 已调高到 `1500`（kB），因此不会告警。如果 chunk 超过该上限，应在 `router.tsx` 中引入路由级懒加载（或 `manualChunks`），而不是再次调高上限。

---

## 禁止模式

- `any`——lint 报错；使用 `unknown` 并收窄类型。
- 在页面/组件中直接调用 `fetch`——使用 `src/api/*`（保留 `credentials: 'include'`、`ApiError`、401 处理）。
- 用 `EventSource` 处理 AI 流——改写是 POST，任务订阅虽是 GET 也必须走 `subscribeTask`（`getSSE`：统一 `ApiError` / 401 / 中止 / 重连）。
- 在页面里直接对 generate / polish / wizard 做流式请求——它们返回 202 `Task`，实时输出只能订阅 `/tasks/{id}/stream`。
- 在组件卸载 / 离开页面时取消后台任务——卸载只 `detach`，取消必须是用户显式操作（`cancel()`）。
- 静态的 AntD `message` / `Modal.confirm`——使用 `App.useApp()`。
- 直接 `dayjs(serverTime)` / `new Date(serverTime)`——使用 `formatTime` / `parseServerTime`。
- 向 `useLoader` 传入内联的（未 memo 化的）loader 函数。
- 在 `client.ts` 之外硬编码 `/api/v1` 路径。
- 在用户操作中静默吞掉错误——应显示 `message.error(errorMessage(err))`（仅有的有意静默捕获是 `useProjectStore.refresh()` 和编辑器的 `reloadChapter()`）。

---

## 必需模式

- 异步处理函数：`setX(true)` → `try { await …; message.success(…) } catch (err) { message.error(errorMessage(err)) } finally { setX(false) }`（见 `WorldPanel.handleSave`、`ExportModalInner.handleExport`）。
- 不等待结果的 promise 用 `void` 标记（`void load()`、`void refreshProject()`、`onClick={() => void handleSave()}`）。
- 同步流式 UI（局部改写）使用 `useStreamTask` 并传递 `signal`；后台任务 UI 使用 `useTaskStream`，挂载时按资源（`chapter_id` / `project_id + kind`）查询活动任务并自动接上，创建 / 重试 / 取消后调用 `useTasksStore.track(task)`。两者在运行期间都要提供可见的停止（取消）按钮。
- 忙碌时禁用会冲突的控件（`disabled={task.running}`、`loading={saving}`、`confirmLoading`）。
- 用户可见的字符串使用简体中文；代码注释与现有文件一致，使用中文 JSDoc 单行注释。
- 新增的用户级 store 要在 `App.tsx` 中随 `userId` 变化而重置。

---

## 测试要求

- 纯逻辑（解析器、格式化函数、分组）需配套同目录的 `*.test.ts` Vitest 测试。现有测试套件包括 `src/api/sse.test.ts`（SSE 解析器 + `runGeneration` stub-fetch）、`src/api/tasks.test.ts`（`subscribeTask` 快照覆盖 / 重连退避 / done 与 error 分派 / 404 不重连 / 中止，以及 `queueLabel`、`taskLink`、查询串）、`src/stores/tasks.test.ts`（`pollInterval` 3s/15s、`upsertActive`、`countFinished`）、`src/utils/time.test.ts`（UTC 解析）、`src/utils/textOffset.test.ts`（UTF-16 ↔ 码点）、`src/utils/textDiff.test.ts`（逐字 diff 与增删字数）等，必须全部通过（`npm test`）。
- 测试环境是 `node`（没有 jsdom）；没有组件测试。需要 DOM/`fetch` 的代码通过 stub 全局对象来测试。
- 修改 `sse.ts` 或 `client.ts` 时，必须遵循 [hook-guidelines.md](./hook-guidelines.md#场景sse-客户端解析器) 和 [type-safety.md](./type-safety.md#场景api-客户端契约) 中的「必需的测试」清单。
- 测试名可以使用中文，与现有测试套件保持一致（`it('处理跨 chunk 截断', …)`）。

---

## 代码评审清单

- [ ] 在 `frontend/` 中 `npm run lint && npm run build && npm test` 通过。
- [ ] `src/api/types.ts` 中新增/修改的传输类型与 `backend/app/schemas/*.py` 一致（字段名、可空性、`Literal` 枚举），且标签 `Record` 覆盖所有枚举值。
- [ ] PATCH 请求体遵循后端语义（省略即保持不变；空 `api_key` 保留原密钥；绝不发送 `status: 'generating'`）。
- [ ] 所有网络调用都经过 `src/api/`；错误通过 `errorMessage` 显示。
- [ ] 流式：传递了 `signal`、存在停止按钮、中止按 `aborted` 而非错误处理、运行期间输入框只读/禁用。
- [ ] 后台任务：卸载只断开订阅；挂载自动接上活动任务；取消 / 失败 / 中断 / `detached` 各分支都处理；`snapshot` 覆盖文本；离开守卫不因任务运行而拦截。
- [ ] 章节编辑器的不变式保持完好：AI 操作前自动保存、Ctrl/Cmd+S、`useBlocker` + `beforeunload` 防护（仅未保存 / 改写待采纳）、卡在 `generating`（且无活动任务）时可复位且禁用保存。
- [ ] 服务端时间戳使用 `formatTime` 渲染。
- [ ] 任何新增的用户级全局状态在登出/401 时都会被重置。
- [ ] 包体积仍低于 `chunkSizeWarningLimit`（检查 `vite build` 输出）。
