import { request, type RequestOptions } from './client'
import type { PenName, ProjectPenName } from './types'

type Scope = Pick<RequestOptions, 'signal' | 'isCurrent'>

/** 笔名库总是当前登录用户的私有资源，不接受作者 ID。 */
export const penNamesApi = {
  list: (scope?: Scope) => request<PenName[]>('/pen-names', scope),
  create: (name: string, scope?: Scope) => request<PenName>('/pen-names', { method: 'POST', body: { name }, ...scope }),
  rename: (id: number, name: string, revision: number, scope?: Scope) =>
    request<PenName>(`/pen-names/${id}`, { method: 'PATCH', body: { name, expected_revision: revision }, ...scope }),
  remove: (id: number, revision: number, scope?: Scope) =>
    request<void>(`/pen-names/${id}`, { method: 'DELETE', query: { expected_revision: revision }, ...scope }),
  setProject: (projectId: number, penNameId: number | null, scope?: Scope) =>
    request<{ project_id: number; pen_name: ProjectPenName | null }>(`/projects/${projectId}/pen-name`, {
      method: 'PUT', body: { pen_name_id: penNameId }, ...scope,
    }),
}
