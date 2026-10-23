# State Management

> How state is managed in this project.

---

## Overview

Zustand 5 (`create<State>((set, get) => ({ … }))`) for a minimal set of cross-page state; React `useState` for everything else; server data is fetched per screen with `useLoader` (no query cache). No Redux, no Context providers besides AntD's `ConfigProvider`/`App`.

---

## State Categories

| Category | Where | Examples |
|----------|-------|----------|
| Session (global) | `stores/auth.ts` → `useAuthStore` | `status: 'unknown' \| 'authenticated' \| 'anonymous'`, `user`, `registrationEnabled`; actions `init`, `login`, `register`, `logout`, `clear` |
| User-scoped cache (global) | `stores/aiConfigs.ts` → `useAIConfigStore` | `configs`, `loaded`, `loading`; `load(force = false)`, `reset()` |
| Current project (global) | `stores/project.ts` → `useProjectStore` | `project`, `loading`, `error`; `load(id)`, `refresh()`, `setProject(p)`, `reset()` |
| Screen data (local) | `useLoader` in the page | world/characters/outlines/chapters lists, the open chapter, export chapter list |
| Form / UI state (local) | `useState`, `Form.useForm` | modal open flags, `editing`, `saving`, editor `title`/`content`, `aiConfigId`, test results on the AI config page |
| Stream state (local) | `useStreamTask` | `text`, `running`, `error`, abort controller |

---

## When to Use Global State

Only when two or more unrelated screens need the same data **and** it is user-scoped:

- `useAuthStore` — read by `RequireAuth`, `LoginPage`, `AppLayout`, `App`.
- `useAIConfigStore` — read by `AIConfigSelect` (every generate UI), `useHasAIConfig`, `AIConfigPage`. `load()` is idempotent: `if (get().loading || (get().loaded && !force)) return`. `AIConfigPage` uses `load(true)` after each mutation so selects elsewhere see the change.
- `useProjectStore` — loaded by `ProjectLayout` (`load(pid)`), shown in the breadcrumb/overview; panels call `refresh()` (silent, errors swallowed) after changing chapters so `chapter_count`/`total_words` update; `OverviewPanel` uses `setProject(updated)` after PATCH. `load(id)` keeps the current project only if `get().project?.id === id`, so switching projects never flashes the old one (`ProjectLayout` also renders a spinner while `project.id !== pid`).

Everything else stays local. Do not put lists like chapters or outlines in a store.

### Clearing user-scoped state (logout / 401 / account switch)

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

- 401 from any API call (except `/auth/login*` and `/auth/me*`, see `shouldRedirectOn401` in `client.ts`) → `clear()` → `status: 'anonymous'` → `RequireAuth` redirects to `/login` with `state.from`.
- `logout()` calls the API and, in `finally`, sets `anonymous` even if the request fails.
- Any change of `user?.id` (logout, 401, login as another user) resets the AI config and project stores. A new user-scoped store **must** add its `reset()` to this effect.
- `init()` runs `/auth/me` and `/auth/config` with `Promise.allSettled`; failure of `/auth/me` means anonymous (401 and network errors are not logged).

---

## Server State

There is no client-side cache invalidation framework. Rules in use:

- After a successful mutation, refetch the affected list (`reload()`) or write the server response back (`applyServerChapter(updated)`, `setProject(updated)`). Never patch local state optimistically before the server responds.
- Server responses are the source of truth for derived fields (`word_count`, `status`, `updated_at`); the editor shows a client-side `countWords` only for live typing/streaming and replaces it with the server chapter after save.
- After a failed or aborted generation, `reloadChapter()` syncs only server `status` (backend reverts it) and keeps local text.

---

## Common Mistakes

- Adding a user-scoped store without resetting it on `userId` change — the next user would see the previous user's data.
- Calling `useAIConfigStore().load()` expecting a refetch — it is a no-op once loaded; use `load(true)`.
- Selecting the whole store when one field is needed in frequently re-rendering components; prefer selectors (`useAuthStore((s) => s.user)`, `useProjectStore((s) => s.refresh)`), as `App`, `AppLayout` and the editor do.
- Handling 401 in individual pages — it is centralized via `setUnauthorizedHandler`; pages just show `errorMessage(err)`.
