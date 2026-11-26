import { ApiError, rawRequest, request, type RequestOptions } from './client'
import { receiveAttachment, type DocumentFormat, type DownloadOptions } from './exports'
import type { ReaderBlock } from './reader'
import type { ChapterStatus } from './types'

export type SerializationStage = 'revision' | 'proofreading' | 'final'
export type SerializationRequestOptions = Pick<RequestOptions, 'signal' | 'isCurrent' | 'editorActor'>
export interface SerializationPage<T> {
  items: T[]
  offset: number
  limit: number
  total: number
}

export interface SerializationGoal {
  daily_goal: number | null
  revision: number
}

export interface SerializationOverview extends SerializationGoal {
  stock_count: number
  support_days: number | null
  remaining_chapters: number | null
  days: { date: string; planned_count: number; first_published_count: number }[]
}

export interface SerializationChapter {
  chapter_id: number
  chapter_no: number
  title: string
  status: ChapterStatus
  stage: SerializationStage
  planned_date: string | null
  revision: number
  is_published: boolean
  latest_edition_id: number | null
}

export interface SerializationEditionBrief {
  id: number
  seq: number
  chapter_no: number
  title: string
  created_at: string
}

export interface SerializationEdition extends SerializationEditionBrief {
  content: string
  blocks: ReaderBlock[] | null
}

export interface SerializationDraft {
  chapter_no: number
  title: string
  content: string
  blocks: ReaderBlock[] | null
  content_revision: number
  fingerprint: string
}

export interface SerializationChapterDetail {
  scene_id: number
  chapter: SerializationChapter
  draft: SerializationDraft | null
  edition: SerializationEdition | null
  editions: SerializationEditionBrief[]
  draft_changed: boolean
}

export interface SerializationPublication {
  id: number
  chapter_id: number | null
  edition_id: number
  edition_seq: number
  chapter_no: number
  title: string
  channel: string
  published_at: string
  created_at: string
}

export interface SerializationPublicationDetail {
  publication: SerializationPublication
  edition: SerializationEdition
  draft: SerializationDraft | null
  draft_changed: boolean
}

export interface SerializationDay {
  planned: SerializationPage<SerializationChapter>
  first_published: SerializationPage<SerializationPublication>
}

export interface SerializationEntryUpdate {
  expected_scene_id: number
  expected_revision: number
  stage?: Exclude<SerializationStage, 'final'>
  planned_date?: string | null
}

export interface PreflightHit {
  field: 'title' | 'content'
  term: string
  start: number
  end: number
  context: string
}

export interface PreflightPage {
  confirmation_token: string
  total: number
  offset: number
  limit: number
  hits: PreflightHit[]
}

type ReviewedWrite<T> =
  | { kind: 'saved'; result: T }
  | { kind: 'review'; page: PreflightPage; stale: boolean }

async function reviewedWrite<T>(path: string, body: unknown, opts?: SerializationRequestOptions, offset?: number): Promise<ReviewedWrite<T>> {
  try {
    const result = await request<T>(path, { ...opts, method: 'POST', body, query: { preflight_offset: offset } })
    return { kind: 'saved', result }
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 409) throw err
    const code = err.body?.code
    if (code !== 'serialization_preflight_required' && code !== 'stale_serialization_preflight') throw err
    const page = err.body?.preflight as PreflightPage | undefined
    if (!page || !page.confirmation_token || !Array.isArray(page.hits)) throw err
    return { kind: 'review', page, stale: code === 'stale_serialization_preflight' }
  }
}

export interface SerializationFinalizeRequest {
  confirmation_token?: string
  expected_scene_id: number
  expected_revision: number
  expected_content_revision: number
  expected_fingerprint: string
}

export interface SerializationPublishRequest {
  confirmation_token?: string
  expected_revision: number
  edition_id: number
  channel: string
  published_at: string
  request_id: string
}

type Paging = { offset?: number; limit?: number }
export type SerializationChapterQuery = Paging & { planned_date?: string; stage?: SerializationStage; stock_only?: boolean }
export type SerializationPublicationQuery = Paging & { channel?: string; chapter_id?: number }

function base(pid: number) {
  return `/projects/${pid}/serialization`
}

export const serializationApi = {
  overview: (pid: number, month: string, opts?: SerializationRequestOptions) =>
    request<SerializationOverview>(`${base(pid)}/overview`, { ...opts, query: { month } }),
  day: (pid: number, date: string, query: Paging = {}, opts?: SerializationRequestOptions) =>
    request<SerializationDay>(`${base(pid)}/days/${date}`, { ...opts, query }),
  chapters: (pid: number, query: SerializationChapterQuery = {}, opts?: SerializationRequestOptions) =>
    request<SerializationPage<SerializationChapter>>(`${base(pid)}/chapters`, { ...opts, query }),
  chapter: (pid: number, cid: number, editionId?: number, opts?: SerializationRequestOptions) =>
    request<SerializationChapterDetail>(`${base(pid)}/chapters/${cid}`, { ...opts, query: { edition_id: editionId } }),
  publications: (pid: number, query: SerializationPublicationQuery = {}, opts?: SerializationRequestOptions) =>
    request<SerializationPage<SerializationPublication>>(`${base(pid)}/publications`, { ...opts, query }),
  publication: (pid: number, id: number, opts?: SerializationRequestOptions) =>
    request<SerializationPublicationDetail>(`${base(pid)}/publications/${id}`, opts),
  setGoal: (pid: number, body: { daily_goal: number | null; expected_revision: number }, opts?: SerializationRequestOptions) =>
    request<SerializationGoal>(`${base(pid)}/goal`, { ...opts, method: 'PUT', body }),
  updateEntry: (pid: number, cid: number, body: SerializationEntryUpdate, opts?: SerializationRequestOptions) =>
    request<{ revision: number }>(`${base(pid)}/chapters/${cid}`, { ...opts, method: 'PATCH', body }),
  finalize: (pid: number, cid: number, body: SerializationFinalizeRequest, opts?: SerializationRequestOptions, offset?: number) =>
    reviewedWrite<SerializationEditionBrief>(`${base(pid)}/chapters/${cid}/editions`, body, opts, offset),
  publish: (pid: number, cid: number, body: SerializationPublishRequest, opts?: SerializationRequestOptions, offset?: number) =>
    reviewedWrite<{ id: number }>(`${base(pid)}/chapters/${cid}/publications`, body, opts, offset),
  downloadEdition: async (pid: number, cid: number, editionId: number, opts: DownloadOptions) => {
    if (opts.signal?.aborted || !opts.isCurrent()) return null
    const response = await rawRequest(`${base(pid)}/chapters/${cid}/editions/${editionId}/export`, {
      signal: opts.signal, isCurrent: opts.isCurrent,
    })
    return receiveAttachment(response, `定稿-${editionId}.txt`, opts)
  },
  download: async (pid: number, id: number, format: DocumentFormat, opts: DownloadOptions) => {
    if (opts.signal?.aborted || !opts.isCurrent()) return null
    const response = await rawRequest(`${base(pid)}/publications/${id}/export`, {
      query: { format }, signal: opts.signal, isCurrent: opts.isCurrent,
    })
    return receiveAttachment(response, `发布记录-${id}.${format}`, opts)
  },
}
