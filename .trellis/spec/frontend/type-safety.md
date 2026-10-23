# 类型安全

> 本项目中的类型安全模式。

---

## 概述

TypeScript ~5.8，启用严格模式（`frontend/tsconfig.app.json`：`strict`、`noUnusedLocals`、`noUnusedParameters`、`noFallthroughCasesInSwitch`、`verbatimModuleSyntax`）。ESLint 强制 `'@typescript-eslint/no-explicit-any': 'error'`。没有运行时校验库（不用 Zod/Yup）：默认信任响应体与 `src/api/types.ts` 一致，而该文件镜像后端的 Pydantic schema。不可信的数据结构（错误响应体、SSE 载荷）通过 `unknown` + 类型守卫进行收窄。

---

## 类型组织

- **所有传输类型都放在 `src/api/types.ts`**（文件头注释：`与后端 app/schemas/*.py 对齐的类型定义`）。每个 Pydantic 模型对应一个 TS interface：`*Out` → 去后缀的名字（`AIConfig`、`Project`、`Chapter`），`*Create`/`*Update`/`*Request` → 同名。
- 字段名保持与传输格式完全一致的 `snake_case`。`datetime` 字段为 `string`。带默认值的可选请求字段用 `?:`；可空字段用 `| null`。
- 枚举是从后端 `Literal[...]` 复制而来的字符串字面量联合类型：

  | TS 类型 | 取值 | 后端来源 |
  |---------|--------|----------------|
  | `ProviderName` | `'openai_compat' \| 'anthropic' \| 'gemini'` | `schemas/ai_config.py` |
  | `ChapterStatus` | `'draft' \| 'generating' \| 'done'` | `schemas/chapter.py` |
  | `ChapterUpdate.status` | `'draft' \| 'done' \| null`（绝不为 `generating`） | `ChapterUpdate.status: Literal["draft", "done"] \| None` |
  | `ProjectStatus` | `'planning' \| 'writing' \| 'completed'` | `schemas/project.py` |
  | `WizardKind` | `'outline' \| 'characters' \| 'world'` | 向导路由 `/projects/{pid}/wizard/{kind}` |
  | `VersionSource` | `'manual' \| 'baseline' \| 'generate' \| 'polish' \| 'rewrite' \| 'restore'` | `schemas/chapter_version.py` |
  | `GenerationKind` | `'generate' \| 'polish' \| 'rewrite' \| 'expand' \| 'condense' \| 'deslop' \| 'continue'` | 同上 |
  | `GenerationStatus` | `'success' \| 'error' \| 'cancelled'` | 同上 |
  | `RewriteOp` | `'rewrite' \| 'expand' \| 'condense' \| 'deslop' \| 'continue'` | 同上 |
  | `TaskKind` | `'chapter.generate' \| 'chapter.polish' \| 'wizard.outline' \| 'wizard.characters' \| 'wizard.world' \| 'character.generate' \| 'organization.generate' \| 'relations.parse'` | `schemas/task.py` |
  | `WorldCategory` | `'place' \| 'faction' \| 'item' \| 'rule' \| 'history' \| 'other'` | `schemas/story.py` |
  | `LifeStatus` | `'alive' \| 'dead' \| 'missing' \| 'other'` | 同上 |
  | `MemberStatus` | `'active' \| 'left' \| 'expelled' \| 'dead'` | `schemas/organization.py` |
  | `RelationCategory` | `'family' \| 'social' \| 'hostile' \| 'work' \| 'other'` | `schemas/relationship.py` |
  | `RelationStatus` | `'normal' \| 'broken' \| 'past' \| 'complicated'` | 同上 |
  | `CardResolution` | `'skip' \| 'rename'` | `schemas/card.py` |
  | `TaskStatus` | `'queued' \| 'running' \| 'succeeded' \| 'failed' \| 'cancelled' \| 'interrupted'` | 同上 |

- UI 标签映射使用 `Record<Union, …>` 类型，因此新增枚举值后在补上标签前会产生编译错误：`PROJECT_STATUS: Record<ProjectStatus, { label; color }>`、`CHAPTER_STATUS: Record<ChapterStatus, …>`、`PROVIDER_LABELS: Record<AIConfig['provider'], string>`、`WIZARD_KIND_LABELS: Record<WizardKind, string>`。
- 类型级映射把泛型参数与载荷绑定：`WizardItemMap { outline: Outline; characters: Character; world: WorldSetting }` → `runWizard<K>(…): Promise<WizardItemMap[K][]>`。
- 组件本地的表单结构（`FormValues`）保留在组件文件中。

