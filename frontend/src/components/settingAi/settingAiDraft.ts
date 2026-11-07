import type { SettingAiSettings, SettingAiSettingsPatch } from '../../api/settingAiVisibility'
import type { SettingEntryRef } from '../../api/types'

export type SettingAiDraft = Omit<SettingAiSettings, 'ai_visibility_revision' | 'hideable_builtin_fields'>

export function settingAiDraft(value: SettingAiSettings): SettingAiDraft {
  return {
    ai_mode: value.ai_mode,
    mention_enabled: value.mention_enabled,
    mention_case_sensitive: value.mention_case_sensitive,
    mention_exclusions: [...value.mention_exclusions],
    ai_hidden_builtin_fields: [...value.ai_hidden_builtin_fields],
    links: value.links.map((link) => ({ ...link, reference: { ...link.reference } })),
  }
}

export const settingLinkKey = ({ kind, id }: SettingEntryRef) => `${kind}:${id}`
export const normalizeExclusions = (phrases: readonly string[]): string[] => [...new Set(phrases.map((item) => item.trim()))]
const sameSet = (left: string[], right: string[]) =>
  left.length === right.length && left.every((item) => right.includes(item))

export function settingAiDraftError(value: SettingAiDraft): string | null {
  const exclusions = normalizeExclusions(value.mention_exclusions)
  if (exclusions.some((item) => !item || Array.from(item).length > 80)) return '排除短语不能为空，每项最多 80 字。'
  if (exclusions.length > 20) return '最多可以设置 20 个排除短语。'
  if (value.links.length > 32) return '最多可以选择 32 个关联条目。'
  return null
}

function changedSettings(base: SettingAiSettings, draft: SettingAiDraft) {
  const exclusions = normalizeExclusions(draft.mention_exclusions)
  return {
    ai_mode: draft.ai_mode !== null && draft.ai_mode !== base.ai_mode,
    mention_enabled: draft.mention_enabled !== base.mention_enabled,
    mention_case_sensitive: draft.mention_case_sensitive !== base.mention_case_sensitive,
    mention_exclusions: !sameSet(exclusions, base.mention_exclusions),
    ai_hidden_builtin_fields: !sameSet(draft.ai_hidden_builtin_fields, base.ai_hidden_builtin_fields),
    links: !sameSet(draft.links.map((item) => settingLinkKey(item.reference)), base.links.map((item) => settingLinkKey(item.reference))),
  }
}

export function settingAiDirty(base: SettingAiSettings | null, draft: SettingAiDraft | null): boolean {
  return !!base && !!draft && Object.values(changedSettings(base, draft)).some(Boolean)
}

/** 未编辑项不随 PATCH 提交；空数组用于显式清除。 */
export function settingAiPatch(base: SettingAiSettings, draft: SettingAiDraft): SettingAiSettingsPatch {
  const changed = changedSettings(base, draft)
  return {
    expected_ai_visibility_revision: base.ai_visibility_revision,
    ...(changed.ai_mode && draft.ai_mode !== null ? { ai_mode: draft.ai_mode } : {}),
    ...(changed.mention_enabled ? { mention_enabled: draft.mention_enabled } : {}),
    ...(changed.mention_case_sensitive ? { mention_case_sensitive: draft.mention_case_sensitive } : {}),
    ...(changed.mention_exclusions ? { mention_exclusions: normalizeExclusions(draft.mention_exclusions) } : {}),
    ...(changed.ai_hidden_builtin_fields ? { ai_hidden_builtin_fields: [...draft.ai_hidden_builtin_fields] } : {}),
    ...(changed.links ? { links: draft.links.map((link) => ({ ...link.reference })) } : {}),
  }
}

/** 列表只重放作者实际添加／移除的项，不能顺便撤销另一会话刚保存的限制。 */
export function rebaseSet<T>(base: T[], draft: T[], latest: T[], key: (value: T) => string): T[] {
  const baseKeys = new Set(base.map(key))
  const draftKeys = new Set(draft.map(key))
  const values = new Map<string, T>()
  for (const item of latest) {
    const id = key(item)
    if (!baseKeys.has(id) || draftKeys.has(id)) values.set(id, item)
  }
  for (const item of draft) {
    const id = key(item)
    if (!baseKeys.has(id) && !values.has(id)) values.set(id, item)
  }
  return [...values.values()]
}

/** 只能在作者确认对照后调用；保留草稿修改过的项，未修改项采用最新数据。 */
export function rebaseSettingAiDraft(base: SettingAiSettings, draft: SettingAiDraft, latest: SettingAiSettings): SettingAiDraft {
  const merged = settingAiDraft(latest)
  const changed = changedSettings(base, draft)
  return {
    ai_mode: changed.ai_mode ? draft.ai_mode : merged.ai_mode,
    mention_enabled: changed.mention_enabled ? draft.mention_enabled : merged.mention_enabled,
    mention_case_sensitive: changed.mention_case_sensitive ? draft.mention_case_sensitive : merged.mention_case_sensitive,
    mention_exclusions: rebaseSet(base.mention_exclusions, normalizeExclusions(draft.mention_exclusions), merged.mention_exclusions, (item) => item),
    ai_hidden_builtin_fields: rebaseSet(base.ai_hidden_builtin_fields, draft.ai_hidden_builtin_fields, merged.ai_hidden_builtin_fields, (item) => item),
    links: rebaseSet(base.links, draft.links, merged.links, (item) => settingLinkKey(item.reference)),
  }
}
