# Hook 规范

> 本项目中 hook 的使用方式。

---

## 概述

项目没有使用数据请求库（没有 React Query/SWR）。`src/hooks/` 中的五个小型自定义 hook 覆盖了全部场景：一次性异步加载（`useLoader`）、同步流式请求（`useStreamTask`，目前只有局部改写）、后台任务订阅（`useTaskStream`，整章生成 / 润色 / 向导）、路由参数解析（`useProjectId`）以及派生的 store 选择器（`useHasAIConfig`）。全局缓存数据存放在 Zustand store 中（见 [state-management.md](./state-management.md)）。

---

## 自定义 Hook 模式

### `useStreamTask` — `src/hooks/useStreamTask.ts`

```ts
export type StreamOutcome<T> = { status: 'done'; value: T } | { status: 'aborted' } | { status: 'error'; message: string }

export function useStreamTask(): {
  text: string; running: boolean; error: string | null
  start: <T>(runner: (onDelta: (delta: string) => void, signal: AbortSignal) => Promise<T>) => Promise<StreamOutcome<T>>
  stop: () => void
  reset: () => void
}
```

- 在 ref 中持有一个 `AbortController`。`start` 会中止之前的 controller、创建新的 controller、清空 `text`/`error`、设置 `running`，并追加每段 delta（`setText((prev) => prev + delta)`）。
- 从不抛出异常：返回可辨识联合类型 `StreamOutcome`。中止（`isAbortError(err) || controller.signal.aborted`）→ `{ status: 'aborted' }`，且不设置 `error`；其他错误 → `setError(errorMessage(err))` 并返回 `{ status: 'error', message }`。
- `finally` 仅在该 controller 仍是当前 controller 时才清除 `running`（较新的 `start` 在竞争中胜出）。
- 卸载时中止：`useEffect(() => () => controllerRef.current?.abort(), [])`。
- 用法——runner 接收 hook 提供的 `onDelta`/`signal`，并将其传给 API 函数：
  ```ts
  // src/components/WizardGenerator.tsx
  const outcome = await task.start((onDelta, signal) => runWizard(projectId, kind, body, onDelta, signal))
  if (outcome.status === 'done') { … } else if (outcome.status === 'aborted') { message.info('已停止生成，原有数据未改变') }
  ```
  错误通过 `task.error` 渲染（`<Alert type="error" message="生成失败" description={task.error} />`）。

### `useTaskStream` — `src/hooks/useTaskStream.ts`

```ts
export type TaskOutcome = TaskEnd | { status: 'detached' } | { status: 'lost'; message: string }
export function useTaskStream(): {
  task: Task | null; text: string; running: boolean; error: string | null
  continueInfo: ContinueEvent | null; reconnecting: boolean
  follow: (initial: Task) => Promise<TaskOutcome>
  detach: () => void
  cancel: () => Promise<void>
  reset: () => void
}
```

- 命令式（与 `useStreamTask` 同形），而不是 design 里的 `useTaskStream(taskId)`：调用方需要在同一个 async 流程里拿到终止结果（章节 `applyServerChapter`、向导 `onDone`）。只读场景（任务面板查看输出）在 effect 里 `follow(task)`。
- `follow` 先中止上一个订阅，`task` 立即设为传入值（排队位置马上可见），清空 `text`/`error`/`continueInfo`，`running = true`；回调全部经 `subscribeTask`：`snapshot` 覆盖 `text` 并替换 `task`（为 null 时保留）、`delta` 追加、`continue` → `continueInfo`、`progress` 合并 `status/progress/stage/queue_position`（载荷不含 `queue_position` 且不再排队时置 null）、重连时 `reconnecting = true`。
- 结束：`TaskEnd`（`succeeded` 带 `result`；`failed`/`cancelled`/`interrupted` 带 `detail`），`failed`/`interrupted` 写入 `error`，**取消不算错误**；本地中止（再次 follow、`detach`、卸载）→ `detached`；订阅 4xx / 重连耗尽 → `lost` 并写 `error`。从不抛出。
- **卸载 / `detach` 只断开订阅，不取消任务**（D1：关闭页面不中断）。`cancel()` 调 `POST /tasks/{id}/cancel`，之后由订阅收到 `error(status=cancelled)` 结束 follow；409（已结束）静默，其他错误抛给调用方。
- 所有 setState 都用 `controllerRef.current === controller` 守卫，过期订阅的回调被丢弃。

