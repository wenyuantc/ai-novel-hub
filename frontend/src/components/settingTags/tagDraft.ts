import type { SettingEntryRef, SettingTagsOut } from '../../api/types'
import type { ProjectResource } from '../../utils/projectRefresh'

// 与后端 Python str.strip 的 Unicode 空白集合一致，BOM 不属于空白。
const WHITESPACE = new Set([0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000])
function whitespace(code: number) { return (code >= 9 && code <= 13) || (code >= 28 && code <= 32) || (code >= 0x2000 && code <= 0x200a) || WHITESPACE.has(code) }
export function stripSettingWhitespace(value: string) {
  let start = 0
  let end = value.length
  while (start < end && whitespace(value.charCodeAt(start))) start += 1
  while (end > start && whitespace(value.charCodeAt(end - 1))) end -= 1
  return value.slice(start, end)
}

export function normalizeSettingTags(raw: unknown): string[] {
  if (!Array.isArray(raw)) throw new Error('整理标签必须是字符串数组')
  const result: string[] = []
  const seen = new Set<string>()
  for (const item of raw) {
    if (typeof item !== 'string') throw new Error('整理标签必须是字符串')
    const tag = stripSettingWhitespace(item)
    if (!tag) continue
    if (Array.from(tag).length > 50) throw new Error('每个整理标签不能超过 50 字')
    if (!seen.has(tag)) {
      seen.add(tag)
      result.push(tag)
      if (result.length > 50) throw new Error('每条设定最多保存 50 个整理标签')
    }
  }
  return result
}

export function sameTags(left: readonly string[], right: readonly string[]) {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

export function tagSnapshot(value: SettingTagsOut, entry: SettingEntryRef): SettingTagsOut {
  if (value.entry.reference.kind !== entry.kind || value.entry.reference.id !== entry.id) throw new Error('整理标签响应与当前条目不一致，请重试')
  return { ...value, entry: { ...value.entry, reference: { ...value.entry.reference } }, setting_tags: normalizeSettingTags(value.setting_tags) }
}

export function preventTagFormSubmit(event: { key: string; preventDefault: () => void }) {
  if (event.key === 'Enter') event.preventDefault()
}

export function settingTagResources(kind: SettingEntryRef['kind']): readonly ProjectResource[] {
  const resource: Record<SettingEntryRef['kind'], ProjectResource> = { character: 'characters', world: 'world', organization: 'organizations', career: 'careers' }
  return ['setting-categories', resource[kind]]
}
