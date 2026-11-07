import { ApiError } from '../../api/client'
import type { SeriesAiSettings, SeriesEntry, SeriesKind } from '../../api/series'
import type { CharacterInput, SettingOrigin } from '../../api/types'
import type { ProjectResource } from '../../utils/projectRefresh'
import { rebaseSet } from '../settingAi/settingAiDraft'

export const SERIES_AI_DEFAULTS: SeriesAiSettings = {
  ai_mode: 'detected', mention_enabled: true, mention_case_sensitive: true,
  mention_exclusions: [], ai_hidden_builtin_fields: [],
}

export type SettingScope = 'all' | 'book' | 'series'
export const SERIES_SCOPE_OPTIONS: { value: SettingScope; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'book', label: '本书' },
  { value: 'series', label: '系列' },
]

export function inSettingScope(origin: SettingOrigin | undefined, scope: SettingScope): boolean {
  return scope === 'all' || (origin?.scope === 'series' ? scope === 'series' : scope === 'book')
}

export function seriesEntryName(entry: SeriesEntry) {
  return entry.kind === 'character' ? entry.data.name : entry.data.title
}

export function projectReference(entry: SeriesEntry, projectId: number) {
  return entry.uses.find((use) => use.project_id === projectId && use.ref.kind === entry.kind)?.ref
}

export function seriesSettingResources(kind: SeriesKind): readonly ProjectResource[] {
  return [kind === 'character' ? 'characters' : 'world', 'planning', 'setting-ai-visibility', 'setting-categories']
}

export function seriesNeedsReview(err: unknown) {
  return !(err instanceof ApiError) || err.status === 0 || err.status >= 500
    || (err.status === 409 && ['stale_revision', 'series_source_changed'].some((code) => code === err.body?.code))
}

/** 数组是完整的有序字段，只有作者确实修改的键进入 PATCH。 */
export function diffSeriesValues<T extends object>(before: T, draft: T): Partial<T> {
  return Object.fromEntries(Object.entries(draft).filter(([key, value]) =>
    JSON.stringify(before[key as keyof T]) !== JSON.stringify(value),
  )) as Partial<T>
}

/** 并发新增的隐藏字段及排除短语不能被旧草稿整体覆盖。 */
export function rebaseSeriesAi(base: SeriesAiSettings, draft: SeriesAiSettings, latest: SeriesAiSettings): SeriesAiSettings {
  return {
    ...latest,
    ...diffSeriesValues(base, draft),
    ai_hidden_builtin_fields: rebaseSet(base.ai_hidden_builtin_fields, draft.ai_hidden_builtin_fields, latest.ai_hidden_builtin_fields, (value) => value),
    mention_exclusions: rebaseSet(base.mention_exclusions, draft.mention_exclusions, latest.mention_exclusions, (value) => value),
  }
}

const BOOK_CHARACTER_FIELDS = new Set<keyof CharacterInput>([
  'age', 'current_state', 'life_status', 'role_type', 'sort', 'always_in_context', 'relations_text',
])

export function bookCharacterPatch(patch: Partial<CharacterInput>): Partial<CharacterInput> {
  return Object.fromEntries(Object.entries(patch).filter(([key]) => BOOK_CHARACTER_FIELDS.has(key as keyof CharacterInput)))
}
