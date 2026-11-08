import type { Node as PMNode } from '@tiptap/pm/model'
import type { Transaction } from '@tiptap/pm/state'
import type { RichDocument, RichMarker } from '../../api/richDocument'
import { resolveRichCursor, type RichCursor } from './richEditorPositions'
import { replaceMarkers } from './richMarkerMapping'
import { repairRichPrefixes } from './richPrefixRepair'
import { projectSectionDocument } from './richSectionProjection'

type Scope = string | null
interface Edit { start: number; end: number; nextStart: number; nextEnd: number }
const targets = (document: RichDocument): Map<Scope, string> => new Map<Scope, string>([
  [null, document.content], ...document.sections.map((item): [Scope, string] => [item.key, item.content]),
])
const scopeMatches = (cursor: RichCursor | null, scope: Scope) => cursor !== null && (scope === null
  ? cursor.scope === 'prose' : cursor.scope === 'section' && cursor.sectionKey === scope)
const boundary = (chars: string[], pos: number) => pos >= 0 && pos <= chars.length
  && !(pos > 0 && pos < chars.length && chars[pos - 1] === '\r' && chars[pos] === '\n')
const read = (node: PMNode) => projectSectionDocument(node.toJSON())
function intervals(doc: PMNode): Map<Scope, { from: number; to: number }[]> {
  const result = new Map<Scope, { from: number; to: number }[]>([[null, []]])
  doc.forEach((node, pos) => {
    if (node.type.name === 'authorSection') {
      if (typeof node.attrs.key === 'string') result.set(node.attrs.key, [{ from: pos + 1, to: pos + node.nodeSize - 1 }])
    } else result.get(null)!.push({ from: pos, to: pos + node.nodeSize })
  })
  return result
}
function touches(ranges: { from: number; to: number }[], start: number, end: number): boolean {
  return ranges.some((range) => start === end ? range.from < start && start < range.to
    : range.from < end && start < range.to)
}

