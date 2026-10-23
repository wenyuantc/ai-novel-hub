# Hook 规范

> 本项目中 hook 的使用方式。

---

## 概述

项目没有使用数据请求库（没有 React Query/SWR）。`src/hooks/` 中的四个小型自定义 hook 覆盖了全部场景：一次性异步加载（`useLoader`）、流式 AI 任务（`useStreamTask`）、路由参数解析（`useProjectId`）以及派生的 store 选择器（`useHasAIConfig`）。全局缓存数据存放在 Zustand store 中（见 [state-management.md](./state-management.md)）。

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
- 流式：`useStreamTask().start(runner)`，其中 `runner` 调用 `generateChapter` / `polishChapter` / `runWizard`，它们都基于 `src/api/sse.ts` 中的 `runGeneration` 构建（见下方场景）。

---

## 场景：SSE 客户端解析器

### 1. 范围 / 触发条件
任何对 `src/api/sse.ts`、流式端点（`POST /api/v1/chapters/{id}/generate`、`/chapters/{id}/polish`、`/projects/{pid}/wizard/{kind}`）或 `backend/app/api/sse.py` 中后端 SSE 帧格式的修改。后端端点是带 JSON body 的 POST，因此无法使用 `EventSource`（仅支持 GET，不能带 body）——流式通过 `fetch` + `ReadableStream` + 手写解析器实现。

### 2. 签名
```ts
export interface SSEMessage { event: string; data: string }
export class SSEParser { feed(chunk: string): SSEMessage[]; flush(): SSEMessage[] }
export async function postSSE(path: string, body: unknown, handlers: { onMessage: (msg: SSEMessage) => void }, signal?: AbortSignal): Promise<void>
export interface GenerationCallbacks<TDone> {
  onDelta: (text: string) => void; signal?: AbortSignal; parseDone?: (data: unknown) => TDone
  onEvent?: (event: string, data: unknown) => void   // delta/done/error 以外的事件（如 continue）
}
// src/api/chapters.ts
export function generateChapter(cid, body, onDelta, signal?, onContinue?: (e: ContinueEvent) => void): Promise<ChapterDone>
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
  - `done` → 章节：`{ "chapter": ChapterOut, "warning"?: string }`（`ChapterDone`；`generateChapter` 返回整个载荷，`polishChapter` 仍只返回 `Chapter`）；局部改写：`{ text, start, end, original, before, log_id }`（`RewriteDone`，偏移为码点）；向导：`{ "kind": K, "items": [...] }`。先暂存，流结束后返回。
  - `error` → `{ "detail": string }` → 流结束后 `throw new StreamError(detail)`（兜底为 `生成失败`）。
  - 其他事件（目前只有整章生成的 `continue` → `{ round, words, target }`，每轮自动续写开始前，round 从 1 起最多 2）→ `cb.onEvent?.(event, data)`；`generateChapter` 用类型守卫校验后转给 `onContinue`。续写轮首个分块前后端会先发一个 `delta {"text":"\n\n"}`，前端无需特殊处理。不认识的事件被忽略，所以新增事件对旧代码兼容。
- `useStreamTask` 不感知额外事件：调用方在 runner 闭包里把自己的回调（如 `setContinueInfo`）传给 API 函数，并在 `start` 前后自行复位。
- 解析规则：接受任意 chunk 切分；行尾可为 LF、CRLF 或单独的 CR；chunk 末尾的 `\r` 会保留到下一个 chunk（可能是 CRLF 的一半）；以 `:` 开头的行被忽略；`field: value` 去掉一个前导空格；多行 `data:` 以 `\n` 拼接；事件名为空时默认为 `message`；`flush()` 派发最后一个未以空行结尾的事件。`runGeneration` 会忽略非 JSON 的 `data`。
- 解码器：`TextDecoder('utf-8')` 配合 `{ stream: true }`，使跨 chunk 拆分的多字节中文字符能正确解码；EOF 时执行最终的 `decoder.decode()` + `parser.flush()`；在 `finally` 中执行 `reader.releaseLock()`。

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

修改 `postSSE`/`runGeneration` 时，新增使用 stub `fetch`（返回 `ReadableStream`）的测试，断言：非 2xx → 带 `status`/`detail` 的 `ApiError`；`error` 事件 → `StreamError`；缺少 `done` → `StreamError`；已中止的 signal → `isAbortError(err) === true`。

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
