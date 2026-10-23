# Hook Guidelines

> How hooks are used in this project.

---

## Overview

There is no data-fetching library (no React Query/SWR). Four small custom hooks in `src/hooks/` cover everything: one-shot async loading (`useLoader`), streaming AI tasks (`useStreamTask`), route param parsing (`useProjectId`) and a derived store selector (`useHasAIConfig`). Global cached data lives in Zustand stores (see [state-management.md](./state-management.md)).

---

## Custom Hook Patterns

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

- Owns one `AbortController` in a ref. `start` aborts any previous controller, creates a new one, clears `text`/`error`, sets `running`, and appends each delta (`setText((prev) => prev + delta)`).
- Never throws: returns a discriminated `StreamOutcome`. Abort (`isAbortError(err) || controller.signal.aborted`) → `{ status: 'aborted' }` without setting `error`; other errors → `setError(errorMessage(err))` and `{ status: 'error', message }`.
- `finally` only clears `running` if the controller is still the current one (a newer `start` wins the race).
- Aborts on unmount: `useEffect(() => () => controllerRef.current?.abort(), [])`.
- Usage — the runner receives the hook's `onDelta`/`signal` and passes them to the API function:
  ```ts
  // src/components/WizardGenerator.tsx
  const outcome = await task.start((onDelta, signal) => runWizard(projectId, kind, body, onDelta, signal))
  if (outcome.status === 'done') { … } else if (outcome.status === 'aborted') { message.info('已停止生成，原有数据未改变') }
  ```
  Errors are rendered from `task.error` (`<Alert type="error" message="生成失败" description={task.error} />`).

### `useLoader` — `src/hooks/useLoader.ts`

```ts
export function useLoader<T>(fn: () => Promise<T>): {
  data: T | undefined; loading: boolean; error: string | null
  reload: () => void
  setData: (updater: (prev: T | undefined) => T) => void
}
```

- Runs `fn` in an effect keyed by `[fn, version]`; an `alive` flag drops results from stale runs.
- `fn` **must** be memoized by the caller with `useCallback`, otherwise it refetches every render.
- Errors are normalized with `errorMessage(err)`; on error the previous `data` is kept.
- `reload()` sets `loading: true` and bumps `version`.
  ```ts
  // src/pages/project/WorldPanel.tsx
  const loader = useCallback(() => worldApi.list(pid), [pid])
  const { data: items = [], loading, reload } = useLoader(loader)
  ```

### `useProjectId` — `src/hooks/useProjectId.ts`

```ts
export function useProjectId(): number { const { pid } = useParams(); return Number(pid) }
```
Returns `NaN` for a bad param; callers check `Number.isFinite(pid)` (`ProjectLayout`).

### `useHasAIConfig` — `src/hooks/useHasAIConfig.ts`

```ts
export function useHasAIConfig(): boolean | null  // null while the store is not loaded
```
Reads `useAIConfigStore()`; it does **not** trigger loading — `AIConfigSelect` (rendered next to every generate button) calls `load()`. Callers disable generate buttons only on an explicit `false` (`disabled={hasConfig === false || …}`), so the button is not blocked during loading.

---

## Data Fetching

- Reads: `useLoader` + `api/<resource>.ts` functions. Cross-page cached reads (current user, AI configs, current project) use stores instead.
- Writes: call the API function directly in an `async` handler, then `reload()` / `setProject()` / `refresh()`.
- Streaming: `useStreamTask().start(runner)` where `runner` calls `generateChapter` / `polishChapter` / `runWizard`, all built on `runGeneration` in `src/api/sse.ts` (scenario below).

---

## Scenario: SSE client parser

### 1. Scope / Trigger
Any change to `src/api/sse.ts`, to a streaming endpoint (`POST /api/v1/chapters/{id}/generate`, `/chapters/{id}/polish`, `/projects/{pid}/wizard/{kind}`), or to the backend SSE framing in `backend/app/api/sse.py`. The backend endpoints are POST with a JSON body, so `EventSource` (GET only, no body) cannot be used — streaming is `fetch` + `ReadableStream` + a hand-written parser.

### 2. Signatures
```ts
export interface SSEMessage { event: string; data: string }
export class SSEParser { feed(chunk: string): SSEMessage[]; flush(): SSEMessage[] }
export async function postSSE(path: string, body: unknown, handlers: { onMessage: (msg: SSEMessage) => void }, signal?: AbortSignal): Promise<void>
export interface GenerationCallbacks<TDone> { onDelta: (text: string) => void; signal?: AbortSignal; parseDone?: (data: unknown) => TDone }
export async function runGeneration<TDone>(path: string, body: unknown, cb: GenerationCallbacks<TDone>): Promise<TDone>
export class StreamError extends Error {}           // name = 'StreamError'
export function isAbortError(err: unknown): boolean // DOMException && name === 'AbortError'
```

### 3. Contracts
- Request: `fetch(buildUrl(path), { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' }, body: JSON.stringify(body ?? {}), credentials: 'include', signal })`.
- Wire format (backend `format_sse`): first frame is the comment `: stream-start\n\n`, then `event: <delta|done|error>\ndata: <JSON>\n\n`. Data is always JSON so newlines in prose never break frames.
- Event payloads:
  - `delta` → `{ "text": string }` → `cb.onDelta(text)` (empty text skipped).
  - `done` → chapter: `{ "chapter": ChapterOut }`; wizard: `{ "kind": K, "items": [...] }`. Stored, returned after the stream ends.
  - `error` → `{ "detail": string }` → after the stream ends, `throw new StreamError(detail)` (fallback `生成失败`).