### `useLoader` — `src/hooks/useLoader.ts`

```ts
export function useLoader<T>(fn: () => Promise<T>): {
  data: T | undefined; loading: boolean; error: string | null
  reload: () => void
  setData: (updater: (prev: T | undefined) => T) => void
}
```

- 在以 `[fn, version]` 为依赖的 effect 中运行 `fn`；`alive` 标志会丢弃过期运行的结果。
- 调用方**必须**用 `useCallback` 对 `fn` 做 memo，否则每次渲染都会重新请求。
- 错误通过 `errorMessage(err)` 统一规范化；出错时保留之前的 `data`。
- `reload()` 会设置 `loading: true` 并递增 `version`。
  ```ts
  // src/pages/project/WorldPanel.tsx
  const loader = useCallback(() => worldApi.list(pid), [pid])
  const { data: items = [], loading, reload } = useLoader(loader)
  ```

### `useProjectId` — `src/hooks/useProjectId.ts`

```ts
export function useProjectId(): number { const { pid } = useParams(); return Number(pid) }
```
参数无效时返回 `NaN`；调用方用 `Number.isFinite(pid)` 检查（`ProjectLayout`）。

### `useHasAIConfig` — `src/hooks/useHasAIConfig.ts`

```ts
export function useHasAIConfig(): boolean | null  // store 尚未加载时为 null
```
读取 `useAIConfigStore()`；它**不会**触发加载——由 `AIConfigSelect`（渲染在每个生成按钮旁边）调用 `load()`。调用方仅在明确为 `false` 时禁用生成按钮（`disabled={hasConfig === false || …}`），因此加载期间按钮不会被阻塞。

---

## 数据请求

- 读取：`useLoader` + `api/<resource>.ts` 中的函数。跨页面缓存的读取（当前用户、AI 配置、当前项目）改用 store。
- 写入：在 `async` 处理函数中直接调用 API 函数，然后调用 `reload()` / `setProject()` / `refresh()`。
- 后台任务：`generateChapter` / `polishChapter` / `runWizard` 返回 202 `Task`（普通 JSON），随后 `useTasksStore.track(task)` + `useTaskStream().follow(task)`；订阅基于 `subscribeTask`（见下方场景）。
- 同步流式：`useStreamTask().start(runner)`，目前只有 `rewriteChapter`（`runGeneration` POST SSE）。

---

## 场景：SSE 客户端解析器

### 1. 范围 / 触发条件
任何对 `src/api/sse.ts`、`src/api/tasks.ts` 的 `subscribeTask`、流式端点（同步：`POST /api/v1/chapters/{id}/rewrite`；任务订阅：`GET /api/v1/tasks/{id}/stream`）或 `backend/app/api/sse.py` 中后端 SSE 帧格式的修改。改写端点是带 JSON body 的 POST，无法使用 `EventSource`；任务订阅虽然是 GET，也走 `fetch`（`getSSE`）以复用帧解析、`ApiError` 归一化和 401 处理，并能用 `AbortSignal` 精确断开。generate / polish / wizard **不再是 SSE**，返回 202 `Task`。

