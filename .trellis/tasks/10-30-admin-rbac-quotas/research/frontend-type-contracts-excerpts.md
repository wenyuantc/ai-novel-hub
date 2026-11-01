# 前端类型与客户端契约：精确摘录

更新日期：2026-11-02。下文来自已同步RBAC的实际规范，按当前行号精确摘录；新增完整权限/配额契约另由rbac-quotas.md注入。

来源：`.trellis/spec/frontend/type-safety.md`；SHA-256：`d64e33966f85d628178241a3b5e0fcf1fbb2c08a27ac7a8f223e43585b03edf8`。

## 原规范第1–19行

# 类型安全

RBAC传输镜像app/schemas/rbac.py：User必含is_super_admin/roles/effective_permissions:PermissionCode[]/authorization_revision，AdminUser必含is_super_admin/is_management_account/roles/authorization_revision但不含目标完整权限；不能将新字段改optional迁就旧fixture，或用is_admin fallback。[完整角色/配额API与错误契约](../backend/rbac-quotas.md)。QuotaConfig为inherit/unlimited/limited(value)判别联合，limited严格整数0..2147483647；revision（对象）与authorization_revision（全局）不能混用。

管理员系统设置镜像 `SystemSettingsOut/SystemSettingsUpdate` 与bool/int区分式items；该PATCH的null明确表示取消后台覆盖，不是忽略。审计settings_changes仅六项受限值，公开配置请求代次独立于登录态；详见[系统设置](../backend/system-settings.md)。

管理员任务的AdminTaskBrief/Detail/List/Counts、AdminTaskListQuery及AdminTaskCancelRequest/Out独立镜像schemas/admin_task.py，不能把普通Task通过强转复用；未知kind/status为unknown、旧入队时间/本轮时长为null。UUID深链比较规范化，原因按Unicode字符计数及控制字符规则校验；task.cancel_request审计新增专用有界字段，其成功只表示受理。详见[任务监控](../backend/admin-task-monitoring.md)。

> 本项目中的类型安全模式。

---

## 概述

TypeScript ~5.8，启用严格模式（`frontend/tsconfig.app.json`：`strict`、`noUnusedLocals`、`noUnusedParameters`、`noFallthroughCasesInSwitch`、`verbatimModuleSyntax`）。ESLint 强制 `'@typescript-eslint/no-explicit-any': 'error'`。没有运行时校验库（不用 Zod/Yup）：默认信任响应体与 `src/api/types.ts` 一致，而该文件镜像后端的 Pydantic schema。不可信的数据结构（错误响应体、SSE 载荷）通过 `unknown` + 类型守卫进行收窄。

---

## 类型组织

## 原规范第65–66行

- 认证/管理员类型：`User.is_admin` 镜像 `UserOut.is_admin`；`Page<T> = { items: T[]; total: number }` 镜像 `AdminUserList`；`AdminUser` 镜像 `AdminUserOut`（`id, username, is_admin, is_active, created_at, project_count`）；`AdminUserCreate`（username/password）、`AdminUserUpdate`（仅is_active?；is_admin会被后端422拒绝）、`ResetPasswordRequest`、`ChangePasswordRequest`。后端契约：`.trellis/spec/backend/auth-admin.md`。
- 用户详情专用 `AdminUserDetail extends AdminUser`，增加 `last_login_at: string|null` 与usage三组对象；active/recycled/deleting均为 `AdminResourceUsage {project_count,chapter_count,total_words}`（后端三组容器名为AdminUserUsage，TS直接内联对象，不另造命名类型）。顶层project_count仍为三组合计，不能改成正常数；原列表/写响应不增加字段。`adminApi.getUser(id, signal?)`沿统一客户端。`parseAdminUserId`仅接受正十进制安全整数，拒绝1e2、小数、负数和越界值；null登录单独显示“暂无记录”，非空时间用formatTime。

## 原规范第104–130行

### PATCH 语义（必须与后端 service 一致）

| 资源 | 规则 | 后端依据 |
|----------|------|------------------|
| 项目、世界观/角色/大纲 | 省略**或** `null` = 不变 | `model_dump(exclude_unset=True)` + `if value is None: continue`（`project_service.update_project`、`story_service.update_owned`） |
| 章节 | 对 `chapter_no`、`title`、`content`、`status` 而言省略/`null` = 不变；显式发送 `outline_id: null` 会**解除**与大纲的关联 | `chapter_service.update_chapter` 检查 `"outline_id" in fields` |
| `generating` 中的章节 | PATCH 带 `title`/`content` 且不带 `status` → 409；仅发送 `{ status: 'draft' }` 是复位路径 | `ConflictError("该章节正在生成中，请等待完成或先复位状态后再编辑")` |
| 管理员用户 | 省略is_active=不变；角色整体替换走专用PUT并带authorization_revision，旧is_admin字段禁止；禁用会递增 `token_version`（目标用户的会话 → 401） | `admin_service.update_user` |
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


## 原规范第137–257行

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
任何对 `src/api/client.ts`、`src/api/projects.ts` 中 `exportProject` / `downloadAttachment` / `parseContentDispositionFilename` / `decodeExportWarnings`、`src/api/transfer.ts`、后端错误格式（`backend/app/core/errors.py`）、认证 cookie 或导出端点（`GET /api/v1/projects/{id}/export`）的修改。