/** 只跟踪 PM 事务能证明的同目标替换，不把区块文字当正文或按相同字面猜测。 */
export function mapSectionMarkerTransaction(transaction: Transaction, input: RichMarker[], saved?: RichDocument | null): RichMarker[] | null {
  let markers = input.map((item) => ({ ...item }))
  const originals = new Map(markers.map((item) => [item.key, item]))
  const scopes = new Set<Scope>(markers.filter((item) => item.status === 'active').map((item) => item.section_key))
  let initial: RichDocument
  try { initial = read(transaction.before) } catch { return null }
  if (!scopes.size) return markers
  // 仅在所有步骤都证明保留结构 gap、且分目标逐段修复后文字与区块位置不变时，跳过格式环绕。
  if (transaction.steps.length && transaction.steps.every((item) => {
    const step = item.toJSON() as { stepType?: string; structure?: boolean }
    return step.stepType === 'replaceAround' && step.structure === true
  })) {
    const fixed = repairRichPrefixes(transaction.before, transaction.doc)
    try {
      const result = fixed && read(fixed.doc)
      const identities = (document: RichDocument) => document.sections.map((item) => [item.key, item.anchor, item.order, item.content])
      if (result && result.content === initial.content && JSON.stringify(identities(result)) === JSON.stringify(identities(initial))) return markers
    } catch { return null }
  }
  const initialTexts = new Map<Scope, string[]>()
  const initialTargets = targets(initial)
  for (const scope of scopes) {
    const text = initialTargets.get(scope)
    if (text === undefined) return null
    initialTexts.set(scope, Array.from(text))
  }
  for (let index = 0; index < transaction.steps.length; index++) {
    const before = transaction.docs[index]
    const after = index + 1 < transaction.docs.length ? transaction.docs[index + 1] : transaction.doc
    let oldDoc: RichDocument; let newDoc: RichDocument
    try { oldDoc = read(before); newDoc = read(after) } catch { return null }
    const raw: { a: number; b: number; c: number; d: number }[] = []
    transaction.steps[index].getMap().forEach((a, b, c, d) => raw.push({ a, b, c, d }))
    // 同一 StepMap 的端点与作用域只解码一次，不随区块标记数量乘法扫描全文。
    const resolved = raw.map((range) => ({ ...range, oldStart: resolveRichCursor(before, range.a),
      oldEnd: resolveRichCursor(before, range.b), nextStart: resolveRichCursor(after, range.c), nextEnd: resolveRichCursor(after, range.d) }))
    const oldTargets = targets(oldDoc); const newTargets = targets(newDoc)
    const oldIntervals = intervals(before); const newIntervals = intervals(after)
    const step = transaction.steps[index].toJSON() as {
      stepType?: string; structure?: boolean; from?: number; to?: number; gapFrom?: number; gapTo?: number
    }
    for (const scope of scopes) {
      const oldText = oldTargets.get(scope); const newText = newTargets.get(scope)
      if (oldText === undefined || newText === undefined) return null
      if (oldText === newText && (!raw.length || step.stepType === 'replaceAround' && step.structure === true)) continue
      const edits: Edit[] = []
      let ambiguous = false
      // Enter 先拆段，再以结构化 setNodeMarkup 写入新段 prefix；后者的 StepMap 是节点边界，
      // 但完整保留的 textblock gap 与前后 prefix 可以精确证明插入/删除的换行码点。
      if (step.stepType === 'replaceAround' && step.structure === true && typeof step.from === 'number'
        && step.gapFrom === step.from + 1 && typeof step.to === 'number' && step.gapTo === step.to - 1) {
        const oldNode = before.nodeAt(step.from); const newNode = after.nodeAt(step.from)
        const a = resolveRichCursor(before, step.gapFrom); const b = resolveRichCursor(after, step.gapFrom)
        if (oldNode?.isTextblock && newNode?.isTextblock && oldNode.content.eq(newNode.content)
          && scopeMatches(a, scope) && scopeMatches(b, scope) && typeof oldNode.attrs.prefix === 'string'
          && typeof newNode.attrs.prefix === 'string') {
          const oldSize = Array.from(oldNode.attrs.prefix).length; const newSize = Array.from(newNode.attrs.prefix).length
          edits.push({ start: a!.offset - oldSize, end: a!.offset, nextStart: b!.offset - newSize, nextEnd: b!.offset })
        }
      }
      for (const range of edits.length ? [] : resolved) {
        const { oldStart: a, oldEnd: b, nextStart: c, nextEnd: d } = range
        if (scopeMatches(a, scope) && scopeMatches(b, scope) && scopeMatches(c, scope) && scopeMatches(d, scope)) {
          edits.push({ start: a!.offset, end: b!.offset, nextStart: c!.offset, nextEnd: d!.offset })
        } else if (touches(oldIntervals.get(scope) ?? [], range.a, range.b)
          || touches(newIntervals.get(scope) ?? [], range.c, range.d)) ambiguous = true
      }
      if (ambiguous) return null
      if (!edits.length) { if (oldText !== newText) return null; continue }
      edits.sort((a, b) => a.start - b.start || a.end - b.end)
      const old = Array.from(oldText); const next = Array.from(newText)
      let previous = 0; const rebuilt: string[] = []
      for (const edit of edits) {
        if (edit.start < previous || edit.end < edit.start || edit.nextEnd < edit.nextStart) return null
        rebuilt.push(old.slice(previous, edit.start).join(''), next.slice(edit.nextStart, edit.nextEnd).join(''))
        previous = edit.end
      }
      rebuilt.push(old.slice(previous).join(''))
      if (rebuilt.join('') !== newText) return null
      for (const edit of edits.reverse()) markers = replaceMarkers(markers, old, edit.start, edit.end,
        edit.nextEnd - edit.nextStart, originals, initialTexts.get(scope)!, saved, scope)
      if (markers.some((marker) => marker.status === 'active' && marker.section_key === scope
        && (!boundary(next, marker.start!) || !boundary(next, marker.end!) || marker.start! >= marker.end!))) return null
    }
  }
  return markers
}
