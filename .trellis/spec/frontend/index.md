# Frontend Development Guidelines

> Conventions for `frontend/` (Vite 8 + React 18 + TypeScript strict + Ant Design 5 + Zustand 5 + React Router 6 + Vitest).

---

## Overview

The frontend is a single-page app served by Vite in dev (`/api` proxied to the FastAPI backend) and built into one static bundle. All backend calls go through `src/api/` (fetch wrapper + SSE client); UI is Ant Design with the `zh_CN` locale; global state is three small Zustand stores; everything else is local component state. These documents describe what the code actually does today — read the relevant one before changing a layer.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | `src/api`, `stores`, `hooks`, `components`, `pages/project`, `utils`, `router.tsx` layout and naming | Filled |
| [Component Guidelines](./component-guidelines.md) | AntD/zh_CN setup, reusable components, page patterns, chapter editor behaviors, AI config page hint, admin users page / `RequireAdmin` | Filled |
| [Hook Guidelines](./hook-guidelines.md) | `useStreamTask`, `useLoader`, `useProjectId`, `useHasAIConfig`; SSE client code-spec scenario | Filled |
| [State Management](./state-management.md) | Zustand stores (auth / aiConfigs / project), global vs local, clearing on logout/401 | Filled |
| [Quality Guidelines](./quality-guidelines.md) | lint/build/test gate, Vite proxy, bundle size, forbidden/required patterns, review checklist | Filled |
| [Type Safety](./type-safety.md) | `types.ts` mirrors backend schemas, enums, PATCH semantics, UTC time parsing; API client code-spec scenario | Filled |

Code-spec scenarios (7-section format):

- API client contract (`src/api/client.ts`, `src/api/projects.ts` export) — in [Type Safety](./type-safety.md#scenario-api-client-contract).
- SSE client parser (`src/api/sse.ts`, `src/api/sse.test.ts`) — in [Hook Guidelines](./hook-guidelines.md#scenario-sse-client-parser).

---

**Language**: All documentation is written in **English**; Chinese appears only when quoting actual UI strings.
