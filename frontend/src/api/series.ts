import { api, request } from './client'
import type { SettingAiMode } from './settingAiVisibility'
import type { WorldCategory } from './types'

export interface Series {
  id: number
  name: string
  revision: number
  book_count: number
  entry_count: number
}

export interface SeriesBook { id: number; title: string; recycled: boolean }
export interface SeriesDetail extends Series { books: SeriesBook[] }
export type SeriesKind = 'character' | 'world'
/** 此引用始终是当前书的实体 ID，不是系列条目 ID。 */
export interface SeriesBookRef { kind: SeriesKind; id: number }
export interface SeriesUse { project_id: number; ref: SeriesBookRef }
export interface SeriesAiSettings {
  ai_mode: SettingAiMode
  mention_enabled: boolean
  mention_case_sensitive: boolean
  mention_exclusions: string[]
  ai_hidden_builtin_fields: string[]
}
export interface SeriesCharacterData {
  name: string
  aliases: string[]
  profile: string
  appearance: string
  personality: string
  background: string
  gender: string
}
export interface SeriesWorldData {
  title: string
  aliases: string[]
  category: WorldCategory
  content: string
}

type EntryData =
  | { kind: 'character'; data: SeriesCharacterData }
  | { kind: 'world'; data: SeriesWorldData }

export type SeriesEntry = EntryData & { id: number; ai_settings: SeriesAiSettings; uses: SeriesUse[] }
export type SeriesEntryInput = EntryData & { ai_settings: SeriesAiSettings }
export interface SeriesEntryPage { items: SeriesEntry[]; total: number; revision: number }
export interface SeriesMutation { revision: number; item?: SeriesEntry | null; ref?: SeriesBookRef | null }

export type SeriesImpactQuery =
  | { action: 'promote'; project_id: number; kind: SeriesKind; entry_id: number }
  | { action: 'leave'; project_id: number }
  | { action: 'unuse' | 'move'; entry_id: number; project_id: number }
  | { action: 'delete'; entry_id: number }
export interface SeriesBlocker { project_id: number; title: string; recycled: boolean; reasons: string[] }
export interface SeriesImpact {
  revision: number
  blockers: SeriesBlocker[]
  affected_books: SeriesBook[]
  source_stamp: string | null
  requires_ai_settings: boolean
}

const seriesPath = (sid: number) => `/series/${sid}`
const entryPath = (sid: number, eid: number) => `${seriesPath(sid)}/entries/${eid}`

export const seriesApi = {
  list: (signal?: AbortSignal) => request<Series[]>('/series', { signal }),
  get: (sid: number, signal?: AbortSignal) => request<SeriesDetail>(seriesPath(sid), { signal }),
  create: (name: string) => api.post<Series>('/series', { name }),
  rename: (sid: number, expected_revision: number, name: string) =>
    api.patch<Series>(seriesPath(sid), { name, expected_revision }),
  remove: (sid: number, expected_revision: number) => api.del(seriesPath(sid), { expected_revision }),
  join: (sid: number, project_id: number, expected_revision: number) =>
    api.post<Series>(`${seriesPath(sid)}/books`, { project_id, expected_revision }),
  leave: (sid: number, projectId: number, expected_revision: number) =>
    request<Series>(`${seriesPath(sid)}/books/${projectId}`, { method: 'DELETE', query: { expected_revision } }),
  entries: (sid: number, query: { kind?: SeriesKind; q?: string; page: number; page_size: number }, signal?: AbortSignal) =>
    request<SeriesEntryPage>(`${seriesPath(sid)}/entries`, { query, signal }),
  entry: (sid: number, eid: number, signal?: AbortSignal) =>
    request<SeriesEntry>(entryPath(sid, eid), { signal }),
  createEntry: (sid: number, expected_revision: number, input: SeriesEntryInput) =>
    api.post<SeriesMutation>(`${seriesPath(sid)}/entries`, { ...input, expected_revision }),
  updateEntry: (sid: number, eid: number, expected_revision: number, input: {
    data?: Partial<SeriesCharacterData & SeriesWorldData>; ai_settings?: Partial<SeriesAiSettings>
  }) => api.patch<SeriesMutation>(entryPath(sid, eid), { ...input, expected_revision }),
  use: (sid: number, eid: number, project_id: number, expected_revision: number) =>
    api.post<SeriesMutation>(`${entryPath(sid, eid)}/uses`, { project_id, expected_revision }),
  unuse: (sid: number, eid: number, projectId: number, expected_revision: number) =>
    request<SeriesMutation>(`${entryPath(sid, eid)}/uses/${projectId}`, { method: 'DELETE', query: { expected_revision } }),
  move: (sid: number, eid: number, project_id: number, expected_revision: number) =>
    api.post<SeriesMutation>(`${entryPath(sid, eid)}/move-to-book`, { project_id, expected_revision }),
  removeEntry: (sid: number, eid: number, expected_revision: number) =>
    request<SeriesMutation>(entryPath(sid, eid), { method: 'DELETE', query: { expected_revision } }),
  promote: (sid: number, body: {
    project_id: number; ref: SeriesBookRef; source_stamp: string; expected_revision: number; ai_settings?: Partial<SeriesAiSettings>
  }) => api.post<SeriesMutation>(`${seriesPath(sid)}/entries/promote`, body),
  impact: (sid: number, query: SeriesImpactQuery, signal?: AbortSignal) =>
    request<SeriesImpact>(`${seriesPath(sid)}/impact`, { query, signal }),
}
