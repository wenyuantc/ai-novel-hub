import type { Node as PMNode } from '@tiptap/pm/model'
import type { Transaction } from '@tiptap/pm/state'
import { ReplaceAroundStep, ReplaceStep, type Step } from '@tiptap/pm/transform'
import type { ProseEdit } from '../../api/richDocument'
import { codePointLength, codePointToUtf16 } from '../../utils/textOffset'
import { resolveRichCursor, resolveRichSelection } from './richEditorPositions'
import { projectSectionDocument } from './richSectionProjection'

export const MAX_PROSE_EDITS = 256

export function appendProseEdits(before?: ProseEdit[] | null, after?: ProseEdit[] | null): ProseEdit[] | null {
  if (!before || !after || before.length + after.length > MAX_PROSE_EDITS) return null
  const edits = [...before, ...after]
  // 反复粘贴再删除大段文字时，限制可选证据的体积，正文仍可照常保存。
  const limit = 2_000_000
  if (edits.reduce((sum, edit) => sum + edit.insert.length, 0) <= limit) return edits
  return edits.reduce((sum, edit) => sum + codePointLength(edit.insert), 0) <= limit ? edits : null
}

/** 根据 PM 真实范围抽取替换文字，并核实范围以外的内容。未能证明时返回 undefined。 */
function projectedEdit(before: string, after: string, start: number, end: number): ProseEdit | null | undefined {
  const prefix = before.slice(0, codePointToUtf16(before, start))
  const suffix = before.slice(codePointToUtf16(before, end))
  if (after.length < prefix.length + suffix.length || !after.startsWith(prefix) || !after.endsWith(suffix)) return undefined
  const insert = after.slice(prefix.length, after.length - suffix.length)
  return start !== end || insert ? { start, end, insert } : null
}

function proseStep(before: PMNode, after: PMNode, step: Step): ProseEdit | null | undefined {
  if (before.content === after.content) return null // 仅同步标记或退场身份等根属性。
  if (step instanceof ReplaceStep) {
    const range = resolveRichSelection(before, step.from, step.to)
    if (range && before.resolve(step.from).sameParent(before.resolve(step.to)) && step.slice.openStart === 0
      && step.slice.openEnd === 0 && step.slice.content.content.every((node) => node.isText)) {
      if (range.start.scope === 'section') return null
      const insert = step.slice.content.textBetween(0, step.slice.content.size)
      return { start: range.start.offset, end: range.end.offset, insert }
    }
    if (range?.start.scope === 'prose' && range.end.scope === 'prose') {
      return projectedEdit(projectSectionDocument(before.toJSON()).content, projectSectionDocument(after.toJSON()).content,
        range.start.offset, range.end.offset)
    }
  }
  const oldText = projectSectionDocument(before.toJSON()).content
  const newText = projectSectionDocument(after.toJSON()).content
  if (oldText === newText) return null

  // 拆段后 RichEnter 通过 setNodeMarkup 单独写入新段的换行前缀。
  if (step instanceof ReplaceAroundStep) {
    const node = before.nodeAt(step.from)
    if (node?.isTextblock && step.gapFrom === step.from + 1 && step.gapTo === step.to - 1
      && step.to === step.from + node.nodeSize && after.nodeAt(step.from)?.content.eq(node.content)) {
      const cursor = resolveRichCursor(before, step.gapFrom)
      if (cursor?.scope === 'prose' && typeof node.attrs.prefix === 'string') {
        return projectedEdit(oldText, newText, cursor.offset - node.attrs.prefix.length, cursor.offset)
      }
    }
  }
  return undefined
}

export function transactionProseEdits(transactions: Transaction[]): ProseEdit[] | null {
  const edits: ProseEdit[] = []
  try {
    for (const tr of transactions) {
      for (const [index, step] of tr.steps.entries()) {
        const edit = proseStep(tr.docs[index], tr.docs[index + 1] ?? tr.doc, step)
        if (edit === undefined) return null
        if (edit) edits.push(edit)
        if (edits.length > MAX_PROSE_EDITS) return null
      }
    }
  } catch {
    // 某些结构事务含有暂不可投影的中间文档，最终稿仍按原流程保存。
    return null
  }
  return edits
}