- 认证/管理员类型：`User.is_admin` 镜像 `UserOut.is_admin`；`Page<T> = { items: T[]; total: number }` 镜像 `AdminUserList`；`AdminUser` 镜像 `AdminUserOut`（`id, username, is_admin, is_active, created_at, project_count`）；`AdminUserCreate`、`AdminUserUpdate`（`is_admin?`、`is_active?`）、`ResetPasswordRequest`、`ChangePasswordRequest`。后端契约：`.trellis/spec/backend/auth-admin.md`。
- 章节版本 / 生成记录 / 局部改写类型（`10-23-chapter-versions`）：`ChapterVersionBrief`（`id, chapter_id, seq, source, word_count, starred, note, generation_log_id, created_at, updated_at`）、`ChapterVersion`（另加 `content`）、`ChapterVersionUpdate`（`starred?`、`note?` ≤200，省略或 null = 不变）、`GenerationLogBrief`（`params: Record<string, unknown>`、`prompt_tokens`/`completion_tokens` 恒为 null）、`GenerationLog`（另加 `messages: GenerationMessage[]`）、`RewriteRequest`、`RewriteDone`（含 `before`：选区前 ≤200 码点原文）、`RewriteApply`（`before?` 原样回传做乐观校验）、`ContinueEvent`、`ChapterDone`；`GenerateChapterRequest.auto_continue?`（后端默认 false）。标签映射在 `api/chapterVersions.ts`：`VERSION_SOURCE`、`GENERATION_KIND_LABELS`、`GENERATION_STATUS`、`REWRITE_OP_LABELS`（均为 `Record<Union, …>`）。
- 后台任务类型（`10-23-task-engine`）：`Task` 镜像 `TaskOut`（`id, kind, status, title, project_id, chapter_id, progress 0–100, stage, error, retry_of_id, character_id`（取自 `input.request.character_id`，目前仅 `relations.parse` 有值）`, queue_position`（仅 queued 有值，0 = 下一个）`, created_at, started_at, finished_at`）；`TaskListQuery`（`project_id? chapter_id? kind? active? limit?`）、`TaskOutput {text}`、`TaskClearResult {deleted}`、`TaskProgress`（订阅 `progress` 载荷，`queue_position?`）。标签映射在 `api/tasks.ts`：`TASK_STATUS: Record<TaskStatus, {label, color}>`、`TASK_KIND_LABELS: Record<TaskKind, string>`；判定函数 `isActiveTask`、`canRetryTask`（`failed|cancelled|interrupted`，与后端 409 规则一致）。`generateChapter` / `polishChapter` / `runWizard` 返回 `Task`；`done.result` 是 `unknown`，必须经 `parseChapterDone` / `parseWizardDone(kind, …)` 收窄（结构不符返回 null，调用方回退为重新拉取 / 空列表）；订阅事件里的 `task` 用 `readTask`（`id: number` + `status: string`）收窄，可能为 null。
- 结构化设定类型（`10-23-story-entities`）：`Character` 新增 `aliases: string[]`、`age`、`gender`、`appearance`、`personality`、`background`、`current_state`、`life_status: string`、`always_in_context`；`CharacterInput.always_in_context?: boolean | null`（创建时省略 = 按定位推断）。`WorldSetting.category: string`（Out 为 str，读取时经 `utils/world.ts` 的 `asWorldCategory` 收窄），`WorldSettingInput.category?: WorldCategory`。组织：`Organization`（含 `member_count`）、`OrganizationInput`、`OrganizationUpdate = Partial<OrganizationInput>`（`parent_id` / `power_level` 显式 null = 清空）、`Member`（含 `organization_name` / `character_name`）、`MemberInput`、`MemberUpdate`、`OrganizationWithMembers(Out)`。关系：`RelationshipType`（`project_id: null` + `builtin` = 预置只读）、`TypeRef`（`type_id` 或 `role_a + role_b + category`）、`RelationshipCreate`、`RelationshipUpdate`（`a_id` 调换方向）、`Relationship`（`other_id/other_name/other_role/self_role` 仅按 `character_id` 查询时有值，否则 null）、`RelationshipHistory.changes: Record<string, unknown>`、`RelationDraft`（`subject_is_a`）、`CharacterWithRelations(Out)`、`RelationshipGraph {nodes, edges, organizations}`、`ChapterCast {character_ids}`。卡片：`CardPreview`、`CardResolutions`、`CardImportResult`。AI 任务结果：`CharacterGenerateResult`、`OrganizationGenerateResult`、`RelationsParseResult`、`RelationSuggestion`、`MemberSuggestion`——`done.result` 必须经 `api/entityTasks.ts` 的 `parseCharacterGenerateResult` / `parseOrganizationGenerateResult` / `parseRelationsParseResult` 收窄（缺字段补默认值，`target_id` 为 null 时强制 `resolved=false`）。标签映射：`api/characters.ts` 的 `LIFE_STATUS`、`ROLE_TYPES`；`api/organizations.ts` 的 `MEMBER_STATUS`；`api/relationships.ts` 的 `RELATION_CATEGORY`、`RELATION_STATUS`（均为 `Record<Union, …>`）；后端返回 `string` 的字段用 `lifeStatusLabel` / `memberStatusLabel` / `categoryLabel` / `statusLabel` 兜底未知值。
- **关系语义**：「a 是 b 的 `role_a`，b 是 a 的 `role_b`」。前端视角换算只用 `perspective(rel, viewerId)` / `typeOptions` / `describeRelation`；保存建议只用 `suggestionsToDrafts`（with-relations）/ `suggestionsToBatch(subjectId, …)`（`a_id = subject_is_a ? 主体 : 目标`）——不要在组件里手写方向判断。
- **偏移与字数按码点**：后端偏移与 `len()` 都按 Unicode 码点，JS 字符串按 UTF-16。TextArea 偏移发请求前用 `utils/textOffset.ts` 的 `utf16ToCodePoint` 换算，回显用 `codePointToUtf16`；选区长度用 `codePointLength`。`countWords` 为 `Array.from(text.replace(/\s+/g, '')).length`（emoji / 扩展 B 区生僻字算 1 字），与后端 `count_words` 一致。`textOffset.test.ts` 覆盖 emoji 与 `𠀀`/`𠮷`。
- 共享表单规则放在 `src/utils/validation.ts`（`usernameRules` 3–32 位且不含空白、`passwordRules` 6–72 位、`confirmPasswordRules(field = 'password')`）。登录/注册、修改密码、管理员创建用户和重置密码都导入它们——绝不在页面中重复声明这些限制。

