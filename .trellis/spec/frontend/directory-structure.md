# Directory Structure

> How frontend code is organized in this project.

---

## Overview

All source lives in `frontend/src/`. The split is by technical layer, not by feature: HTTP/SSE code in `api/`, global state in `stores/`, reusable hooks in `hooks/`, reusable UI in `components/`, route screens in `pages/` (project workspace screens in `pages/project/`), pure helpers in `utils/`. There is no `assets/` folder; the only static asset is `frontend/public/favicon.svg`. Styling is AntD props plus inline `style` objects; `src/index.css` is the only stylesheet.

---

## Directory Layout

```
frontend/
├── vite.config.ts          # dev proxy /api -> backend, chunkSizeWarningLimit, vitest config
├── eslint.config.js        # typescript-eslint + react-hooks + react-refresh, no-explicit-any = error
├── tsconfig.app.json       # strict, noUnusedLocals/Parameters, verbatimModuleSyntax
└── src/
    ├── main.tsx            # dayjs.locale('zh-cn'), StrictMode, mounts <App/>
    ├── App.tsx             # ConfigProvider(zh_CN) + AntApp + RouterProvider, 401 handler, store reset on user change
    ├── router.tsx          # createBrowserRouter route table (data router -> useBlocker works)
    ├── index.css
    ├── api/
    │   ├── client.ts       # fetch wrapper: API_BASE, ApiError, request/api.get/post/patch/del, 401 hook, errorMessage
    │   ├── sse.ts          # SSEParser, postSSE, runGeneration, StreamError, isAbortError
    │   ├── sse.test.ts     # parser unit tests
    │   ├── types.ts        # TS mirrors of backend/app/schemas/*.py
    │   ├── auth.ts         # authApi
    │   ├── aiConfigs.ts    # aiConfigApi, PROVIDER_LABELS
    │   ├── projects.ts     # projectApi, PROJECT_STATUS, exportProject, parseContentDispositionFilename
    │   ├── story.ts        # makeCrud -> worldApi / characterApi / outlineApi
    │   ├── chapters.ts     # chapterApi, generateChapter, polishChapter, CHAPTER_STATUS, countWords
    │   └── wizard.ts       # runWizard, WIZARD_KIND_LABELS
    ├── stores/             # auth.ts, aiConfigs.ts, project.ts (Zustand)
    ├── hooks/              # useStreamTask.ts, useLoader.ts, useProjectId.ts, useHasAIConfig.ts
    ├── components/         # AppLayout, RequireAuth, AIConfigSelect, StreamOutput, WizardGenerator,
    │                       # WizardModal, ExportModal, StoryPreview
    ├── pages/
    │   ├── LoginPage.tsx, ProjectListPage.tsx, AIConfigPage.tsx, NotFoundPage.tsx
    │   └── project/        # ProjectLayout + OverviewPanel, WizardPage, WorldPanel, CharactersPanel,
    │                       # OutlinesPanel, ChaptersPanel, ChapterEditorPage
    └── utils/              # time.ts (+ time.test.ts), outline.ts (groupOutlines, volumeLabel)
```

---

## Module Organization

**`api/` — one module per backend resource.** Each module exports a plain object of thin functions over `api.*` from `client.ts`, plus UI label maps for that resource's enums. Streaming endpoints are free functions built on `runGeneration`:

```ts
// src/api/chapters.ts
export const chapterApi = {
  list: (pid: number) => api.get<ChapterBrief[]>(`/projects/${pid}/chapters`),
  update: (pid: number, cid: number, body: ChapterUpdate) =>
    api.patch<Chapter>(`/projects/${pid}/chapters/${cid}`, body),
  ...
}
export function generateChapter(cid: number, body: GenerateChapterRequest,
  onDelta: (text: string) => void, signal?: AbortSignal): Promise<Chapter> {
  return runGeneration<ChapterDone>(`/chapters/${cid}/generate`, body, { onDelta, signal }).then((d) => d.chapter)
}
```

Resources with identical CRUD shape share a factory instead of copy-paste:

```ts
// src/api/story.ts
export const worldApi = makeCrud<WorldSetting, WorldSettingInput>('world')
export const characterApi = makeCrud<Character, CharacterInput>('characters')
export const outlineApi = makeCrud<Outline, OutlineInput>('outlines')
```

Paths passed to `api.*` are relative to `API_BASE = '/api/v1'`; never hard-code `/api/v1` in pages.

**`router.tsx`** — single `createBrowserRouter` table. `/login` is public; everything else is nested under `<RequireAuth><AppLayout/></RequireAuth>`. The project workspace is `projects/:pid` with `ProjectLayout` and child panels (`overview`, `wizard`, `world`, `characters`, `outlines`, `chapters`, `chapters/:cid`). A data router is required because the chapter editor uses `useBlocker`.

**New feature checklist:** types in `api/types.ts` → resource functions in `api/<resource>.ts` → (only if cross-page) store in `stores/` → screen in `pages/` or `pages/project/` → route in `router.tsx`.

---

## Naming Conventions

- Components and pages: `PascalCase.tsx`, one default-exported component per file (`export default function WorldPanel()`); small helpers may be named exports in the same file (`FullPageSpin` in `RequireAuth.tsx`, `WorldPreview`/`CharacterPreview`/`OutlinePreview` in `StoryPreview.tsx`).
- Project workspace sub-screens are `*Panel.tsx`; full screens are `*Page.tsx`.
- Hooks: `useXxx.ts`, named export.
- Stores: `stores/<domain>.ts` exporting `use<Domain>Store` (`useAuthStore`, `useAIConfigStore`, `useProjectStore`).
- API modules: `api/<resource>.ts` exporting `<resource>Api` objects; label maps are `SCREAMING_CASE` (`PROJECT_STATUS`, `CHAPTER_STATUS`, `PROVIDER_LABELS`, `WIZARD_KIND_LABELS`).
- Tests: co-located `*.test.ts` (Vitest `include: ['src/**/*.test.ts']`).
- Wire fields keep backend `snake_case` (`ai_config_id`, `chapter_no`); local variables are `camelCase`.

---

## Examples

- `src/api/story.ts` — generic CRUD factory for same-shaped resources.
- `src/pages/project/WorldPanel.tsx` — canonical panel: `useProjectId` + `useLoader` + AntD `Card`/`List`/`Modal` form + `WizardModal`.
- `src/pages/project/ChapterEditorPage.tsx` — route entry keyed by id (`<ChapterLoader key={chapterId} …/>`) to rebuild editor state when switching chapters.
