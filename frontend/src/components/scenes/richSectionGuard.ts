import { Extension } from '@tiptap/core'
import { isHistoryTransaction } from '@tiptap/pm/history'
import { Plugin } from '@tiptap/pm/state'
import type { RichDocument } from '../../api/richDocument'
import { repairRichPrefixes, verifyRichPrefixRepair } from './richPrefixRepair'
import { historyRetiredSections } from './richSectionHistory'
import { mapSectionMarkerTransaction } from './richSectionMarkerMapping'
import { projectSectionDocument } from './richSectionProjection'
import { mappedSectionIdentities, verifySectionMetadataTransaction, verifySectionPlanTransaction } from './richSectionTransaction'
import { validSectionDocument } from './richSectionValidity'

/** 校验正文、区块和标记事务；无法证明结构映射时拒绝修改并保留原稿。 */
export const RichSectionGuard = Extension.create<{ getSavedDocument: () => RichDocument | null; isComposing: () => boolean }>({
  name: 'richSectionGuard', priority: 2500,
  addOptions() { return { getSavedDocument: () => null, isComposing: () => false } },
  addProseMirrorPlugins() {
    const editor = this.editor; const { getSavedDocument, isComposing } = this.options
    const rejected = () => queueMicrotask(() => { if (!editor.isDestroyed) editor.view.dom.dispatchEvent(new CustomEvent('rich-mapping-rejected')) })
    return [new Plugin({ filterTransaction(transaction, state) {
      if (!transaction.docChanged) return true
      try {
        if (transaction.getMeta('rich-prefix-fix')) {
          const after = projectSectionDocument(transaction.doc.toJSON())
          if (!verifyRichPrefixRepair(transaction) || !validSectionDocument(after)) throw new Error('分隔符修复不可信')
          return true
        }
        const before = projectSectionDocument(state.doc.toJSON())
        let after: RichDocument | null = null
        try { after = projectSectionDocument(transaction.doc.toJSON()) } catch { /* 尝试安全的逐目标分隔符修复 */ }
        // 标题/列表切换可能暂时重置 prefix，只有逐目标逐段可证明时才允许修复。
        if (!after || !validSectionDocument(after, false)) {
          const fixed = repairRichPrefixes(state.doc, transaction.doc)
          if (!fixed) throw new Error('无法修复分隔符')
          after = projectSectionDocument(fixed.doc.toJSON())
          if (!validSectionDocument(after, false)) throw new Error('结构无效')
        }
        if (transaction.getMeta('rich-section-plan')) {
          const saved = getSavedDocument()
          if (!saved || !verifySectionPlanTransaction(transaction, state, saved) || !validSectionDocument(after)) throw new Error('区块计划不可信')
          return true
        }
        if (transaction.getMeta('rich-section-intent')) {
          if (!verifySectionMetadataTransaction(transaction, state) || !validSectionDocument(after)) throw new Error('区块元数据事务不可信')
          return true
        }
        if (transaction.getMeta('rich-section-history-sync')) {
          const saved = getSavedDocument()
          if (!saved) throw new Error('历史缺少已保存基线')
          const expected = state.tr.setDocAttribute('retiredSectionKeys', historyRetiredSections(before, saved))
          if (!expected.doc.eq(transaction.doc) || JSON.stringify(expected.steps.map((item) => item.toJSON()))
            !== JSON.stringify(transaction.steps.map((item) => item.toJSON())) || !validSectionDocument(after)) throw new Error('历史身份同步无效')
          return true
        }
        if (isHistoryTransaction(transaction)) {
          const saved = getSavedDocument()
          const normalized = saved ? { ...after, retired_section_keys: historyRetiredSections(after, saved) } : after
          if (!validSectionDocument(normalized)) throw new Error('历史结构无效')
          return true
        }
        if (!mappedSectionIdentities(transaction)) throw new Error('区块身份未可靠映射')
        const original = new Map(before.sections.map((item) => [item.key, item]))
        if (after.sections.some((item) => {
          const old = original.get(item.key)
          return !old || old.title !== item.title || old.include_in_ai !== item.include_in_ai
        }) || JSON.stringify(before.retired_section_keys) !== JSON.stringify(after.retired_section_keys)) throw new Error('区块资料缺少显式事务')
        if (transaction.getMeta('rich-marker-intent') || transaction.getMeta('rich-marker-sync')) {
          const expected = state.tr.setDocAttribute('markers', transaction.doc.attrs.markers)
          if (!expected.doc.eq(transaction.doc) || JSON.stringify(expected.steps.map((item) => item.toJSON()))
            !== JSON.stringify(transaction.steps.map((item) => item.toJSON()))) throw new Error('标记事务不可信')
          if (!validSectionDocument(after)) throw new Error('标记结构无效')
        } else {
          if (JSON.stringify(before.markers) !== JSON.stringify(after.markers)) throw new Error('标记缺少显式事务')
          // 组合期间文档属性暂留上次提交的标记；RichMarkerState 使用事务间 pending 快照逐步校验，
          // 不能再拿已经失效的旧属性对后续候选文字做第二次映射。
          if (isComposing()) return true
          const markers = mapSectionMarkerTransaction(transaction, before.markers, getSavedDocument())
          if (!markers || !validSectionDocument({ ...after, markers })) throw new Error('标记无法可靠映射')
        }
        return true
      } catch { rejected(); return false }
    } })]
  },
})
