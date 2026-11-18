import { rawRequest, request, type RequestOptions } from './client'
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

export interface SerializationFinalizeRequest {
  expected_scene_id: number
  expected_revision: number
  expected_content_revision: number
  expected_fingerprint: string
}

export interface SerializationPublishRequest {
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
  finalize: (pid: number, cid: number, body: SerializationFinalizeRequest, opts?: SerializationRequestOptions) =>
    request<SerializationEditionBrief>(`${base(pid)}/chapters/${cid}/editions`, { ...opts, method: 'POST', body }),
  publish: (pid: number, cid: number, body: SerializationPublishRequest, opts?: SerializationRequestOptions) =>
    request<{ id: number }>(`${base(pid)}/chapters/${cid}/publications`, { ...opts, method: 'POST', body }),
  download: async (pid: number, id: number, format: DocumentFormat, opts: DownloadOptions) => {
    if (opts.signal?.aborted || !opts.isCurrent()) return null
    const response = await rawRequest(`${base(pid)}/publications/${id}/export`, {
      query: { format }, signal: opts.signal, isCurrent: opts.isCurrent,
    })
    return receiveAttachment(response, `发布记录-${id}.${format}`, opts)
  },
}
