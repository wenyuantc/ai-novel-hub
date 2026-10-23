# Type Safety

> Type safety patterns in this project.

---

## Overview

TypeScript ~5.8 in strict mode (`frontend/tsconfig.app.json`: `strict`, `noUnusedLocals`, `noUnusedParameters`, `noFallthroughCasesInSwitch`, `verbatimModuleSyntax`). ESLint enforces `'@typescript-eslint/no-explicit-any': 'error'`. There is no runtime validation library (no Zod/Yup): response bodies are trusted to match `src/api/types.ts`, which mirrors the backend Pydantic schemas. Untrusted shapes (error bodies, SSE payloads) are narrowed with `unknown` + type guards.

---

## Type Organization

- **All wire types live in `src/api/types.ts`** (header comment: `与后端 app/schemas/*.py 对齐的类型定义`). One TS interface per Pydantic model: `*Out` → plain name (`AIConfig`, `Project`, `Chapter`), `*Create`/`*Update`/`*Request` → same name.
- Field names stay `snake_case` exactly as on the wire. `datetime` fields are `string`. Optional-with-default request fields are `?:`; nullable fields are `| null`.
- Enums are string-literal unions copied from backend `Literal[...]`:

  | TS type | Values | Backend source |
  |---------|--------|----------------|
  | `ProviderName` | `'openai_compat' \| 'anthropic' \| 'gemini'` | `schemas/ai_config.py` |
  | `ChapterStatus` | `'draft' \| 'generating' \| 'done'` | `schemas/chapter.py` |
  | `ChapterUpdate.status` | `'draft' \| 'done' \| null` (never `generating`) | `ChapterUpdate.status: Literal["draft", "done"] \| None` |
  | `ProjectStatus` | `'planning' \| 'writing' \| 'completed'` | `schemas/project.py` |
  | `WizardKind` | `'outline' \| 'characters' \| 'world'` | wizard route `/projects/{pid}/wizard/{kind}` |

- UI label maps are typed with `Record<Union, …>` so adding an enum value is a compile error until labelled: `PROJECT_STATUS: Record<ProjectStatus, { label; color }>`, `CHAPTER_STATUS: Record<ChapterStatus, …>`, `PROVIDER_LABELS: Record<AIConfig['provider'], string>`, `WIZARD_KIND_LABELS: Record<WizardKind, string>`.
- Type-level maps tie generic parameters to payloads: `WizardItemMap { outline: Outline; characters: Character; world: WorldSetting }` → `runWizard<K>(…): Promise<WizardItemMap[K][]>`.
- Component-local form shapes (`FormValues`) stay in the component file.

- Auth/admin types: `User.is_admin` mirrors `UserOut.is_admin`; `Page<T> = { items: T[]; total: number }` mirrors `AdminUserList`; `AdminUser` mirrors `AdminUserOut` (`id, username, is_admin, is_active, created_at, project_count`); `AdminUserCreate`, `AdminUserUpdate` (`is_admin?`, `is_active?`), `ResetPasswordRequest`, `ChangePasswordRequest`. Backend contract: `.trellis/spec/backend/auth-admin.md`.
- Shared form rules live in `src/utils/validation.ts` (`usernameRules` 3–32 no whitespace, `passwordRules` 6–72, `confirmPasswordRules(field = 'password')`). Login/register, change-password, admin create and reset-password all import them — never re-declare the limits in a page.

### PATCH semantics (must match backend services)

