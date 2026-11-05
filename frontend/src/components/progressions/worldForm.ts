import type { ProgressionJsonValue, WorldCategory, WorldSetting, WorldSettingInput } from '../../api/types'
import { asWorldCategory } from '../../utils/world'
import { cleanAliases } from '../../api/characters'

export interface WorldFormValues {
  title: string
  category: WorldCategory
  content: string
  sort: number | null
  aliases?: string[]
}

export function worldFormValues(item: WorldSetting, baseContent: string): WorldFormValues {
  return { title: item.title, category: asWorldCategory(item.category), content: baseContent, sort: item.sort, aliases: item.aliases ?? [] }
}

export function worldBaselineMatches(initial: WorldFormValues, baseline: Record<string, ProgressionJsonValue>): boolean {
  return typeof baseline.content === 'string' && baseline.content === initial.content
}

export function worldMetadataMatches(initial: WorldFormValues, latest: WorldSetting): boolean {
  return latest.title === initial.title && asWorldCategory(latest.category) === initial.category && latest.sort === initial.sort && JSON.stringify(latest.aliases ?? []) === JSON.stringify(initial.aliases ?? [])
}

export function hasWorldContentDraft(values: WorldFormValues, initial: WorldFormValues): boolean {
  return (values.content ?? '') !== initial.content
}

/** 仅发真正修改的字段，同一次请求携带基础编辑意图和时间线版本。 */
export function toWorldBaselinePatch(
  values: WorldFormValues,
  initial: WorldFormValues,
  revision: number,
): (Partial<WorldSettingInput> & { edit_mode: 'baseline'; expected_revision: number }) | null {
  const patch: Partial<WorldSettingInput> = {}
  const title = values.title.trim()
  const category = asWorldCategory(values.category)
  const content = values.content ?? ''
  const aliases = cleanAliases(values.aliases ?? []).aliases
  if (JSON.stringify(aliases) !== JSON.stringify(initial.aliases ?? [])) patch.aliases = aliases
  if (title !== initial.title.trim()) patch.title = title
  if (category !== initial.category) patch.category = category
  if (content !== initial.content) patch.content = content
  if (values.sort !== null && values.sort !== undefined && values.sort !== initial.sort) patch.sort = values.sort
  return Object.keys(patch).length ? { ...patch, edit_mode: 'baseline', expected_revision: revision } : null
}