### PATCH 语义（必须与后端 service 一致）

| 资源 | 规则 | 后端依据 |
|----------|------|------------------|
| 项目、世界观/角色/大纲 | 省略**或** `null` = 不变 | `model_dump(exclude_unset=True)` + `if value is None: continue`（`project_service.update_project`、`story_service.update_owned`） |
| 章节 | 对 `chapter_no`、`title`、`content`、`status` 而言省略/`null` = 不变；显式发送 `outline_id: null` 会**解除**与大纲的关联 | `chapter_service.update_chapter` 检查 `"outline_id" in fields` |
| `generating` 中的章节 | PATCH 带 `title`/`content` 且不带 `status` → 409；仅发送 `{ status: 'draft' }` 是复位路径 | `ConflictError("该章节正在生成中，请等待完成或先复位状态后再编辑")` |
| 管理员用户 | 省略 `is_admin` / `is_active` = 不变；禁用会递增 `token_version`（目标用户的会话 → 401） | `admin_service.update_user` |
| AI 配置 | 省略 `api_key` 或传 `''` 保留已存储的密钥；只要出现 `base_url`（包括 `null`/`''`）就会被应用，`null` = provider 默认值 | `ai_config_service.update_config` 中的 `if data.api_key:` / `if "base_url" in fields:` |

前端的相应做法（`AIConfigPage.handleSave`）：始终发送 `base_url`（`v.base_url?.trim() ? v.base_url.trim() : null`），`api_key` 仅在非空时发送。`AIConfigUpdate = Partial<AIConfigCreate>`。

### 时间戳

后端的 datetime 序列化时可能不带时区后缀。务必通过 `src/utils/time.ts` 解析：

