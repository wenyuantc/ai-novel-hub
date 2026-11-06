import { SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'
import type { CardMappings, CardPreview, CardResolutions, CardVersion, SettingEntryKind, SettingEntryType } from '../../api/types'

export type CardEntryGroup = 'characters' | 'organizations' | 'careers'
interface LocalEntry { ref: string; kind: SettingEntryKind; name: string; setting_tags?: string[] }
export interface CardMetadata {
  version: CardVersion | null
  entries: LocalEntry[]
  external: Array<LocalEntry & { type: SettingEntryType }>
  usages: Array<{ target: string; owner: LocalEntry; types: SettingEntryType[] }>
}
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const entryType = (value: unknown): value is SettingEntryType => typeof value === 'string' && Object.hasOwn(SETTING_ENTRY_TYPE_LABELS, value)
const kind = (value: unknown): value is SettingEntryKind => value === 'character' || value === 'world' || value === 'organization' || value === 'career'
const GROUP_KIND: Record<CardEntryGroup, SettingEntryKind> = { characters: 'character', organizations: 'organization', careers: 'career' }
const cardVersion = (value: unknown): value is CardVersion => value === 1 || value === 2 || value === 3 || value === 4 || value === 5 || value === 6

/** 仅提取界面所需局部图；完整文件结构仍由后端统一校验，旧版不解释未来字段。 */
export function cardMetadata(data: unknown): CardMetadata {
  const result: CardMetadata = { version: record(data) && cardVersion(data.version) ? data.version : null, entries: [], external: [], usages: [] }
  const version = result.version ?? 0
  if (!record(data) || version < 4) return result
  const scopes = new Map<string, SettingEntryType[]>()
  const links: Array<{ target: string; owner: LocalEntry }> = []
  if (Array.isArray(data.field_definitions)) for (const definition of data.field_definitions) {
    if (record(definition) && typeof definition.ref === 'string' && definition.field_type === 'reference' && Array.isArray(definition.reference_types)) scopes.set(definition.ref, definition.reference_types.filter(entryType))
  }
  for (const group of ['characters', 'organizations', 'careers'] as const) {
    const entries = data[group]
    if (!Array.isArray(entries)) continue
    for (const entry of entries) {
      if (!record(entry) || typeof entry.ref !== 'string' || typeof entry.name !== 'string') continue
      const owner: LocalEntry = { ref: entry.ref, name: entry.name, kind: GROUP_KIND[group], ...(version >= 5 ? { setting_tags: Array.isArray(entry.setting_tags) ? entry.setting_tags.filter((tag): tag is string => typeof tag === 'string') : [] } : {}) }
      result.entries.push(owner)
      if (version >= 6 && Array.isArray(entry.ai_link_refs)) for (const ref of entry.ai_link_refs) {
        if (typeof ref === 'string') links.push({ target: ref, owner })
      }
      if (Array.isArray(entry.fields)) for (const field of entry.fields) {
        if (record(field) && typeof field.definition_ref === 'string' && record(field.value) && typeof field.value.entry_ref === 'string') {
          const types = scopes.get(field.definition_ref)
          if (types) result.usages.push({ target: field.value.entry_ref, owner, types })
        }
      }
    }
  }
  if (Array.isArray(data.external_references)) for (const entry of data.external_references) {
    if (record(entry) && typeof entry.ref === 'string' && typeof entry.name === 'string' && kind(entry.kind) && entryType(entry.type)) result.external.push({ ref: entry.ref, name: entry.name, kind: entry.kind, type: entry.type })
  }
  if (links.length) {
    const types = new Map<string, SettingEntryType>()
    for (const entry of result.entries) if (entryType(entry.kind)) types.set(entry.ref, entry.kind)
    for (const entry of result.external) types.set(entry.ref, entry.type)
    for (const link of links) {
      const type = types.get(link.target)
      if (type) result.usages.push({ ...link, types: [type] })
    }
  }
  return result
}

export interface CardTagSummary { kind: SettingEntryKind; name: string; tags: string[]; action: 'created' | 'renamed' | 'skipped' }
/** 只描述 v5+ 实际将写入的新建标签；跳过及旧版职业复用都不改变已有标签。 */
export function cardTagImportSummary(metadata: CardMetadata, resolutions: CardResolutions, preview: Pick<CardPreview, 'characters' | 'organizations' | 'careers'>): CardTagSummary[] {
  if ((metadata.version ?? 0) < 5) return []
  return metadata.entries.map((entry) => {
    const group = entry.kind === 'character' ? 'characters' : entry.kind === 'organization' ? 'organizations' : 'careers'
    const item = preview[group].find((item) => item.name === entry.name)
    return { kind: entry.kind, name: entry.name, tags: [...(entry.setting_tags ?? [])], action: !entryCreated(entry, resolutions, preview) ? 'skipped' : item?.status === 'new' ? 'created' : 'renamed' }
  })
}

/** 改为包内新建后，旧的显式目标必须移除，不能覆盖包内身份。 */
export function withoutInternalMapping(mappings: CardMappings, metadata: CardMetadata, group: CardEntryGroup, name: string): CardMappings {
  const refs = new Set(metadata.entries.filter((entry) => entry.kind === GROUP_KIND[group] && entry.name === name).map((entry) => entry.ref))
  return { definitions: { ...mappings.definitions }, references: Object.fromEntries(Object.entries(mappings.references).filter(([ref]) => !refs.has(ref))) }
}

export function cleanCardMappings(mappings: CardMappings): CardMappings {
  return { references: Object.fromEntries(Object.entries(mappings.references).map(([ref, target]) => [ref, { ...target }])), definitions: Object.fromEntries(Object.entries(mappings.definitions).map(([ref, mapping]) => [ref, {
    action: mapping.action, ...(mapping.action === 'reuse' ? { target_definition_id: mapping.target_definition_id } : mapping.label?.trim() ? { label: mapping.label.trim() } : {}), option_mappings: { ...mapping.option_mappings },
  }])) }
}

function entryCreated(entry: LocalEntry, resolutions: CardResolutions, preview: Pick<CardPreview, 'characters' | 'organizations' | 'careers'>): boolean {
  const group = entry.kind === 'character' ? 'characters' : entry.kind === 'organization' ? 'organizations' : 'careers'
  const decision = resolutions[group]?.[entry.name]
  return decision !== undefined ? decision === 'rename' : preview[group].some((item) => item.name === entry.name && (item.status === 'new' || item.status === 'rename'))
}

export function isInternalReference(ref: string, metadata: CardMetadata, resolutions: CardResolutions, preview: Pick<CardPreview, 'characters' | 'organizations' | 'careers'>): boolean {
  const entry = metadata.entries.find((entry) => entry.ref === ref)
  return !!entry && entryCreated(entry, resolutions, preview)
}

/** 只让实际导入的字段和 AI 关联限制候选范围；跳过条目的配置从不覆写目标。 */
export function referenceTypes(ref: string, targetKind: SettingEntryKind, metadata: CardMetadata, resolutions: CardResolutions,
  preview: Pick<CardPreview, 'characters' | 'organizations' | 'careers'>): SettingEntryType[] {
  const scopes = metadata.usages.filter((usage) => usage.target === ref && entryCreated(usage.owner, resolutions, preview)).map((usage) => usage.types)
  return (Object.keys(SETTING_ENTRY_TYPE_LABELS).filter(entryType)).filter((type) => type.split('.')[0] === targetKind && scopes.every((scope) => scope.includes(type)))
}
