# 目录结构

> 本项目前端代码的组织方式。

---

## 概述

所有源码位于 `frontend/src/`。按技术分层而非按功能划分：HTTP/SSE 代码在 `api/`，全局状态在 `stores/`，可复用 hook 在 `hooks/`，可复用 UI 在 `components/`，路由页面在 `pages/`（项目工作区页面在 `pages/project/`），纯函数辅助工具在 `utils/`。没有 `assets/` 目录；唯一的静态资源是 `frontend/public/favicon.svg`。样式使用 AntD props 加内联 `style` 对象；`src/index.css` 是唯一的样式表。

---

## 目录布局

```
frontend/
├── vite.config.ts          # 开发代理 /api -> 后端、chunkSizeWarningLimit、vitest 配置
├── eslint.config.js        # typescript-eslint + react-hooks + react-refresh，no-explicit-any = error
├── tsconfig.app.json       # strict、noUnusedLocals/Parameters、verbatimModuleSyntax
└── src/
    ├── main.tsx            # dayjs.locale('zh-cn')、StrictMode，挂载 <App/>
    ├── App.tsx             # ConfigProvider(zh_CN) + AntApp + RouterProvider、401 处理器、用户切换时重置 store
    ├── router.tsx          # createBrowserRouter 路由表（data router -> 可使用 useBlocker）
    ├── index.css
    ├── api/
    │   ├── client.ts       # fetch 封装：API_BASE、ApiError、request/api.get/post/patch/del、401 hook、errorMessage
    │   ├── sse.ts          # SSEParser、postSSE、runGeneration、StreamError、isAbortError
    │   ├── sse.test.ts     # 解析器单元测试
    │   ├── types.ts        # backend/app/schemas/*.py 的 TS 镜像
    │   ├── auth.ts         # authApi
    │   ├── aiConfigs.ts    # aiConfigApi、PROVIDER_LABELS
    │   ├── projects.ts     # projectApi、PROJECT_STATUS、exportProject、parseContentDispositionFilename
    │   ├── story.ts        # makeCrud -> worldApi / characterApi / outlineApi
    │   ├── chapters.ts     # chapterApi、generateChapter、polishChapter、CHAPTER_STATUS、countWords
    │   └── wizard.ts       # runWizard、WIZARD_KIND_LABELS
    ├── stores/             # auth.ts、aiConfigs.ts、project.ts（Zustand）
    ├── hooks/              # useStreamTask.ts、useLoader.ts、useProjectId.ts、useHasAIConfig.ts
    ├── components/         # AppLayout、RequireAuth、AIConfigSelect、StreamOutput、WizardGenerator、
    │                       # WizardModal、ExportModal、StoryPreview
    ├── pages/
    │   ├── LoginPage.tsx, ProjectListPage.tsx, AIConfigPage.tsx, NotFoundPage.tsx
    │   └── project/        # ProjectLayout + OverviewPanel、WizardPage、WorldPanel、CharactersPanel、
    │                       # OutlinesPanel、ChaptersPanel、ChapterEditorPage
    └── utils/              # time.ts（+ time.test.ts）、outline.ts（groupOutlines、volumeLabel）
```

---

## 模块组织

**`api/`——每个后端资源对应一个模块。** 每个模块导出一个普通对象，其中是基于 `client.ts` 中 `api.*` 的轻量函数，另外导出该资源枚举对应的 UI 标签映射。流式端点则是基于 `runGeneration` 构建的独立函数：

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

CRUD 结构完全相同的资源共用一个工厂函数，而不是复制粘贴：

```ts
// src/api/story.ts
export const worldApi = makeCrud<WorldSetting, WorldSettingInput>('world')
export const characterApi = makeCrud<Character, CharacterInput>('characters')
export const outlineApi = makeCrud<Outline, OutlineInput>('outlines')
```

传给 `api.*` 的路径是相对于 `API_BASE = '/api/v1'` 的；绝不要在页面中硬编码 `/api/v1`。

**`router.tsx`**——唯一的 `createBrowserRouter` 路由表。`/login` 是公开的；其余全部嵌套在 `<RequireAuth><AppLayout/></RequireAuth>` 之下。项目工作区是 `projects/:pid`，由 `ProjectLayout` 及其子面板组成（`overview`、`wizard`、`world`、`characters`、`outlines`、`chapters`、`chapters/:cid`）。必须使用 data router，因为章节编辑器用到了 `useBlocker`。

**新功能清单：** 在 `api/types.ts` 中定义类型 → 在 `api/<resource>.ts` 中编写资源函数 →（仅在跨页面时）在 `stores/` 中建 store → 在 `pages/` 或 `pages/project/` 中编写页面 → 在 `router.tsx` 中注册路由。

---

## 命名约定

- 组件与页面：`PascalCase.tsx`，每个文件默认导出一个组件（`export default function WorldPanel()`）；小型辅助组件可在同一文件中具名导出（`RequireAuth.tsx` 中的 `FullPageSpin`，`StoryPreview.tsx` 中的 `WorldPreview`/`CharacterPreview`/`OutlinePreview`）。
- 项目工作区子页面命名为 `*Panel.tsx`；完整页面命名为 `*Page.tsx`。
- Hook：`useXxx.ts`，具名导出。
- Store：`stores/<domain>.ts`，导出 `use<Domain>Store`（`useAuthStore`、`useAIConfigStore`、`useProjectStore`）。
- API 模块：`api/<resource>.ts`，导出 `<resource>Api` 对象；标签映射使用 `SCREAMING_CASE`（`PROJECT_STATUS`、`CHAPTER_STATUS`、`PROVIDER_LABELS`、`WIZARD_KIND_LABELS`）。
- 测试：与源码同目录放置的 `*.test.ts`（Vitest `include: ['src/**/*.test.ts']`）。
- 传输字段保持后端的 `snake_case`（`ai_config_id`、`chapter_no`）；局部变量使用 `camelCase`。

---

## 示例

- `src/api/story.ts`——面向同结构资源的通用 CRUD 工厂。
- `src/pages/project/WorldPanel.tsx`——标准面板：`useProjectId` + `useLoader` + AntD `Card`/`List`/`Modal` 表单 + `WizardModal`。
- `src/pages/project/ChapterEditorPage.tsx`——以 id 作为 key 的路由入口（`<ChapterLoader key={chapterId} …/>`），在切换章节时重建编辑器状态。