### 2. 签名
```ts
export const API_BASE = '/api/v1'
export class ApiError extends Error { readonly status: number; readonly detail: string; readonly body: Record<string, unknown> | null }
export function setUnauthorizedHandler(handler: (() => void) | null): void
export function buildUrl(path: string, query?: Record<string, QueryValue>): string
export async function readErrorBody(resp: Response): Promise<{ detail: string; body: Record<string, unknown> | null }>
export async function readErrorDetail(resp: Response): Promise<string>
export async function handleErrorResponse(path: string, resp: Response): Promise<never>
export async function rawRequest(path: string, options?: RequestOptions): Promise<Response>
export async function request<T>(path: string, options?: RequestOptions): Promise<T>
export const api: { get<T>(path, query?); post<T>(path, body?, signal?); patch<T>(path, body); put<T>(path, body); del(path, query?): Promise<void>; upload<T>(path, form: FormData, signal?) }
export function errorMessage(err: unknown): string
export function parseContentDispositionFilename(header: string | null): string | null
export type ExportFormat = 'txt' | 'md' | 'epub'
export function decodeExportWarnings(header: string | null): string[]   // decodeURIComponent 后按「；」切分，非法编码按原文
export async function downloadAttachment(path, query, fallbackName): Promise<{ filename: string; headers: Headers }>
export function exportQuery(format, { chapterIds?, author?, includeSynopsis? }): Record<string, string>   // EPUB 才带 author / include_synopsis
export async function exportProject(projectId, format, options?: ExportOptions): Promise<{ filename: string; warnings: string[] }>
// src/api/transfer.ts（只被懒加载的备份 / 导入弹窗引用）
export function backupQuery(options: BackupOptions): Record<string, boolean>   // generation_logs → logs
export const transferApi: { estimate(pid); download(pid, options): Promise<string>; preview(file, signal?): Promise<ImportPreview>; importBackup({token, title?}): Promise<ImportResult> }
```

### 3. 契约
- 每个请求都使用 `credentials: 'include'`（HttpOnly cookie 会话；JS 中不持有 token）。请求头：`Accept: application/json`；仅在有 JSON 请求体时设置 `Content-Type: application/json`；请求体为 `FormData`（`api.upload`，知识库上传）时原样发送、**不设置** `Content-Type`（由浏览器带 multipart boundary）。
- `buildUrl` 跳过值为 `null`/`undefined` 的查询参数，数组则重复同一个 key。
- 非 2xx → `handleErrorResponse`：读取响应体文本，若 `detail` 为字符串则提取它（后端总是发送 `{"detail": "中文"}`），否则回退为 `请求失败（HTTP ${status}）`；响应体是 JSON 对象时整体保存在 `ApiError.body`（后端 `AppError.extra` 的结构化字段：模板校验 400 的 `errors`、删除被引用风格 409 的 `references`），只能经 `utils/prompts.ts` 的 `issuesFromError` / `utils/styles.ts` 的 `readStyleReferences` 收窄读取，非 JSON 时为 null；仅当前认证请求代次的401调用已注册处理器，除非路径以 `/auth/login` 或 `/auth/me` 开头；始终抛出 `ApiError(status, detail)`。
- 网络失败 → `ApiError(0, '网络错误，无法连接服务器')`；`AbortError` 原样重新抛出。
- `request<T>`：204 → `undefined`；空响应体 → `undefined`；否则 `JSON.parse`。
- `App.tsx` 注册 `() => useAuthStore.getState().clear()`；随后 `RequireAuth` 重定向到 `/login`。
- 导出 / 备份下载：`downloadAttachment` 直接调用 `fetch(buildUrl(path, query), { credentials: 'include' })`（导出 query = `exportQuery`：`{ format, chapter_ids?, author?, include_synopsis? }`；备份 query = `backupQuery`），其中 `chapter_ids` 是逗号拼接的字符串（后端 `export_service.parse_chapter_ids`）。响应为 blob；文件名取自 `Content-Disposition`，优先使用 RFC 5987 的 `filename*=UTF-8''<percent-encoded>`（后端发送 `attachment; filename="novel.{ext}"; filename*=UTF-8''{quote(name)}`），其次回退到普通 `filename=`，最后为 `novel.${format}`。通过 object URL + 临时 `<a download>` 下载，1 秒后撤销 URL。错误同样经过 `handleErrorResponse`。`X-Export-Warnings`（EPUB 跳过的空章节等）是 UTF-8 百分号编码、多条以「；」分隔，后端 CORS 已 `expose_headers`；`exportProject` 用 `decodeExportWarnings` 解码后返回 `warnings`。
- 备份类型（`types.ts`）：`BackupOptions` / `BackupEstimate` / `ImportPreview` / `ImportStyleBrief`（`action: preset|reuse|create|copy`）/ `ImportRequest` / `ImportResult` 镜像 `backend/app/schemas/transfer.py`。预览成功体**没有** `errors`：文件不合法时为 400，问题列表在 `ApiError.body.errors`，只能经 `utils/transfer.ts` 的 `importFileErrors` 收窄读取。导入用 JSON `{token, title?}`（后端也接受 multipart 直接导入，前端不用）。

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
`src/api/client.test.ts`（`10-23-knowledge-base` 新增）覆盖 `client.ts` 的前四条与 FormData 上传；`src/api/projects.test.ts`（`10-23-project-io-reader` 新增）覆盖 `parseContentDispositionFilename`、`decodeExportWarnings`、`exportQuery`、`backupQuery`（下载本身仍没有单元测试）。修改它们时，需添加 / 更新 Vitest 测试（stub `globalThis.fetch`），断言：
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
- 对服务端数据做宽泛的 `as` 断言。在类型系统无法推导的地方，以下现有的窄断言是可接受的：`request` 中的 `undefined as T` / `JSON.parse(text) as T`、`WizardGenerator` 中在 `kind` 检查后的 `result as WizardItemMap['world'][]`、来自固定单选组的 `e.target.value as ExportFormat`、`utils/transfer.ts` 中对后端 `action: str` 的 `action as ImportStyleAction`（查表失败有回退），以及 `main.tsx` 中的非空断言 `document.getElementById('root')!`。
