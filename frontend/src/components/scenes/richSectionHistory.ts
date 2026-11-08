import { Extension } from '@tiptap/core'
import { isHistoryTransaction } from '@tiptap/pm/history'
import { Plugin } from '@tiptap/pm/state'
import type { RichDocument } from '../../api/richDocument'
import { projectSectionDocument } from './richSectionProjection'

/** 跨保存撤销/重做不能抹去已持久化的区块身份墓碑。 */
export function historyRetiredSections(document: RichDocument, saved: RichDocument): string[] {
  const active = new Set(document.sections.map((item) => item.key))
  const retired = new Set([...document.retired_section_keys, ...saved.retired_section_keys])
  for (const item of saved.sections) if (!active.has(item.key)) retired.add(item.key)
  for (const key of active) retired.delete(key)
  return [...retired]
}

export const RichSectionHistory = Extension.create<{ getSavedDocument: () => RichDocument | null }>({
  name: 'richSectionHistory', priority: 2450,
  addOptions() { return { getSavedDocument: () => null } },
  addProseMirrorPlugins() {
    const { getSavedDocument } = this.options
    return [new Plugin({ appendTransaction(transactions, _old, state) {
      if (!transactions.some(isHistoryTransaction)) return null
      const saved = getSavedDocument()
      if (!saved) return null
      const document = projectSectionDocument(state.doc.toJSON())
      const retired = historyRetiredSections(document, saved)
      if (retired.length === document.retired_section_keys.length && retired.every((key) => document.retired_section_keys.includes(key))) return null
      return state.tr.setDocAttribute('retiredSectionKeys', retired).setMeta('rich-section-history-sync', true)
        .setMeta('addToHistory', false)
    } })]
  },
})
