import { request } from './client'
import type { Character, SettingEntryKind, WorldCategory, WorldSetting } from './types'

export type MentionSourceType = 'chapter' | 'chat' | 'note' | 'knowledge'
export type MentionEntityKind = SettingEntryKind
export type MentionCategory = Exclude<SettingEntryKind, 'world'> | WorldCategory
export interface MentionEntity { kind: MentionEntityKind; id: number; name: string; category: MentionCategory; aliases: string[] }
export interface MentionSpan { start: number; end: number; text: string; entities: MentionEntity[] }
export interface MentionAnchor { start: number; end: number; content_hash: string }
export interface MentionSource { type: MentionSourceType; id: number; title: string; chapter_no: number | null; conversation_id: number | null; created_at: string | null }
export interface MentionOccurrence extends MentionAnchor { source: MentionSource; text: string; context_before: string; context_after: string; entities: MentionEntity[] }
export interface MentionCatalog { entities: MentionEntity[]; dictionary_hash: string; available_source_types: MentionSourceType[] }
export interface MentionPreview { spans: MentionSpan[]; content_hash: string; dictionary_hash: string }
export interface MentionPage { items: MentionOccurrence[]; total: number; page: number; page_size: number; source_counts: Record<MentionSourceType, number>; dictionary_hash: string }
export interface MentionChapter { id: number; chapter_no: number; title: string; has_content: boolean }
export interface MentionChapterCount { chapter_id: number; count: number; ambiguous_count: number }
export interface MentionSummaryEntry { entity: MentionEntity; mention_count: number; ambiguous_count: number; chapter_count: number; last_chapter_no: number | null; gap_chapters: number | null; counts: MentionChapterCount[] }
export interface MentionSummary { chapters: MentionChapter[]; entries: MentionSummaryEntry[]; total: number; page: number; page_size: number }
export interface MentionSourceWindow { source: MentionSource; content_hash: string; stale: boolean; window_start: number; text: string; spans: MentionSpan[]; target_start: number | null; target_end: number | null }
export interface MentionQuery { entity_kind?: MentionEntityKind; entity_id?: number; source_type?: MentionSourceType; chapter_from?: number; chapter_to?: number; page?: number; page_size?: number }
export interface MentionSummaryQuery extends Omit<MentionQuery, 'source_type'> { category?: MentionCategory; q?: string }
const base = (pid: number) => `/projects/${pid}/mentions`
export const mentionApi = {
  character: (pid: number, id: number, signal?: AbortSignal) => request<Character>(`/projects/${pid}/characters/${id}`, { signal }),
  world: (pid: number, id: number, signal?: AbortSignal) => request<WorldSetting>(`/projects/${pid}/world/${id}`, { signal }),
  catalog: (pid: number, signal?: AbortSignal) => request<MentionCatalog>(`${base(pid)}/catalog`, { signal }),
  preview: (pid: number, text: string, signal?: AbortSignal) => request<MentionPreview>(`${base(pid)}/preview`, { method: 'POST', body: { text }, signal }),
  list: (pid: number, query: MentionQuery = {}, signal?: AbortSignal) => request<MentionPage>(base(pid), { query: { ...query }, signal }),
  summary: (pid: number, query: MentionSummaryQuery = {}, signal?: AbortSignal) => request<MentionSummary>(`${base(pid)}/summary`, { query: { ...query }, signal }),
  source: (pid: number, type: MentionSourceType, id: number, anchor?: Partial<MentionAnchor>, signal?: AbortSignal) => request<MentionSourceWindow>(`${base(pid)}/sources/${type}/${id}`, { query: { ...anchor }, signal }),
}
export const MENTION_SOURCE_LABELS: Record<MentionSourceType, string> = { chapter: '正文', chat: '聊天', note: '笔记', knowledge: '资料' }
export const MENTION_CATEGORY_LABELS: Record<MentionCategory, string> = {
  character: '角色', place: '地点', faction: '世界观势力', item: '物品', rule: '世界观规则', history: '世界观历史', other: '其他世界观',
  organization: '组织', career: '职业',
}
