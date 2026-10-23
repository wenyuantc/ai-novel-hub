# Quality Guidelines

> Code quality standards for frontend development.

---

## Overview

The quality gate is three npm scripts run from `frontend/`:

```bash
npm run lint && npm run build && npm test
```

- `lint` → `eslint .` (flat config `eslint.config.js`: `@eslint/js` recommended, `typescript-eslint` recommended, `eslint-plugin-react-hooks` flat recommended, `react-refresh/only-export-components` warn with `allowConstantExport`, `@typescript-eslint/no-explicit-any: error`).
- `build` → `tsc -b && vite build` (type-check is part of the build; strict tsconfig with unused locals/params as errors).
- `test` → `vitest run` (`environment: 'node'`, `include: ['src/**/*.test.ts']`).

### Dev server proxy (`vite.config.ts`)

`/api` is proxied to `process.env.VITE_BACKEND_URL ?? 'http://localhost:8000'` with `changeOrigin: true`. For `text/event-stream` responses the proxy rewrites headers so nothing buffers or compresses the stream:

```ts
proxy.on('proxyRes', (proxyRes) => {
  if (String(proxyRes.headers['content-type'] ?? '').startsWith('text/event-stream')) {
    proxyRes.headers['cache-control'] = 'no-cache, no-transform'
    proxyRes.headers['x-accel-buffering'] = 'no'
  }
})
```
The backend sends the same headers (`SSE_HEADERS` in `backend/app/api/sse.py`). Keep both sides in sync if the proxy or a reverse proxy is changed; otherwise generation text arrives in one burst at the end.

### Bundle size

`vite build` currently emits a single JS chunk of about 1.26 MB (`dist/assets/index-*.js` ≈ 1,260 kB, ≈ 393 kB gzip) because AntD and all pages are bundled together and no route uses `React.lazy` / dynamic `import()`. `build.chunkSizeWarningLimit` is raised to `1500` (kB) so this does not warn. If the chunk grows past the limit, introduce route-level lazy loading in `router.tsx` (or `manualChunks`) rather than raising the limit again.

---

## Forbidden Patterns

- `any` — lint error; use `unknown` and narrow.
- Direct `fetch` in pages/components — use `src/api/*` (keeps `credentials: 'include'`, `ApiError`, 401 handling).
- `EventSource` for AI streams — endpoints are POST; use `runGeneration`.
- Static AntD `message` / `Modal.confirm` — use `App.useApp()`.
- Raw `dayjs(serverTime)` / `new Date(serverTime)` — use `formatTime` / `parseServerTime`.
- Inline (non-memoized) loader functions passed to `useLoader`.
- Hard-coded `/api/v1` paths outside `client.ts`.
- Silently swallowing errors in user actions — show `message.error(errorMessage(err))` (the only intentional silent catches are `useProjectStore.refresh()` and the editor's `reloadChapter()`).

---

## Required Patterns

- Async handlers: `setX(true)` → `try { await …; message.success(…) } catch (err) { message.error(errorMessage(err)) } finally { setX(false) }` (see `WorldPanel.handleSave`, `ExportModalInner.handleExport`).
- Fire-and-forget promises are marked with `void` (`void load()`, `void refreshProject()`, `onClick={() => void handleSave()}`).
- Every streaming UI uses `useStreamTask` and forwards `signal`; provide a visible Stop button while `running`.
- Disable conflicting controls while busy (`disabled={task.running}`, `loading={saving}`, `confirmLoading`).
- User-visible strings are Simplified Chinese; code comments are Chinese JSDoc one-liners as in existing files.
- New user-scoped stores reset on `userId` change in `App.tsx`.

---

## Testing Requirements

- Pure logic (parsers, formatters, grouping) gets co-located `*.test.ts` Vitest tests. Current suites: `src/api/sse.test.ts` (5 cases, SSE parser) and `src/utils/time.test.ts` (2 cases, UTC parsing) — 7 tests total, all must pass.
- Test environment is `node` (no jsdom); there are no component tests. Code that needs DOM/`fetch` is tested by stubbing globals.
- Changes to `sse.ts` or `client.ts` must follow the "Tests Required" lists in [hook-guidelines.md](./hook-guidelines.md#scenario-sse-client-parser) and [type-safety.md](./type-safety.md#scenario-api-client-contract).
- Test names may be Chinese, matching existing suites (`it('处理跨 chunk 截断', …)`).

---

## Code Review Checklist

- [ ] `npm run lint && npm run build && npm test` passes in `frontend/`.
- [ ] New/changed wire types in `src/api/types.ts` match `backend/app/schemas/*.py` (field names, nullability, `Literal` enums), and label `Record`s cover every enum value.
- [ ] PATCH bodies respect backend semantics (omit to keep; empty `api_key` keeps key; never `status: 'generating'`).
- [ ] All network calls go through `src/api/`; errors shown via `errorMessage`.
- [ ] Streaming: `signal` forwarded, Stop button present, abort handled as `aborted` not error, inputs read-only/disabled while running.
- [ ] Chapter editor invariants intact: auto-save before AI, Ctrl/Cmd+S, `useBlocker` + `beforeunload` guard, stuck-`generating` reset with save disabled.
- [ ] Server timestamps rendered with `formatTime`.
- [ ] Any new user-scoped global state is reset on logout/401.
- [ ] Bundle still under `chunkSizeWarningLimit` (check `vite build` output).
