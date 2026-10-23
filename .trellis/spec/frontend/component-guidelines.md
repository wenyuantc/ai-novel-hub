# Component Guidelines

> How components are built in this project.

---

## Overview

Every UI element is an Ant Design 5 component. Locale is Chinese: AntD `zh_CN` in `App.tsx`, dayjs `zh-cn` in `main.tsx`. Feedback (`message`, `modal`) always comes from `App.useApp()` so it inherits the `ConfigProvider` theme/locale — never the static `message.success(...)` import.

```tsx
// src/App.tsx
<ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
  <AntApp>
    <RouterProvider router={router} />
  </AntApp>
</ConfigProvider>

// src/main.tsx
import 'dayjs/locale/zh-cn'
dayjs.locale('zh-cn')
```

---

## Component Structure

Function components with a default export, a local `interface Props`, and a one-line Chinese JSDoc describing purpose. Data loading uses `useLoader` with a `useCallback`-memoized loader; mutations are `async` handlers with `try/catch/finally` that show `message.error(errorMessage(err))`.

### Reusable components (`src/components/`)

| Component | Contract (from source) |
|-----------|------------------------|
| `AppLayout` | Top `Layout.Header` with menu (`我的项目` / `模型配置`), username dropdown; logout calls `useAuthStore().logout()` then `navigate('/login', { replace: true })`; renders `<Outlet/>`. |
| `RequireAuth` | Route guard: `status === 'unknown'` → `<FullPageSpin/>`; `'anonymous'` → `<Navigate to="/login" replace state={{ from: location.pathname + location.search }} />`. `LoginPage` reads `state.from` (default `/projects`). |
| `AIConfigSelect` | `{ value?: number \| null; onChange?: (v: number \| null) => void; disabled?: boolean }`. Calls `useAIConfigStore().load()` on mount. Empty selection = `null` = "use default config" (placeholder `默认：{name}（{model}）`). When loaded and zero configs, renders a warning `Alert` linking to `/settings/ai` instead of the select. |
| `StreamOutput` | `{ text; running; placeholder?; maxHeight? = 360 }`. `pre-wrap` monospace box, auto-scrolls to bottom while `running`, shows a small `Spin` while running. |
| `WizardGenerator<K extends WizardKind>` | `{ projectId; kind: K; existingCount?; showResult?; onDone?(items: WizardItemMap[K][]); onRunningChange?(running) }`. Form disabled while running; Start button disabled when `useHasAIConfig() === false`; running shows a danger `停止` button wired to `task.stop`. On abort shows `已停止生成，原有数据未改变`. `existingCount > 0` shows an "will replace" `Alert`. |
| `WizardModal<K>` | Wraps `WizardGenerator` in a `Modal` (`maskClosable={false}`, `destroyOnHidden`, `footer={null}`). Refuses to close while running (`message.warning('正在生成，请先停止')`). On done: `onDone(items)` then `onClose()`. Used by `WorldPanel`, `CharactersPanel`, `OutlinesPanel`. |
| `ExportModal` | `{ projectId; open; onClose }`. Renders `ExportModalInner` only when `open` (so its `useLoader` fetches fresh chapters each open). Format radio `txt`/`md`; row selection disables `generating` chapters; empty selection exports all. Calls `exportProject` and shows `已导出：{filename}`. Used by `OverviewPanel`. |
| `StoryPreview` | Named exports `WorldPreview`, `CharacterPreview`, `OutlinePreview` (uses `groupOutlines`/`volumeLabel` from `utils/outline.ts`). |

### Page patterns

- **Panel CRUD** (`WorldPanel`, `CharactersPanel`, `OutlinesPanel`, `ChaptersPanel`): `const pid = useProjectId()`; `const loader = useCallback(() => worldApi.list(pid), [pid])`; `const { data: items = [], loading, reload } = useLoader(loader)`; AntD `Form.useForm` inside a `Modal` for create/edit; `Popconfirm` for delete; `reload()` after every successful mutation.
- **Project-level numbers**: after mutations that change word/chapter counts, call `useProjectStore((s) => s.refresh)` (`ChaptersPanel`, `OutlinesPanel`, chapter editor). `OverviewPanel` writes the PATCH result back with `setProject(updated)`.
- **Destructive confirms** use `modal.confirm` (project delete in `ProjectListPage`, regenerate-over-content in the editor) or `Popconfirm` (row deletes).
- **Route params**: `useProjectId()` for `:pid`; guard invalid ids with `Number.isFinite` and render `<Result status="404" …/>`.

### Chapter editor (`src/pages/project/ChapterEditorPage.tsx`)

Three layers: `ChapterEditorPage` (parses `:cid`, renders `<ChapterLoader key={chapterId} …/>` so switching chapters rebuilds all state) → `ChapterLoader` (loads chapter + sibling list) → `ChapterEditor`. Behaviors that must be preserved:

1. **Dirty tracking**: `const dirty = content !== chapter.content || title !== chapter.title`; `applyServerChapter(c)` resets `chapter`, `title`, `content` from the server response.
2. **Auto-save before AI generate/polish**:
   ```ts
   const runAI = async (mode: AIMode) => {
     if (dirty) {
       if (!(await save())) return
       message.info('已自动保存当前修改')
     }
     const outcome = await task.start((onDelta, signal) => mode === 'generate' ? generateChapter(...) : polishChapter(...))
   ```
   On `done` → `applyServerChapter(outcome.value)` (server already saved, status `done`). On abort/error → `reloadChapter()` syncs only server status and keeps local text.
