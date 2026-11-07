import { request } from './client'
import type { QueryValue } from './client'
import type {
  MatrixColumnRef, PlanningBatch, PlanningBinding, PlanningContextPreview, PlanningDetached,
  PlanningMatrix, PlanningMerge, PlanningMutation, PlanningPage, PlanningPickerEntry, PlanningReferenceKind,
  PlanningRevision, PlanningSource, PlanningTag, PlanningTagCreate, PlanningTagMutation, PlanningTagUpdate,
  PlanningWeave, Subplot, SubplotCreate, SubplotCrossing, SubplotCrossingCreate, SubplotCrossingMutation,
  SubplotCrossingUpdate, SubplotMutation, SubplotNodeMutation, SubplotNodePut, SubplotUpdate,
} from './types'
import { invalidateProject } from '../utils/projectRefresh'

export interface PlanningMatrixQuery {
  page?: number; page_size?: number; column_page?: number
  column_mode?: 'settings' | 'subplots' | 'custom'; column_refs?: MatrixColumnRef[]
  source_kind?: 'chapter' | 'plan'; volume_no?: number; tag_id?: number; q?: string
  entity_kind?: PlanningReferenceKind; entry_q?: string
  source_refs?: PlanningSource[]
}
export interface PlanningEntryQuery { kind?: PlanningReferenceKind; category?: string; q?: string; page?: number; page_size?: number }
export interface PlanningWeaveQuery {
  axis?: 'chapter' | 'plan'; page?: number; page_size?: number; subplot_page?: number
  subplot_page_size?: number; subplot_ids?: number[]; gap_threshold?: number
}
export interface PlanningCrossingQuery { page?: number; page_size?: number; source_kind?: PlanningSource['kind']; source_id?: number; subplot_id?: number }
export interface PlanningListQuery { page?: number; page_size?: number; q?: string }
const base = (pid: number) => `/projects/${pid}/planning`
const sourceQuery = (source: PlanningSource) => ({ source_kind: source.kind, source_id: source.id })
function changed<T extends PlanningRevision>(pid: number, promise: Promise<T>) {
  return promise.then((result) => { invalidateProject(pid, ['planning', 'chapters', 'outlines']); return result })
}
export function planningMatrixQuery(query: PlanningMatrixQuery): Record<string, QueryValue> {
  const { column_refs, source_refs, ...rest } = query
  return { ...rest, column_refs: column_refs === undefined ? undefined : JSON.stringify(column_refs), source_refs: source_refs === undefined ? undefined : JSON.stringify(source_refs) }
}
/** 规划写操作均提交作者看到的版本；冲突和网络失败不自动重放。 */
export const planningApi = {
  matrix: (pid: number, query: PlanningMatrixQuery = {}, signal?: AbortSignal) =>
    request<PlanningMatrix>(`${base(pid)}/matrix`, { query: planningMatrixQuery(query), signal }),
  entries: (pid: number, query: PlanningEntryQuery = {}, signal?: AbortSignal) =>
    request<PlanningPage<PlanningPickerEntry>>(`${base(pid)}/entries`, { query: { ...query }, signal }),
  tags: (pid: number, query: PlanningListQuery = {}, signal?: AbortSignal) =>
    request<PlanningPage<PlanningTag>>(`${base(pid)}/tags`, { query: { ...query }, signal }),
  createTag: (pid: number, body: PlanningTagCreate) => changed(pid, request<PlanningTagMutation>(`${base(pid)}/tags`, { method: 'POST', body })),
  updateTag: (pid: number, id: number, body: PlanningTagUpdate) => changed(pid, request<PlanningTagMutation>(`${base(pid)}/tags/${id}`, { method: 'PATCH', body })),
  deleteTag: (pid: number, id: number, revision: number) => changed(pid, request<PlanningRevision>(`${base(pid)}/tags/${id}`, { method: 'DELETE', query: { expected_revision: revision } })),
  subplots: (pid: number, query: PlanningListQuery = {}, signal?: AbortSignal) =>
    request<PlanningPage<Subplot>>(`${base(pid)}/subplots`, { query: { ...query }, signal }),
  createSubplot: (pid: number, body: SubplotCreate) => changed(pid, request<SubplotMutation>(`${base(pid)}/subplots`, { method: 'POST', body })),
  updateSubplot: (pid: number, id: number, body: SubplotUpdate) => changed(pid, request<SubplotMutation>(`${base(pid)}/subplots/${id}`, { method: 'PATCH', body })),
  deleteSubplot: (pid: number, id: number, revision: number) => changed(pid, request<PlanningRevision>(`${base(pid)}/subplots/${id}`, { method: 'DELETE', query: { expected_revision: revision } })),
  putNode: (pid: number, subplotId: number, source: PlanningSource, body: SubplotNodePut) => changed(pid,
    request<SubplotNodeMutation>(`${base(pid)}/subplots/${subplotId}/nodes/${source.kind}/${source.id}`, { method: 'PUT', body })),
  deleteNode: (pid: number, subplotId: number, source: PlanningSource, revision: number) => changed(pid,
    request<PlanningRevision>(`${base(pid)}/subplots/${subplotId}/nodes/${source.kind}/${source.id}`, { method: 'DELETE', query: { expected_revision: revision } })),
  crossings: (pid: number, query: PlanningCrossingQuery = {}, signal?: AbortSignal) =>
    request<PlanningPage<SubplotCrossing>>(`${base(pid)}/crossings`, { query: { ...query }, signal }),
  createCrossing: (pid: number, body: SubplotCrossingCreate) => changed(pid, request<SubplotCrossingMutation>(`${base(pid)}/crossings`, { method: 'POST', body })),
  updateCrossing: (pid: number, id: number, body: SubplotCrossingUpdate) => changed(pid, request<SubplotCrossingMutation>(`${base(pid)}/crossings/${id}`, { method: 'PATCH', body })),
  deleteCrossing: (pid: number, id: number, revision: number) => changed(pid, request<PlanningRevision>(`${base(pid)}/crossings/${id}`, { method: 'DELETE', query: { expected_revision: revision } })),
  batch: (pid: number, body: PlanningBatch) => changed(pid, request<PlanningMutation>(`${base(pid)}/units/batch`, { method: 'PATCH', body })),
  bind: (pid: number, id: number, body: PlanningBinding) => changed(pid, request<PlanningMutation>(`${base(pid)}/units/${id}/binding`, { method: 'POST', body })),
  merge: (pid: number, body: PlanningMerge) => changed(pid, request<PlanningMutation>(`${base(pid)}/units/merge`, { method: 'POST', body })),
  detached: (pid: number, query: PlanningListQuery = {}, signal?: AbortSignal) =>
    request<PlanningDetached>(`${base(pid)}/detached`, { query: { ...query }, signal }),
  weave: (pid: number, query: PlanningWeaveQuery = {}, signal?: AbortSignal) =>
    request<PlanningWeave>(`${base(pid)}/weave`, { query: { ...query }, signal }),
  preview: (pid: number, source: PlanningSource, scope: 'chapter' | 'selection' = 'chapter', signal?: AbortSignal) =>
    request<PlanningContextPreview>(`${base(pid)}/units/context-preview`, { query: { ...sourceQuery(source), scope }, signal }),
}

export type PlanningReviewResource = { kind: 'tags' | 'subplots' | 'crossings'; id?: number; q?: string }
/** 冲突后的显式核对按稳定 ID 查找，名称变化与分页不会使旧条目误被当成新建。 */
export async function readPlanningReviewResource(pid: number, resource: PlanningReviewResource, signal?: AbortSignal): Promise<PlanningPage<PlanningTag | Subplot | SubplotCrossing>> {
  let page = 1
  for (;;) {
    const query = { page, page_size: 100, q: resource.id === undefined ? resource.q : undefined }
    const result = resource.kind === 'tags' ? await planningApi.tags(pid, query, signal)
      : resource.kind === 'subplots' ? await planningApi.subplots(pid, query, signal)
        : await planningApi.crossings(pid, { page, page_size: 100 }, signal)
    if (resource.id === undefined) return result
    const item = result.items.find((candidate) => candidate.id === resource.id)
    if (item || result.page * result.page_size >= result.total || result.items.length === 0) return { ...result, items: item ? [item] : [] }
    page += 1
  }
}
