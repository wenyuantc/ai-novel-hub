import { api, buildUrl, handleErrorResponse, ApiError } from './client'
import type { Project, ProjectCreate, ProjectStatus, ProjectUpdate } from './types'

export const projectApi = {
  list: () => api.get<Project[]>('/projects'),
  get: (id: number) => api.get<Project>(`/projects/${id}`),
  create: (body: ProjectCreate) => api.post<Project>('/projects', body),
  update: (id: number, body: ProjectUpdate) => api.patch<Project>(`/projects/${id}`, body),
  remove: (id: number) => api.del(`/projects/${id}`),
}

export const PROJECT_STATUS: Record<ProjectStatus, { label: string; color: string }> = {
  planning: { label: '构思中', color: 'blue' },
  writing: { label: '连载中', color: 'green' },
  completed: { label: '已完结', color: 'gold' },
}

export type ExportFormat = 'txt' | 'md'

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

/** 导出并触发浏览器下载。 */
export async function exportProject(projectId: number, format: ExportFormat, chapterIds?: number[]): Promise<string> {
  const path = `/projects/${projectId}/export`
  const query: Record<string, string> = { format }
  if (chapterIds && chapterIds.length > 0) query.chapter_ids = chapterIds.join(',')
  let resp: Response
  try {
    resp = await fetch(buildUrl(path, query), { credentials: 'include' })
  } catch {
    throw new ApiError(0, '网络错误，无法连接服务器')
  }
  if (!resp.ok) await handleErrorResponse(path, resp)
  const blob = await resp.blob()
  const filename = parseContentDispositionFilename(resp.headers.get('content-disposition')) ?? `novel.${format}`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  return filename
}
