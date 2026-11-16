import type { DocumentExportRequest, DocumentFormat, StoryContent, StoryScope, StoryUnit } from '../../api/exports'
import type { SettingEntryKind, SettingEntryRef } from '../../api/types'

export interface ExportItem {
  /** 章节、场景、聊天与笔记用数字字符串，设定使用 kind:id。 */
  key: string
  label: string
  description?: string
  disabled?: boolean
}

/** 分页勾选只影响本页，保留其他页已选名称；禁用项不能被批量选中。 */
export function updateSelection(selected: ExportItem[], items: ExportItem[], checked: boolean): ExportItem[] {
  const keys = new Set(items.filter((item) => !item.disabled).map((item) => item.key))
  if (!checked) return selected.filter((item) => !keys.has(item.key))
  const existing = new Set(selected.map((item) => item.key))
  const added: ExportItem[] = []
  for (const item of items) {
    if (!item.disabled && !existing.has(item.key)) {
      existing.add(item.key)
      added.push(item)
    }
  }
  return [...selected, ...added]
}

export interface ExportSelection {
  format: DocumentFormat
  scope: StoryScope
  unit: StoryUnit
  content: StoryContent
  chapters: ExportItem[]
  scenes: ExportItem[]
  settings: ExportItem[]
  conversations: ExportItem[]
  notes: ExportItem[]
}

function numericId(value: string): number {
  const id = Number(value)
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(id) || id < 1) throw new Error('所选条目身份无效，请重新选择')
  return id
}

function settingRef(item: ExportItem): SettingEntryRef {
  const [kind, raw, extra] = item.key.split(':')
  if (extra !== undefined || !raw || !['character', 'world', 'organization', 'career'].includes(kind)) {
    throw new Error('所选设定身份无效，请重新选择')
  }
  return { kind: kind as SettingEntryKind, id: numericId(raw) }
}

export function buildDocumentRequest(selection: ExportSelection): DocumentExportRequest {
  const story = selection.unit === 'chapter' ? selection.chapters : selection.scenes
  if (selection.scope === 'selected' && !story.length) throw new Error('请选择要导出的章节或场景')
  if (selection.scope === 'none' && !selection.settings.length && !selection.conversations.length && !selection.notes.length) {
    throw new Error('请选择正文、摘要或创作资料')
  }
  return {
    format: selection.format,
    story: {
      scope: selection.scope, unit: selection.unit, content: selection.content,
      ids: selection.scope === 'selected' ? story.map((item) => numericId(item.key)) : [],
    },
    materials: {
      settings: selection.settings.map(settingRef),
      conversation_ids: selection.conversations.map((item) => numericId(item.key)),
      note_ids: selection.notes.map((item) => numericId(item.key)),
    },
  }
}