```ts
export function parseServerTime(value: string): dayjs.Dayjs {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return dayjs(hasZone ? value : `${value}Z`)   // 无时区 -> UTC
}
export function formatTime(value: string | null | undefined, fmt = 'YYYY-MM-DD HH:mm'): string
```
`time.test.ts` 断言 `'2026-10-22T08:00:00'` → `'2026-10-22T08:00:00.000Z'`，并断言 `+08:00` / `Z` 输入保留其时区。绝不要直接对服务端时间戳调用 `dayjs(value)` 或 `new Date(value)`。

---

## 校验

- **输入**：AntD `Form` 规则与后端 `Field` 限制保持一致（例如向导中 `volume_count` 为 `InputNumber min={1} max={20}`，`character_count` 为 `min={3} max={30}`，编辑器 `target_words` 为 `min={300} max={20000}`，指令 `max: 2000`；登录用户名 3–32 位，密码 6–72 位）。字符串发送前先 `.trim()`。后端仍是最终权威，并返回 `{detail}`（422 的 detail 以 `请求参数校验失败：` 开头）。
- **响应**：不做运行时校验；通过 `request<T>` 定类型。
- **不可信 JSON**：从 `unknown` 收窄：
  ```ts
  // src/api/client.ts
  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null
  }
  const data: unknown = JSON.parse(text)
  if (isRecord(data) && typeof data.detail === 'string') return data.detail
  ```
  `sse.ts` 的 `readText`/`readDetail` 对 SSE 载荷做同样处理；`runGeneration` 接受一个可选的 `parseDone?: (data: unknown) => TDone` 校验函数；`subscribeTask` 用 `isContinueEvent` / `isProgress` / `readTask` 守卫各事件载荷。

---

## 常用模式

- 全部使用 `catch (err)` / `(err: unknown)`，并通过 `errorMessage(err)` 转换。
- 结果使用可辨识联合类型：`StreamOutcome<T>`（`status: 'done' | 'aborted' | 'error'`）、`AuthStatus`。
- 泛型 API 辅助函数：`request<T>`、`api.get<T>`、`makeCrud<TItem, TInput>`、`runGeneration<TDone>`。
- 仅类型导入使用 `import type { … }`（`verbatimModuleSyntax` 要求）。

---

## 场景：API 客户端契约

### 1. 范围 / 触发条件
任何对 `src/api/client.ts`、`src/api/projects.ts` 中 `exportProject` / `parseContentDispositionFilename`、后端错误格式（`backend/app/core/errors.py`）、认证 cookie 或导出端点（`GET /api/v1/projects/{id}/export`）的修改。

### 2. 签名
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

### 3. 契约
- 每个请求都使用 `credentials: 'include'`（HttpOnly cookie 会话；JS 中不持有 token）。请求头：`Accept: application/json`；仅在有请求体时设置 `Content-Type: application/json`。
- `buildUrl` 跳过值为 `null`/`undefined` 的查询参数，数组则重复同一个 key。
- 非 2xx → `handleErrorResponse`：读取响应体文本，若 `detail` 为字符串则提取它（后端总是发送 `{"detail": "中文"}`），否则回退为 `请求失败（HTTP ${status}）`；遇到 401 时调用已注册的处理器，除非路径以 `/auth/login` 或 `/auth/me` 开头；始终抛出 `ApiError(status, detail)`。
- 网络失败 → `ApiError(0, '网络错误，无法连接服务器')`；`AbortError` 原样重新抛出。
- `request<T>`：204 → `undefined`；空响应体 → `undefined`；否则 `JSON.parse`。
- `App.tsx` 注册 `() => useAuthStore.getState().clear()`；随后 `RequireAuth` 重定向到 `/login`。
- 导出：直接调用 `fetch(buildUrl(path, { format, chapter_ids? }), { credentials: 'include' })`，其中 `chapter_ids` 是逗号拼接的字符串（后端 `export_service.parse_chapter_ids`）。响应为 blob；文件名取自 `Content-Disposition`，优先使用 RFC 5987 的 `filename*=UTF-8''<percent-encoded>`（后端发送 `attachment; filename="novel.{ext}"; filename*=UTF-8''{quote(name)}`），其次回退到普通 `filename=`，最后为 `novel.${format}`。通过 object URL + 临时 `<a download>` 下载，1 秒后撤销 URL。错误同样经过 `handleErrorResponse`。