### 2. 签名
```ts
export interface SSEMessage { event: string; data: string }
export class SSEParser { feed(chunk: string): SSEMessage[]; flush(): SSEMessage[] }
export function postSSE(path: string, body: unknown, handlers: { onMessage: (msg: SSEMessage) => void }, signal?: AbortSignal): Promise<void>
export function getSSE(path: string, handlers: { onMessage: (msg: SSEMessage) => void }, signal?: AbortSignal): Promise<void>  // 两者共用 readSSE
export interface GenerationCallbacks<TDone> {
  onDelta: (text: string) => void; signal?: AbortSignal; parseDone?: (data: unknown) => TDone
  onEvent?: (event: string, data: unknown) => void   // delta/done/error 以外的事件（如 continue）
}
// src/api/chapters.ts / wizard.ts —— 创建任务（202）
export function generateChapter(cid, body: GenerateChapterRequest): Promise<Task>
export function polishChapter(cid, body: PolishChapterRequest): Promise<Task>
export function runWizard(pid, kind, body: WizardRequest): Promise<Task>
export function parseChapterDone(result: unknown): ChapterDone | null
export function parseWizardDone<K>(kind: K, result: unknown): WizardItemMap[K][] | null
// src/api/tasks.ts
export type TaskEnd = { status: 'succeeded'; task: Task | null; result: unknown } | { status: 'failed' | 'cancelled' | 'interrupted'; task: Task | null; detail: string }
export interface TaskStreamHandlers { onSnapshot?(task: Task | null, text); onDelta?(text); onContinue?(e: ContinueEvent); onProgress?(p: TaskProgress); onReconnect?(attempt, delayMs) }
export function subscribeTask(taskId, handlers, signal?, options?: { maxRetries? = 8; baseDelayMs? = 1000; maxDelayMs? = 15000; stableMs? = 5000; sleep?; now? }): Promise<TaskEnd>
export function reconnectDelay(attempt, base = 1000, max = 15000): number   // min(base·2^(n-1), max)
// src/api/chapterVersions.ts
export function rewriteChapter(cid, body: RewriteRequest, onDelta, signal?): Promise<RewriteDone>
export async function runGeneration<TDone>(path: string, body: unknown, cb: GenerationCallbacks<TDone>): Promise<TDone>
export class StreamError extends Error {}           // name = 'StreamError'
export function isAbortError(err: unknown): boolean // DOMException 且 name === 'AbortError'
```

### 3. 契约
- 请求：`fetch(buildUrl(path), { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' }, body: JSON.stringify(body ?? {}), credentials: 'include', signal })`。
- 传输格式（后端 `format_sse`）：第一帧是注释 `: stream-start\n\n`，之后是 `event: <delta|done|error>\ndata: <JSON>\n\n`。data 始终是 JSON，因此正文中的换行绝不会破坏帧。
- 事件 payload：
  - `delta` → `{ "text": string }` → `cb.onDelta(text)`（空文本跳过）。
  - `done` → 局部改写：`{ text, start, end, original, before, log_id }`（`RewriteDone`，偏移为码点）。先暂存，流结束后返回。（章节 `{chapter, warning?}` 与向导 `{kind, items}` 现在是任务 `done.result`，见下方「任务订阅」。）
  - `error` → `{ "detail": string }` → 流结束后 `throw new StreamError(detail)`（兜底为 `生成失败`）。
  - 其他事件 → `cb.onEvent?.(event, data)`（改写目前没有额外事件；整章生成的 `continue` → `{ round, words, target }` 现在经任务订阅到达，`subscribeTask` 用 `isContinueEvent` 校验后转给 `onContinue`，round 从 1 起最多 2）。续写轮首个分块前后端会先发一个 `delta {"text":"\n\n"}`，前端无需特殊处理。不认识的事件被忽略，所以新增事件对旧代码兼容。
- `useStreamTask` 不感知额外事件：调用方在 runner 闭包里把自己的回调（如 `setContinueInfo`）传给 API 函数，并在 `start` 前后自行复位。
- 解析规则：接受任意 chunk 切分；行尾可为 LF、CRLF 或单独的 CR；chunk 末尾的 `\r` 会保留到下一个 chunk（可能是 CRLF 的一半）；以 `:` 开头的行被忽略；`field: value` 去掉一个前导空格；多行 `data:` 以 `\n` 拼接；事件名为空时默认为 `message`；`flush()` 派发最后一个未以空行结尾的事件。`runGeneration` 会忽略非 JSON 的 `data`。
- 解码器：`TextDecoder('utf-8')` 配合 `{ stream: true }`，使跨 chunk 拆分的多字节中文字符能正确解码；EOF 时执行最终的 `decoder.decode()` + `parser.flush()`；在 `finally` 中执行 `reader.releaseLock()`。

