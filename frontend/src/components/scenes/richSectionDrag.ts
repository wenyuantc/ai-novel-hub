import { Extension } from '@tiptap/core'
import { NodeSelection, Plugin, Selection } from '@tiptap/pm/state'
import type { RichDocument } from '../../api/richDocument'
import { findRichCursor, resolveRichCursor, richCursorCandidates } from './richEditorPositions'
import { projectSectionDocument } from './richSectionProjection'
import { createSectionPlanTransaction } from './richSectionTransaction'

/** 拖动只提交经验证的同景区块移动计划，不让默认 drop 删除/复制身份。 */
export const RichSectionDrag = Extension.create<{ getSavedDocument: () => RichDocument | null }>({
  name: 'richSectionDrag', priority: 2200,
  addOptions() { return { getSavedDocument: () => null } },
  addProseMirrorPlugins() {
    const editor = this.editor; const { getSavedDocument } = this.options
    const reject = () => queueMicrotask(() => { if (!editor.isDestroyed) editor.view.dom.dispatchEvent(new CustomEvent('rich-mapping-rejected')) })
    return [new Plugin({ props: { handleDrop(view, event, slice, moved) {
      const selection = view.state.selection
      let containsSection = false
      slice.content.forEach((node) => { if (node.type.name === 'authorSection') containsSection = true })
      if (!(selection instanceof NodeSelection) || selection.node.type.name !== 'authorSection') {
        if (containsSection) { reject(); return true }
        return false
      }
      const sectionKey: unknown = selection.node.attrs.key
      if (!moved || typeof sectionKey !== 'string' || slice.content.childCount !== 1
        || slice.content.firstChild?.type.name !== 'authorSection' || slice.content.firstChild.attrs.key !== sectionKey
        || !view.editable || view.composing) { reject(); return true }
      const point = view.posAtCoords({ left: event.clientX, top: event.clientY })
      if (!point) { reject(); return true }
      const nearest = Selection.near(view.state.doc.resolve(point.pos)).from
      let cursor = resolveRichCursor(view.state.doc, nearest)
      if (cursor?.scope === 'section') {
        const targetKey = cursor.sectionKey
        const part = projectSectionDocument(view.state.doc.toJSON()).sections.find((item) => item.key === targetKey)
        let position = -1
        view.state.doc.forEach((node, pos) => { if (node.type.name === 'authorSection' && node.attrs.key === targetKey) position = pos })
        const dom = position < 0 ? null : view.nodeDOM(position)
        const box = dom instanceof HTMLElement ? dom.getBoundingClientRect() : null
        if (!part || !box) { reject(); return true }
        const above = event.clientY < box.top + box.height / 2
        const matches = richCursorCandidates(view.state.doc, { scope: 'prose', offset: part.anchor })
          .filter((item) => item.cursor.scope === 'prose' && (above
            ? item.cursor.rightSection === targetKey : item.cursor.leftSection === targetKey)
            && findRichCursor(view.state.doc, item.cursor) !== null)
        cursor = matches.length === 1 ? matches[0].cursor : null
      }
      if (!cursor || cursor.scope !== 'prose' || findRichCursor(view.state.doc, cursor) === null) { reject(); return true }
      const saved = getSavedDocument()
      if (!saved) { reject(); return true }
      try {
        view.dispatch(createSectionPlanTransaction(view.state, { action: 'move', key: sectionKey, cursor }, saved))
      } catch { reject() }
      return true
    } } })]
  },
})