- Parser rules: accepts arbitrary chunk splits; line endings LF, CRLF or lone CR; a trailing `\r` at chunk end is held until the next chunk (could be half of CRLF); lines starting with `:` are ignored; `field: value` strips one leading space; multiple `data:` lines join with `\n`; empty event name defaults to `message`; `flush()` dispatches a final event not terminated by a blank line. Non-JSON `data` is ignored by `runGeneration`.
- Decoder: `TextDecoder('utf-8')` with `{ stream: true }` so multi-byte Chinese characters split across chunks decode correctly; final `decoder.decode()` + `parser.flush()` on EOF; `reader.releaseLock()` in `finally`.

### 4. Validation & Error Matrix

| Situation | Where it surfaces | Frontend result |
|-----------|------------------|-----------------|
| No AI config / bad config (backend `BadRequestError`, e.g. `尚未配置 AI 模型，请先在「模型配置」中添加并设为默认`) | HTTP 400 JSON before streaming | `ApiError(400, detail)` via `handleErrorResponse` |
| Chapter already `generating` (`该章节正在生成中，请稍候再试`) | HTTP 409 JSON | `ApiError(409, detail)` |
| Polish on empty content (`章节正文为空，无法润色`) | HTTP 400 JSON | `ApiError(400, detail)` |
| Chapter/project not owned or missing | HTTP 404 JSON | `ApiError(404, detail)` |
| Session expired | HTTP 401 JSON | `notifyUnauthorized()` then `ApiError(401, …)` |
| Network failure before response | `fetch` rejects | `ApiError(0, '网络错误，无法连接服务器')` |
| 2xx but not `text/event-stream` or no body | content-type check | `ApiError(status, '服务器未返回流式响应')` |
| Model/provider error mid-stream | `event: error` | `StreamError(detail)` |
| Stream ends without `done` | EOF | `StreamError('连接意外中断，生成未完成')` |
| User stops / component unmounts | `AbortController.abort()` | `DOMException AbortError` rethrown → `useStreamTask` returns `{ status: 'aborted' }` |

All of these reach the UI through `errorMessage(err)` (`ApiError.detail` or `Error.message`). Backend reverts chapter status on abort/error, so the editor calls `reloadChapter()` afterwards.

### 5. Good / Base / Bad Cases
- Good: `: stream-start\n\nevent: delta\ndata: {"text":"你好"}\n\nevent: done\ndata: {}\n\n` → `[{delta, '{"text":"你好"}'}, {done, '{}'}]`.
- Base: the same frames split every 3 characters, or with `\r\n` where `\r` and `\n` land in different chunks → identical messages.
- Bad: `event: error\ndata: {"detail":"x"}` → `runGeneration` throws `StreamError('x')`; stream with only deltas and no `done` → `StreamError('连接意外中断，生成未完成')`; HTTP 409 → `ApiError`, never a `StreamError`.

### 6. Tests Required
Existing (`src/api/sse.test.ts`, run with `npm test`):
- `解析标准帧并忽略注释帧` — assert the comment produces no message and delta/done are returned in order.
- `处理跨 chunk 截断` — 3-char chunks yield exactly the two original messages.
- `处理 CRLF 且 \r 与 \n 被拆在两个 chunk` — assert a single `error` event with data `{"detail":"x"}` (no spurious empty-line dispatch).
- `多行 data 以换行拼接，缺省事件名为 message` — `data: a\ndata: b` → `{ event: 'message', data: 'a\nb' }`.
- `流结束时 flush 未以空行结尾的事件` — unterminated `done` is emitted by `flush()`.

When changing `postSSE`/`runGeneration`, add tests with a stubbed `fetch` returning a `ReadableStream`, asserting: non-2xx → `ApiError` with `status`/`detail`; `error` event → `StreamError`; missing `done` → `StreamError`; aborted signal → `isAbortError(err) === true`.

### 7. Wrong vs Correct
```ts
// Wrong: EventSource is GET-only and cannot send the JSON request body the endpoints require
const es = new EventSource(`/api/v1/chapters/${cid}/generate`)

// Wrong: splitting each chunk on '\n\n' loses frames cut across chunks and breaks on CRLF
chunk.split('\n\n').forEach(handleFrame)

// Wrong: treating a pre-stream 409 as a stream error hides the status code from callers
if (!resp.ok) throw new StreamError('生成失败')

// Correct (src/api/sse.ts)
if (!resp.ok) await handleErrorResponse(path, resp)        // ApiError with backend detail
for (const msg of parser.feed(decoder.decode(value, { stream: true }))) handlers.onMessage(msg)
```

---

## Naming Conventions

- Files `useXxx.ts`, named exports, one hook per file.
- Hooks return plain objects (`{ text, running, error, start, stop, reset }`, `{ data, loading, error, reload, setData }`), destructured by callers.
- Outcome/result types are exported next to the hook (`StreamOutcome<T>`).

---

## Common Mistakes

- Passing an inline arrow to `useLoader` → infinite refetch loop. Wrap in `useCallback` with the right deps.
- Using `task.error` for aborts — aborts are deliberately not errors; handle `outcome.status === 'aborted'` with an info message.
- Forgetting to forward `signal` from the runner into the API function — Stop and unmount would no longer cancel the request (and the backend would keep generating).
- Treating `useHasAIConfig() === null` as "no config" — it means "not loaded yet".