- **任务订阅（`subscribeTask`）**：`GET /tasks/{id}/stream`，事件 `snapshot {task, text}`（每次（重）连接的首帧，**覆盖**本地文本，因此重连不重复不遗漏）/ `delta {text}` / `continue` / `progress {status, progress, stage, queue_position?}` / `done {task, result}` / `error {task, detail}`；`: ping` 心跳注释被解析器忽略。`done` → `{status: 'succeeded', result}`；`error` 按 `task.status` 区分 `cancelled` / `interrupted`，其余（含 `task` 为 null）为 `failed`。
- **重连**：流结束但没有 `done`/`error`（`StreamError`）、网络错误（`ApiError(0)` / `TypeError`）、5xx、2xx 但非流式 → 按 `reconnectDelay` 退避后重连，触发 `onReconnect`；只有收到 `snapshot` **且**该连接存活满 `stableMs`（5 秒）后才断开，才把连续失败计数清零——「连上即断」反复循环时计数继续累计、退避加长并最终放弃，不会无限 ~1s 重连；连续失败超过 `maxRetries` 抛最后一次错误。4xx（401 / 404 `任务不存在`）不重连，直接抛 `ApiError`。中止（含等待重连期间）抛 `AbortError`，只断开订阅、不影响任务。

### 4. 校验与错误矩阵

| 情况 | 暴露位置 | 前端结果 |
|-----------|------------------|-----------------|
| 无 AI 配置 / 配置错误（后端 `BadRequestError`，如 `尚未配置 AI 模型，请先在「模型配置」中添加并设为默认`） | 流开始前的 HTTP 400 JSON | 经 `handleErrorResponse` 得到 `ApiError(400, detail)` |
| 章节已处于 `generating`（`该章节正在生成中，请稍候再试`） | HTTP 409 JSON | `ApiError(409, detail)` |
| 对空正文润色（`章节正文为空，无法润色`） | HTTP 400 JSON | `ApiError(400, detail)` |
| 章节/项目不属于当前用户或不存在 | HTTP 404 JSON | `ApiError(404, detail)` |
| 会话过期 | HTTP 401 JSON | 先 `notifyUnauthorized()`，再 `ApiError(401, …)` |
| 收到响应前网络失败 | `fetch` reject | `ApiError(0, '网络错误，无法连接服务器')` |
| 2xx 但不是 `text/event-stream` 或没有 body | content-type 检查 | `ApiError(status, '服务器未返回流式响应')` |
| 流中途模型/提供商出错 | `event: error` | `StreamError(detail)` |
| 流结束但没有 `done` | EOF | `StreamError('连接意外中断，生成未完成')` |
| 用户停止 / 组件卸载 | `AbortController.abort()` | 重新抛出 `DOMException AbortError` → `useStreamTask` 返回 `{ status: 'aborted' }` |

以上所有情况都通过 `errorMessage(err)`（`ApiError.detail` 或 `Error.message`）到达 UI。后端在中止/出错时会回滚章节状态，因此编辑器随后会调用 `reloadChapter()`。

任务创建（generate / polish / wizard / retry）走普通 JSON：400（无配置 / 端点被拦截）、404、409（`该章节正在生成中，请稍候再试` / `已有任务在进行中，请等待其完成后再试`）都是 `ApiError`，由调用方 `message.error`。任务失败（`error` 事件）→ `TaskEnd.detail`（`useTaskStream.error`）；取消 → `status: 'cancelled'`，`detail = 任务已取消`，不当作错误；服务重启 → `interrupted`，`detail = 服务重启，任务已中断`。

### 5. 正例 / 基准 / 反例
- 正例：`: stream-start\n\nevent: delta\ndata: {"text":"你好"}\n\nevent: done\ndata: {}\n\n` → `[{delta, '{"text":"你好"}'}, {done, '{}'}]`。
- 基准：相同的帧每 3 个字符切分一次，或使用 `\r\n` 且 `\r` 与 `\n` 落在不同 chunk 中 → 得到完全相同的消息。
- 反例：`event: error\ndata: {"detail":"x"}` → `runGeneration` 抛出 `StreamError('x')`；只有 delta 而没有 `done` 的流 → `StreamError('连接意外中断，生成未完成')`；HTTP 409 → `ApiError`，绝不会是 `StreamError`。

