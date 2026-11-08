import { Editor, Extension } from '@tiptap/core'
import { ListItem } from '@tiptap/extension-list'
import { Slice } from '@tiptap/pm/model'
import { Plugin } from '@tiptap/pm/state'
import type { RichDocument } from '../../api/richDocument'
import { editorFromDocument, projectEditorDocument } from './richTextProjection'
import { repairRichPrefixes } from './richPrefixRepair'
import { resolveRichSelection, richCursorCandidates } from './richEditorPositions'

/** 原有分隔符由后一个段落持有；新 Enter 明确写入 LF。 */
export const RichPrefixes = Extension.create({
  name: 'richPrefixes',
  addGlobalAttributes() { return [{ types: ['paragraph', 'heading'], attributes: {
    prefix: { default: '\n', rendered: false, keepOnSplit: false }, blockKey: { default: null, rendered: false, keepOnSplit: false },
    prefixMarks: { default: [], rendered: false, keepOnSplit: false },
    boundaryBefore: { default: false, rendered: false, keepOnSplit: false }, scaffold: { default: false, rendered: false, keepOnSplit: false },
  } }] },
})

/** 禁止未支持的嵌套列表，但让 Tab / Shift+Tab 按浏览器语义离开编辑器。 */
export const FocusableListItem = ListItem.extend({ addKeyboardShortcuts() { return {} } })

/** 标题/列表转换有时重置节点属性，仅在逐段文字完全相同时恢复原分隔符。 */
export const PreserveRichPrefixes = Extension.create({
  name: 'preserveRichPrefixes',
  addProseMirrorPlugins() { return [new Plugin({
    appendTransaction(transactions, oldState, newState) {
      if (transactions.some((tr) => tr.getMeta('rich-load') || tr.getMeta('rich-line-change') || tr.getMeta('rich-prefix-fix'))) return null
      const transform = repairRichPrefixes(oldState.doc, newState.doc)
      if (!transform || !transform.steps.length) return null
      let transaction = newState.tr
      for (const step of transform.steps) transaction = transaction.step(step)
      return transaction.setMeta('rich-prefix-fix', true)
    },
  })] },
})

/** 任何不受支持节点/结构都拒绝写入，格式转换只允许经上述可靠修复。 */
export const RichStructureGuard = Extension.create({
  name: 'richStructureGuard', priority: 2500,
  addProseMirrorPlugins() {
    const editor = this.editor
    const rejected = () => queueMicrotask(() => { if (!editor.isDestroyed) editor.view.dom.dispatchEvent(new CustomEvent('rich-mapping-rejected')) })
    return [new Plugin({
    filterTransaction(tr, state) {
      if (!tr.docChanged) return true
      try { projectEditorDocument(tr.doc.toJSON()); return true } catch { /* 仅尝试格式转换的分隔符修复 */ }
      const fixed = repairRichPrefixes(state.doc, tr.doc)
      if (!fixed || !fixed.steps.length) { rejected(); return false }
      try { projectEditorDocument(fixed.doc.toJSON()); return true } catch { rejected(); return false }
    },
  })] },
})

function plainDocument(content: string): RichDocument {
  return { content, formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [] }
}

export const RichPlainPaste = Extension.create({
  name: 'richPlainPaste', priority: 2100,
  addProseMirrorPlugins() { return [new Plugin({ props: {
    handlePaste(view, event) {
      event.preventDefault()
      const text = event.clipboardData?.getData('text/plain') ?? ''
      if (!text) {
        queueMicrotask(() => { if (!view.isDestroyed) view.dom.dispatchEvent(new CustomEvent('rich-paste-rejected')) })
        return true // 不让空文本或纯 HTML/图片悄悄替换当前选区。
      }
      try {
        const node = view.state.schema.nodeFromJSON(editorFromDocument(plainDocument(text)))
        const slice = new Slice(node.content, 1, 1)
        const { from, to } = view.state.selection
        // replaceSelection 使用 replaceRange 可能吞掉左段并错误继承右段的 CRLF。
        const tr = from === to ? view.state.tr.replaceSelection(slice) : view.state.tr.replace(from, to, slice)
        view.dispatch(tr.setMeta('rich-line-change', true).scrollIntoView())
      } catch {
        queueMicrotask(() => { if (!view.isDestroyed) view.dom.dispatchEvent(new CustomEvent('rich-paste-rejected')) })
      }
      return true
    },
  } })] },
})

