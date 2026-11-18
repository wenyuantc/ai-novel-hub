import { Extension } from '@tiptap/core'
import { isHistoryTransaction } from '@tiptap/pm/history'
import { Plugin, type EditorState, type Transaction } from '@tiptap/pm/state'
import type { RichDocument } from '../../api/richDocument'
import { historyRetiredSections, rebaseDraftDocument } from './sceneDraftEvidence'
import { projectSectionDocument } from './richSectionProjection'

export { historyRetiredSections }
export const SAVE_BASELINE_SYNC = 'rich-scene-save-sync'

/** 对齐服务端新确认的墓碑与失效摘录，保持选区、正文和撤销历史。 */
export function syncSceneSaveBaseline(state: EditorState, saved: RichDocument): Transaction | null {
  const before = projectSectionDocument(state.doc.toJSON())
  const next = rebaseDraftDocument(before, saved)
  if (before === next) return null
  const transaction = state.tr
  if (JSON.stringify(next.retired_section_keys) !== JSON.stringify(before.retired_section_keys)) {
    transaction.setDocAttribute('retiredSectionKeys', next.retired_section_keys)
  }
  if (JSON.stringify(next.markers) !== JSON.stringify(before.markers)) {
    transaction.setDocAttribute('markers', next.markers)
  }
  // 既有标记插件不重新推导这笔业已校验的属性同步。
  return transaction.setMeta(SAVE_BASELINE_SYNC, true).setMeta('rich-marker-sync', true).setMeta('addToHistory', false)
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