| Resource | Rule | Backend evidence |
|----------|------|------------------|
| Project, world/characters/outlines | Omitted **or** `null` = unchanged | `model_dump(exclude_unset=True)` + `if value is None: continue` (`project_service.update_project`, `story_service.update_owned`) |
| Chapter | Omitted/`null` = unchanged for `chapter_no`, `title`, `content`, `status`; `outline_id: null` sent explicitly **unlinks** the outline | `chapter_service.update_chapter` checks `"outline_id" in fields` |
| Chapter while `generating` | PATCH with `title`/`content` and no `status` → 409; `{ status: 'draft' }` alone is the reset path | `ConflictError("该章节正在生成中，请等待完成或先复位状态后再编辑")` |
| Admin user | `is_admin` / `is_active` omitted = unchanged; disabling bumps `token_version` (target's sessions → 401) | `admin_service.update_user` |
| AI config | `api_key` omitted or `''` keeps the stored key; `base_url` present (incl. `null`/`''`) is applied, `null` = provider default | `if data.api_key:` / `if "base_url" in fields:` in `ai_config_service.update_config` |

Frontend consequence (`AIConfigPage.handleSave`): `base_url` is always sent (`v.base_url?.trim() ? v.base_url.trim() : null`), `api_key` only when non-empty. `AIConfigUpdate = Partial<AIConfigCreate>`.

### Timestamps

Backend datetimes may be serialized without a timezone suffix. Always parse via `src/utils/time.ts`:

```ts
export function parseServerTime(value: string): dayjs.Dayjs {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return dayjs(hasZone ? value : `${value}Z`)   // naive -> UTC
}
export function formatTime(value: string | null | undefined, fmt = 'YYYY-MM-DD HH:mm'): string
```
`time.test.ts` asserts `'2026-10-22T08:00:00'` → `'2026-10-22T08:00:00.000Z'` and that `+08:00` / `Z` inputs keep their zone. Never call `dayjs(value)` or `new Date(value)` on a server timestamp directly.

---

## Validation

- **Input**: AntD `Form` rules mirror backend `Field` limits (e.g. wizard `InputNumber min={1} max={20}` for `volume_count`, `min={3} max={30}` for `character_count`, editor `target_words` `min={300} max={20000}`, instruction `max: 2000`; login username 3–32, password 6–72). Strings are `.trim()`med before sending. The backend remains authoritative and returns `{detail}` (422 detail starts with `请求参数校验失败：`).
- **Responses**: not validated at runtime; typed via `request<T>`.
- **Untrusted JSON**: narrowed from `unknown`:
  ```ts
  // src/api/client.ts
  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null
  }
  const data: unknown = JSON.parse(text)
  if (isRecord(data) && typeof data.detail === 'string') return data.detail
  ```
  `sse.ts` `readText`/`readDetail` do the same for SSE payloads; `runGeneration` accepts an optional `parseDone?: (data: unknown) => TDone` validator.

---

## Common Patterns

- `catch (err)` / `(err: unknown)` everywhere, converted with `errorMessage(err)`.
- Discriminated unions for results: `StreamOutcome<T>` (`status: 'done' | 'aborted' | 'error'`), `AuthStatus`.
- Generic API helpers: `request<T>`, `api.get<T>`, `makeCrud<TItem, TInput>`, `runGeneration<TDone>`.
- `import type { … }` for type-only imports (required by `verbatimModuleSyntax`).

---

## Scenario: API client contract

### 1. Scope / Trigger
Any change to `src/api/client.ts`, to `exportProject` / `parseContentDispositionFilename` in `src/api/projects.ts`, to backend error format (`backend/app/core/errors.py`), auth cookies, or the export endpoint (`GET /api/v1/projects/{id}/export`).

### 2. Signatures
```ts
export const API_BASE = '/api/v1'
export class ApiError extends Error { readonly status: number; readonly detail: string }
export function setUnauthorizedHandler(handler: (() => void) | null): void
export function buildUrl(path: string, query?: Record<string, QueryValue>): string
export async function readErrorDetail(resp: Response): Promise<string>
export async function handleErrorResponse(path: string, resp: Response): Promise<never>
export async function rawRequest(path: string, options?: RequestOptions): Promise<Response>
export async function request<T>(path: string, options?: RequestOptions): Promise<T>
export const api: { get<T>(path, query?); post<T>(path, body?); patch<T>(path, body); del(path): Promise<void> }
export function errorMessage(err: unknown): string
export function parseContentDispositionFilename(header: string | null): string | null
export async function exportProject(projectId: number, format: 'txt' | 'md', chapterIds?: number[]): Promise<string>
```

### 3. Contracts
- Every request uses `credentials: 'include'` (HttpOnly cookie session; no tokens in JS). Headers: `Accept: application/json`; `Content-Type: application/json` only when a body is given.
- `buildUrl` skips `null`/`undefined` query values and repeats keys for arrays.
- Non-2xx → `handleErrorResponse`: read body text, extract `detail` if it is a string (backend always sends `{"detail": "中文"}`), else fallback `请求失败（HTTP ${status}）`; on 401 call the registered handler unless the path starts with `/auth/login` or `/auth/me`; always throw `ApiError(status, detail)`.
- Network failure → `ApiError(0, '网络错误，无法连接服务器')`; `AbortError` is rethrown untouched.
- `request<T>`: 204 → `undefined`; empty body → `undefined`; otherwise `JSON.parse`.
- `App.tsx` registers `() => useAuthStore.getState().clear()`; `RequireAuth` then redirects to `/login`.
- Export: plain `fetch(buildUrl(path, { format, chapter_ids? }), { credentials: 'include' })` where `chapter_ids` is a comma-joined string (backend `export_service.parse_chapter_ids`). Response is a blob; filename from `Content-Disposition`, preferring RFC 5987 `filename*=UTF-8''<percent-encoded>` (backend sends `attachment; filename="novel.{ext}"; filename*=UTF-8''{quote(name)}`), falling back to plain `filename=`, then `novel.${format}`. Download via object URL + temporary `<a download>`, URL revoked after 1 s. Errors go through the same `handleErrorResponse`.

### 4. Validation & Error Matrix

| Input | Result |
|-------|--------|
| 2xx JSON | parsed `T` |
| 204 / empty body | `undefined` |
| 4xx/5xx with `{"detail":"…"}` | `ApiError(status, detail)` |
| 4xx/5xx with non-JSON or non-string `detail` | `ApiError(status, '请求失败（HTTP n）')` |
| 401 on any path except `/auth/login*`, `/auth/me*` | handler → `clear()` → redirect to login; still throws `ApiError(401, …)` |
| 401 on `/auth/login` (wrong password) or `/auth/me` (not logged in) | throws only; `LoginPage` shows detail, `init()` sets anonymous |
| `fetch` rejects | `ApiError(0, '网络错误，无法连接服务器')` |
| aborted signal | `DOMException` `AbortError` rethrown |
| Export `filename*=UTF-8''%E5%B0%8F%E8%AF%B4.txt` | `小说.txt` |
| Export header with malformed percent-encoding | falls back to plain `filename` |
| Export header missing | `novel.txt` / `novel.md` |

### 5. Good / Base / Bad Cases
- Good: `await chapterApi.update(pid, cid, { title, content })` resolves to `Chapter`; on failure `message.error(errorMessage(err))` shows the backend's Chinese detail.
- Base: `api.del('/projects/1')` → 204 → resolves `undefined`.
- Bad: PATCH content on a generating chapter → `ApiError(409, '该章节正在生成中，请等待完成或先复位状态后再编辑')`; any expired session → global logout instead of per-page handling.

### 6. Tests Required
No unit tests cover `client.ts`/`projects.ts` yet. When modifying them, add Vitest tests (stub `globalThis.fetch`) asserting:
- `fetch` was called with `credentials: 'include'` and URL prefixed by `/api/v1`.
- Error body `{"detail":"x"}` with 400 → `ApiError` with `status === 400`, `detail === 'x'`; non-JSON body → fallback detail.
- 401 on `/projects` invokes the registered handler once; 401 on `/auth/login` and `/auth/me` does not.
- 204 resolves `undefined`.
- `parseContentDispositionFilename`: `filename*=UTF-8''%E5%B0%8F%E8%AF%B4.txt` → `小说.txt`; only `filename="a.md"` → `a.md`; `null` → `null`.

### 7. Wrong vs Correct
```ts
// Wrong: bypasses cookie session, error normalization and the 401 hook
const r = await fetch('/api/v1/projects'); const data = await r.json()

// Wrong: shows "[object Object]" or English HTTP text instead of the backend detail
message.error(String(err))

// Wrong: reads the ASCII fallback first, so Chinese titles always download as novel.txt
/filename="?([^";]+)"?/.exec(header)

// Correct
const projects = await projectApi.list()
message.error(errorMessage(err))
parseContentDispositionFilename(resp.headers.get('content-disposition')) ?? `novel.${format}`
```

---

## Forbidden Patterns

- `any` (lint error). Use `unknown` + narrowing.
- Calling `fetch` directly outside `src/api/` — go through `api.*`, `postSSE`/`runGeneration`, or `exportProject`.
- Redeclaring wire types inside components; extend `types.ts` instead.
- Sending `status: 'generating'` in `ChapterUpdate` (type forbids it; backend rejects it).
- Parsing server timestamps without `parseServerTime`/`formatTime`.
- Broad `as` casts on server data. Existing narrow casts are accepted where the type system cannot follow: `undefined as T` / `JSON.parse(text) as T` in `request`, `result as WizardItemMap['world'][]` after a `kind` check in `WizardGenerator`, `e.target.value as ExportFormat` from a fixed radio group, and the `document.getElementById('root')!` non-null assertion in `main.tsx`.