/** 明确选区的删除/输入只替换精确范围，不能让 replaceRange 扩展并偷换段落前缀。 */
export const RichExactSelection = Extension.create({
  name: 'richExactSelection', priority: 2200,
  addKeyboardShortcuts() {
    const remove = () => {
      const { from, to, empty } = this.editor.state.selection
      if (empty) return false
      this.editor.view.dispatch(this.editor.state.tr.delete(from, to).scrollIntoView())
      return true
    }
    return { Backspace: remove, Delete: remove }
  },
  addProseMirrorPlugins() { return [new Plugin({ props: {
    handleTextInput(view, from, to, text) {
      if (from === to) return false
      view.dispatch(view.state.tr.insertText(text, from, to).scrollIntoView())
      return true
    },
  } })] },
})

export const RichEnter = Extension.create({
  name: 'richEnter', priority: 2000,
  addKeyboardShortcuts() { return {
    Enter: () => {
      let used = ''
      return this.editor.chain().command(({ tr }) => {
        if (!tr.selection.empty) tr.delete(tr.selection.from, tr.selection.to)
        return true
      }).first(({ commands }) => [
        () => { const ok = commands.splitListItem('listItem'); if (ok) used = 'split'; return ok },
        () => { const ok = commands.createParagraphNear(); if (ok) used = 'other'; return ok },
        () => { const ok = commands.liftEmptyBlock(); if (ok) used = 'other'; return ok },
        () => { const ok = commands.splitBlock(); if (ok) used = 'split'; return ok },
      ]).command(({ tr }) => {
        const parent = tr.selection.$from.parent
        if (used === 'split') tr.setMeta('rich-line-change', true)
        if (used === 'split' && ['paragraph', 'heading'].includes(parent.type.name)) {
          tr.setNodeMarkup(tr.selection.$from.before(), undefined, { ...parent.attrs, prefix: '\n' })
        }
        return true
      }).run()
    },
    'Shift-Enter': () => true, // 不把 hardBreak 隐式转成下一次读取无法还原的节点。
  } },
})

/** 正文旧调用只接受明确的正文选区，不能跨过区块或把区块文字当正文。 */
export function editorCodePointSelection(editor: Editor): { start: number; end: number } | null {
  const { from, to } = editor.state.selection
  const selected = resolveRichSelection(editor.state.doc, from, to)
  if (!selected || selected.start.scope !== 'prose' || selected.end.scope !== 'prose') return null
  let crossesSection = false
  if (from !== to) editor.state.doc.nodesBetween(from, to, (node) => {
    if (node.type.name === 'authorSection') { crossesSection = true; return false }
  })
  return crossesSection ? null : { start: selected.start.offset, end: selected.end.offset }
}

/** 整数反向有多个落点时拒绝；调用方须另携区块身份及边向。 */
export function selectEditorCodePoints(editor: Editor, start: number, end: number): boolean {
  const from = richCursorCandidates(editor.state.doc, { scope: 'prose', offset: start })
  const to = richCursorCandidates(editor.state.doc, { scope: 'prose', offset: end })
  if (from.length !== 1 || to.length !== 1 || from[0].pm > to[0].pm) return false
  let crossesSection = false
  if (from[0].pm !== to[0].pm) editor.state.doc.nodesBetween(from[0].pm, to[0].pm, (node) => {
    if (node.type.name === 'authorSection') { crossesSection = true; return false }
  })
  if (crossesSection) return false
  editor.chain().focus().setTextSelection({ from: from[0].pm, to: to[0].pm }).run()
  return true
}

/** 区块定位必须携带身份；折叠内容先展开，含糊坐标不猜测。 */
export function selectSectionCodePoints(editor: Editor, key: string, start: number, end: number): boolean {
  const from = richCursorCandidates(editor.state.doc, { scope: 'section', sectionKey: key, offset: start })
  const to = richCursorCandidates(editor.state.doc, { scope: 'section', sectionKey: key, offset: end })
  if (from.length !== 1 || to.length !== 1 || from[0].pm > to[0].pm) return false
  let position = -1
  editor.state.doc.forEach((node, pos) => { if (node.type.name === 'authorSection' && node.attrs.key === key) position = pos })
  if (position < 0) return false
  editor.view.dom.dispatchEvent(new CustomEvent('rich-section-reveal', { detail: { key } }))
  const node = editor.view.nodeDOM(position)
  const content = node instanceof HTMLElement ? node.querySelector<HTMLElement>('.rich-author-section__content') : null
  if (!content || content.hidden || content.closest('[hidden]')) return false
  editor.view.focus()
  return editor.commands.setTextSelection({ from: from[0].pm, to: to[0].pm })
}