### 6. 必需的测试
现有测试（`src/api/sse.test.ts`，用 `npm test` 运行）：
- `解析标准帧并忽略注释帧` —— 断言注释不产生消息，且 delta/done 按顺序返回。
- `处理跨 chunk 截断` —— 3 字符的 chunk 恰好产出原来的两条消息。
- `处理 CRLF 且 \r 与 \n 被拆在两个 chunk` —— 断言只有一个 data 为 `{"detail":"x"}` 的 `error` 事件（没有因空行误派发的事件）。
- `多行 data 以换行拼接，缺省事件名为 message` —— `data: a\ndata: b` → `{ event: 'message', data: 'a\nb' }`。
- `流结束时 flush 未以空行结尾的事件` —— 未结束的 `done` 由 `flush()` 发出。
- `runGeneration`（stub `fetch` 返回 `ReadableStream`）：delta 累积 + `continue` 走 `onEvent` + done 载荷含 `warning`，并断言 URL 前缀 / `credentials: 'include'` / 请求体；`generateChapter` 把 `continue` 转给 `onContinue`；`error` 事件与缺少 `done` → `StreamError`；409 → `ApiError(status, detail)`；已中止 signal → `isAbortError`。

`src/api/tasks.test.ts`（`subscribeTask`）：GET + `credentials` + 事件分派与 `done.result`；断线（流无终止 + 网络错误）→ 退避 1s/2s 重连、snapshot 覆盖后文本无重复；`error` 按 `task.status` 区分且容忍 `task: null`；404 不重连；5xx 超过上限抛错；「连上即断」循环不清零计数并最终抛错、稳定连接（≥ `stableMs`）后断开才清零；等待重连时中止 → `AbortError`；`reconnectDelay` / `queueLabel` / `taskLink` / list 与 clearFinished 的查询串。

修改 `postSSE`/`getSSE`/`runGeneration`/`subscribeTask` 时，新增使用 stub `fetch`（返回 `ReadableStream`）的测试，断言：非 2xx → 带 `status`/`detail` 的 `ApiError`；`error` 事件 → `StreamError`；缺少 `done` → `StreamError`；已中止的 signal → `isAbortError(err) === true`。

### 7. 错误写法 vs 正确写法
```ts
// 错误：EventSource 仅支持 GET，无法发送端点所需的 JSON 请求体
const es = new EventSource(`/api/v1/chapters/${cid}/generate`)

// 错误：对每个 chunk 按 '\n\n' 切分，会丢失跨 chunk 截断的帧，且遇到 CRLF 会出错
chunk.split('\n\n').forEach(handleFrame)

// 错误：把流开始前的 409 当作流错误处理，会向调用方隐藏状态码
if (!resp.ok) throw new StreamError('生成失败')

// 正确（src/api/sse.ts）
if (!resp.ok) await handleErrorResponse(path, resp)        // 携带后端 detail 的 ApiError
for (const msg of parser.feed(decoder.decode(value, { stream: true }))) handlers.onMessage(msg)
```

---

## 命名约定

- 文件名为 `useXxx.ts`，使用具名导出，每个文件一个 hook。
- Hook 返回普通对象（`{ text, running, error, start, stop, reset }`、`{ data, loading, error, reload, setData }`），由调用方解构。
- 结果类型与 hook 一同导出（`StreamOutcome<T>`）。

---

## 常见错误

- 给 `useLoader` 传内联箭头函数 → 无限重复请求。用 `useCallback` 包裹并写对依赖。
- 用 `task.error` 处理中止——中止被刻意设计为不算错误；应处理 `outcome.status === 'aborted'` 并给出提示信息。
- 忘记把 runner 中的 `signal` 传入 API 函数——停止和卸载将无法取消请求（后端也会继续生成）。
- 把 `useHasAIConfig() === null` 当作"没有配置"——它的含义是"尚未加载"。