### 4. 校验与错误矩阵

| 输入 | 结果 |
|-------|--------|
| 2xx JSON | 解析后的 `T` |
| 204 / 空响应体 | `undefined` |
| 4xx/5xx 且为 `{"detail":"…"}` | `ApiError(status, detail)` |
| 4xx/5xx 且非 JSON 或 `detail` 非字符串 | `ApiError(status, '请求失败（HTTP n）')` |
| 除 `/auth/login*`、`/auth/me*` 外任意路径的 401 | 处理器 → `clear()` → 重定向到登录页；仍然抛出 `ApiError(401, …)` |
| `/auth/login`（密码错误）或 `/auth/me`（未登录）的 401 | 仅抛出；`LoginPage` 显示 detail，`init()` 设为匿名 |
| `fetch` reject | `ApiError(0, '网络错误，无法连接服务器')` |
| signal 已中止 | 重新抛出 `DOMException` `AbortError` |
| 导出 `filename*=UTF-8''%E5%B0%8F%E8%AF%B4.txt` | `小说.txt` |
| 导出响应头的百分号编码格式错误 | 回退到普通 `filename` |
| 导出响应头缺失 | `novel.txt` / `novel.md` |

### 5. 正例 / 基准 / 反例
- 正例：`await chapterApi.update(pid, cid, { title, content })` 解析为 `Chapter`；失败时 `message.error(errorMessage(err))` 显示后端的中文 detail。
- 基准：`api.del('/projects/1')` → 204 → 解析为 `undefined`。
- 反例：对生成中的章节 PATCH content → `ApiError(409, '该章节正在生成中，请等待完成或先复位状态后再编辑')`；任何会话过期 → 全局登出，而非各页面单独处理。

### 6. 必需的测试
目前还没有覆盖 `client.ts`/`projects.ts` 的单元测试。修改它们时，需添加 Vitest 测试（stub `globalThis.fetch`），断言：
- 调用 `fetch` 时带有 `credentials: 'include'`，且 URL 以 `/api/v1` 为前缀。
- 400 且错误体为 `{"detail":"x"}` → `ApiError` 满足 `status === 400`、`detail === 'x'`；非 JSON 响应体 → 回退 detail。
- `/projects` 返回 401 时调用已注册处理器一次；`/auth/login` 和 `/auth/me` 返回 401 时不调用。
- 204 解析为 `undefined`。
- `parseContentDispositionFilename`：`filename*=UTF-8''%E5%B0%8F%E8%AF%B4.txt` → `小说.txt`；仅有 `filename="a.md"` → `a.md`；`null` → `null`。

### 7. 错误写法 vs 正确写法
```ts
// 错误：绕过了 cookie 会话、错误归一化和 401 hook
const r = await fetch('/api/v1/projects'); const data = await r.json()

// 错误：显示 "[object Object]" 或英文 HTTP 文本，而不是后端的 detail
message.error(String(err))

// 错误：先读取 ASCII 回退值，导致中文标题总是被下载为 novel.txt
/filename="?([^";]+)"?/.exec(header)

// 正确
const projects = await projectApi.list()
message.error(errorMessage(err))
parseContentDispositionFilename(resp.headers.get('content-disposition')) ?? `novel.${format}`
```

---

## 禁止模式

- `any`（lint 报错）。使用 `unknown` + 类型收窄。
- 在 `src/api/` 之外直接调用 `fetch`——应通过 `api.*`、`postSSE`/`runGeneration` 或 `exportProject`。
- 在组件内重新声明传输类型；应扩展 `types.ts`。
- 在 `ChapterUpdate` 中发送 `status: 'generating'`（类型禁止；后端拒绝）。
- 不通过 `parseServerTime`/`formatTime` 解析服务端时间戳。
- 对服务端数据做宽泛的 `as` 断言。在类型系统无法推导的地方，以下现有的窄断言是可接受的：`request` 中的 `undefined as T` / `JSON.parse(text) as T`、`WizardGenerator` 中在 `kind` 检查后的 `result as WizardItemMap['world'][]`、来自固定单选组的 `e.target.value as ExportFormat`，以及 `main.tsx` 中的非空断言 `document.getElementById('root')!`。
