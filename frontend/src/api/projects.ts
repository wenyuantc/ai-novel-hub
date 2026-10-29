import { api, buildUrl, handleErrorResponse, ApiError, request, type QueryValue } from './client'
import type { CoverGenerateRequest, CoverPreview, Project, ProjectCreate, ProjectStatus, ProjectUpdate, WizardProgressUpdate } from './types'

export const projectApi = {
  list: () => api.get<Project[]>('/projects'),
  get: (id: number) => api.get<Project>(`/projects/${id}`),
  create: (body: ProjectCreate) => api.post<Project>('/projects', body),
  update: (id: number, body: ProjectUpdate) => api.patch<Project>(`/projects/${id}`, body),
  remove: (id: number) => api.del(`/projects/${id}`),
  /** 向导进度：给出的字段整体替换，返回最新 Project */
  updateWizardProgress: (id: number, body: WizardProgressUpdate) =>
    api.patch<Project>(`/projects/${id}/wizard-progress`, body),
}

/** 直接保留服务端时间戳精度作版本参数，替换封面后不会命中旧图缓存。 */
export function coverUrl(project: Pick<Project, 'id' | 'cover_updated_at'>): string | null {
  return project.cover_updated_at ? buildUrl(`/projects/${project.id}/cover`, { v: project.cover_updated_at }) : null
}

/** 手动上传与 AI 确认共用唯一的封面写入接口。 */
export function uploadCover(id: number, file: Blob): Promise<Project> {
  const form = new FormData()
  form.append('file', file, file instanceof File ? file.name : 'cover')
  return request<Project>(`/projects/${id}/cover`, { method: 'PUT', body: form })
}

export const deleteCover = (id: number) => request<Project>(`/projects/${id}/cover`, { method: 'DELETE' })
export const coverPrompt = (id: number) => api.get<{ prompt: string }>(`/projects/${id}/cover/prompt`)
export const generateCover = (id: number, body: CoverGenerateRequest, signal?: AbortSignal) =>
  api.post<CoverPreview>(`/projects/${id}/cover/generate`, body, signal)

export const PROJECT_STATUS: Record<ProjectStatus, { label: string; color: string }> = {
  planning: { label: '构思中', color: 'blue' },
  writing: { label: '连载中', color: 'green' },
  completed: { label: '已完结', color: 'gold' },
}

export type ExportFormat = 'txt' | 'md' | 'epub'

/** 从 Content-Disposition 中解析文件名，优先 RFC 5987 的 filename*=UTF-8''。 */
export function parseContentDispositionFilename(header: string | null): string | null {
  if (!header) return null
  const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(header)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
    } catch {
      // 落到普通 filename
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/.exec(header)
  return plain ? plain[1].trim() : null
}

/** X-Export-Warnings：UTF-8 百分号编码，多条以「；」分隔；解码失败时按原文处理。 */
export function decodeExportWarnings(header: string | null): string[] {
  if (!header) return []
  let text = header
  try {
    text = decodeURIComponent(header)
  } catch {
    // 不是合法的百分号编码：原样展示
  }
  return text
    .split('；')
    .map((s) => s.trim())
    .filter(Boolean)
}

export interface DownloadResult {
  filename: string
  headers: Headers
}

/** GET 附件并触发浏览器下载（blob），文件名取 Content-Disposition，没有时用 fallbackName。 */
export async function downloadAttachment(
  path: string,
  query: Record<string, QueryValue>,
  fallbackName: string,
): Promise<DownloadResult> {
  let resp: Response
  try {
    resp = await fetch(buildUrl(path, query), { credentials: 'include' })
  } catch {
    throw new ApiError(0, '网络错误，无法连接服务器')
  }
  if (!resp.ok) await handleErrorResponse(path, resp)
  const blob = await resp.blob()
  const filename = parseContentDispositionFilename(resp.headers.get('content-disposition')) ?? fallbackName
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  return { filename, headers: resp.headers }
}

export interface ExportOptions {
  /** 为空则导出全部章节（生成中的除外） */
  chapterIds?: number[]
  /** 仅 EPUB：作者名（≤100 字）；不传时后端取用户名 */
  author?: string
  /** 仅 EPUB：是否生成简介页（默认 true） */
  includeSynopsis?: boolean
}

/** 导出查询参数（纯函数）：txt / md 不带 EPUB 专属参数。 */
export function exportQuery(format: ExportFormat, options: ExportOptions = {}): Record<string, string> {
  const query: Record<string, string> = { format }
  if (options.chapterIds && options.chapterIds.length > 0) query.chapter_ids = options.chapterIds.join(',')
  if (format === 'epub') {
    if (options.author !== undefined) query.author = options.author
    query.include_synopsis = String(options.includeSynopsis ?? true)
  }
  return query
}

/** 导出并触发浏览器下载；warnings 来自 X-Export-Warnings（如跳过的空章节）。 */
export async function exportProject(
  projectId: number,
  format: ExportFormat,
  options: ExportOptions = {},
): Promise<{ filename: string; warnings: string[] }> {
  const { filename, headers } = await downloadAttachment(
    `/projects/${projectId}/export`,
    exportQuery(format, options),
    `novel.${format}`,
  )
  return { filename, warnings: decodeExportWarnings(headers.get('x-export-warnings')) }
}
