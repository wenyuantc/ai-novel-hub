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

`10-23-story-entities` 之前 `vite build` 只产出一个约 1.37 MB 的单一 chunk；新增设定页面后升到 1,510 kB，超过 `build.chunkSizeWarningLimit = 1500`。按本规范改为**路由级懒加载**（`router.tsx` 的 `lazyPage`，React Router `lazy` 路由属性）：章节编辑器、组织页、关系页、职业页、模型配置页单独成 chunk；关系图谱再在 `RelationshipsPanel` 中用 `React.lazy` 拆出。第三方共享依赖在 `vite.config.ts` 的 `build.rolldownOptions.output.codeSplitting.groups` 中命名（Vite 8 / rolldown 下 `rollupOptions` 与 `manualChunks` 已弃用，这是等价的非弃用写法）：`vendor-react`（react / react-dom / scheduler / react-router）、`vendor-antd`（antd、`@ant-design/*`、`rc-*` / `@rc-component/*`、dayjs 及 antd 的工具依赖）；未分组的依赖由 rolldown 自动拆分。当前产物（`10-23-career-system` 后）：入口 `index-*.js` ≈ 125 kB（gzip ≈ 36 kB；首屏同步加载的 JS 合计由约 140 kB 降至约 125 kB——原先 `AIConfigSelect-*.js` ≈ 11 kB、`useProjectId-*.js` ≈ 2 kB 也随入口同步加载，现已并入入口），任务结果查看组件经 `taskResultViewers.ts` 的 `React.lazy` 各自成 chunk、只在打开预览时加载（`CharacterDraftResult-*.js` ≈ 4 kB、`OrganizationDraftResult-*.js` ≈ 4 kB、`CareerDraftResult-*.js` ≈ 3 kB、`RelationsParseResultView-*.js` ≈ 1.5 kB，另有共享的 `RelationSuggestionList-*.js` / `StageListEditor-*.js` ≈ 2 kB），`CareersPanel-*.js` ≈ 7 kB（职业页懒加载路由），`vendor-antd-*.js` ≈ 1,123 kB（gzip ≈ 346 kB，最大的 chunk），`vendor-react-*.js` ≈ 204 kB（gzip ≈ 66 kB），`ChapterEditorPage-*.js` ≈ 38 kB，图谱 `RelationGraph-*.js` ≈ 226 kB（gzip ≈ 73 kB）+ `RelationGraph-*.css` ≈ 15 kB。**规则**：`@xyflow/react` / `@dagrejs/dagre` 只能在 `components/relations/RelationGraph.tsx` 中 import，且**不得**加入任何 `codeSplitting` 分组（否则会被并入同步加载的 vendor chunk；图谱筛选等纯逻辑放 `utils/graphFilter.ts`，不依赖图谱库）；新增分组只放所有页面共享的基础依赖，页面专用库（如 jsdiff）留给自动拆分。任何 chunk 超过 1500 kB 时继续拆路由或细分分组（例如把 `@ant-design/icons` 单独成组），**不得调高上限**。构建后检查输出里没有 `Some chunks are larger than 1500 kB`，并确认只有 `RelationGraph-*.js` 含图谱代码（`grep -l react-flow__ dist/assets/*.js`）。

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

- 纯逻辑（解析器、格式化函数、分组）需配套同目录的 `*.test.ts` Vitest 测试。现有测试套件包括 `src/api/sse.test.ts`（SSE 解析器 + `runGeneration` stub-fetch）、`src/api/tasks.test.ts`（`subscribeTask` 快照覆盖 / 重连退避 / done 与 error 分派 / 404 不重连 / 中止，以及 `queueLabel`、`taskLink`、查询串）、`src/stores/tasks.test.ts`（`pollInterval` 3s/15s、`upsertActive`、`countFinished`）、`src/utils/time.test.ts`（UTC 解析）、`src/utils/textOffset.test.ts`（UTF-16 ↔ 码点）、`src/utils/textDiff.test.ts`（逐字 diff 与增删字数）、`src/api/relationships.test.ts`（视角称呼、类型选项、亲密度颜色）、`src/api/entityTasks.test.ts`（结果收窄、建议 → with-relations / batch 载荷）、`src/api/cards.test.ts`（大小 / JSON 校验、冲突默认跳过、重命名预览）、`src/api/characters.test.ts`（定位兼容旧值、主角判定、别名）、`src/utils/orgTree.test.ts`、`src/utils/world.test.ts`、`src/utils/graphFilter.test.ts` 等，必须全部通过（`npm test`）。
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
- [ ] 包体积仍低于 `chunkSizeWarningLimit`（检查 `vite build` 输出，`vendor-antd` 是最大的 chunk）；图谱库没有被主包或 vendor 分组引用（`RelationGraph-*.js` 单独存在）。