3. **Read-only while streaming**: textarea shows `task.text` while running (`displayContent = task.running ? task.text : content`) with `readOnly={task.running}`; title input, AI forms, tabs and `AIConfigSelect` are `disabled={task.running}`.
4. **Ctrl/Cmd+S**: a window `keydown` listener registered once; it calls the latest `handleSave` through a ref (`saveShortcutRef.current = handleSave` in an effect without deps) so the listener never captures stale state. `handleSave` no-ops while running/saving and shows `没有需要保存的修改` when clean.
5. **Unsaved-leave guard**: `const guard = dirty || task.running`. Browser unload via a `beforeunload` listener (only attached while `guard`); in-app navigation via `useBlocker(({ currentLocation, nextLocation }) => guard && currentLocation.pathname !== nextLocation.pathname)` with a confirm `Modal` whose OK does `task.stop(); blocker.proceed?.()`.
6. **Stuck `generating` chapter**: `const stuck = chapter.status === 'generating' && !task.running` shows a warning `Alert` with a `复位` action → `chapterApi.update(pid, chapter.id, { status: 'draft' })`. While stuck, Save, mark-done and AI buttons are disabled, because the backend rejects content/title PATCH on a generating chapter with 409 (`ConflictError` in `backend/app/services/chapter_service.py`), and generate/polish return 409 `该章节正在生成中，请稍候再试`.
7. **Regenerate over content** asks via `modal.confirm` (`重新生成正文？`); polish is disabled when content is empty.
8. Prev/next navigation sorts siblings by `chapter_no` then `id`.

### AI config page (`src/pages/AIConfigPage.tsx`)

- Loads with `load(true)` (forced) on mount and after every mutation.
- Edit form always starts with an empty `api_key`; on save, the key is only sent when non-empty (`key ? { ...body, api_key: key } : body`) — see PATCH semantics in [type-safety.md](./type-safety.md).
- API key is required on create except for `openai_compat` (local LLMs).
- Base URL hint for `openai_compat` documents Docker + host Ollama: `http://host.docker.internal:11434/v1（Docker 部署下的本机 Ollama，需部署方开启 ALLOW_PRIVATE_AI_ENDPOINTS）`. Without that flag the backend endpoint guard blocks private/loopback addresses; `POST /ai-configs/{id}/test` then returns HTTP 200 with `ok: false` and the backend message (`backend/app/services/ai_config_service.py` converts `BadRequestError` into `AIConfigTestResult(ok=False, message=exc.detail, …)`). The page shows `message.error(\`连通失败：${r.message}\`)` and a red `失败 · {latency_ms} ms` tag with the message in the 连通性 column.

---

## Props Conventions

- Props are a local `interface Props` (not exported), destructured in the signature with defaults inline (`existingCount = 0`, `maxHeight = 360`).
- Controlled-value components follow AntD's `value` / `onChange` shape so they work inside `Form.Item` (`AIConfigSelect`).
- Generic components carry the wizard kind as a type parameter so `onDone` is typed per kind: `WizardGenerator<K extends WizardKind>` → `onDone?: (items: WizardItemMap[K][]) => void`.
- Callbacks are optional (`onDone?.(…)`, `onRunningChange?.(…)`) unless the parent must act (`WizardModal` requires `onClose`/`onDone`).
- Derived types use `Omit<Props, 'open'>` rather than duplicating interfaces (`ExportModalInner`).

---

## Styling Patterns

- AntD layout primitives (`Layout`, `Row/Col` with `xs`/`xl` breakpoints, `Space direction="vertical"`, `Card size="small"`) plus inline `style={{ … }}` objects. No CSS modules, Tailwind or styled-components.
- Shared inline style constants are module-level `const pre: CSSProperties = { whiteSpace: 'pre-wrap', marginBottom: 0 }` (`StoryPreview.tsx`).
- Theme customization only via `ConfigProvider theme.token` (`colorPrimary: '#1677ff'`).
- Status colors come from the label maps (`CHAPTER_STATUS[s].color`, `PROJECT_STATUS[status].color`) rendered as `<Tag>`.

---

## Accessibility

The project relies on AntD's built-in semantics (labelled `Form.Item`, keyboard-operable `Button`/`Menu`/`Modal`). Visible text labels are used for almost all buttons. Known gap: the editor's back button is icon-only (`<Button type="text" size="small" icon={<ArrowLeftOutlined />} …/>`) without `aria-label`; when adding new icon-only buttons, add an `aria-label` or `title`.

---

## Common Mistakes

- Using static `message`/`Modal` from `antd` instead of `App.useApp()` — loses locale/theme context.
- Letting the user edit or save a chapter whose status is `generating` — backend returns 409; keep the `stuck` gating.
- Starting AI generation with unsaved edits — generation reads server content; always `save()` first as `runAI` does.
- Capturing `handleSave` directly in a once-registered `keydown` listener — stale closure; use the ref pattern.
- Closing `WizardModal` mid-stream — prevented on purpose; don't add `maskClosable` or a footer close button.
- Rendering `ExportModalInner` while closed — it would load chapters once and show stale data.
