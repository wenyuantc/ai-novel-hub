import type { Node as PMNode } from '@tiptap/pm/model'
import type { Transaction } from '@tiptap/pm/state'
import type { RichDocument, RichMarker } from '../../api/richDocument'
import { projectEditorDocument } from './richTextProjection'
import { repairRichPrefixes } from './richPrefixRepair'

/** 将少量 PM 坐标翻回码点；不得用词面搜索或猜测 CRLF 中点。 */
function offsets(doc: PMNode, positions: number[]): Map<number, number> | null {
  const wanted = new Set(positions); const found = new Map<number, number>(); let ambiguous = false; let offset = 0
  const record = (pm: number) => {
    if (!wanted.has(pm)) return
    if (found.has(pm) && found.get(pm) !== offset) ambiguous = true
    else found.set(pm, offset)
  }
  doc.descendants((node, pos) => {
    if (node.isTextblock) { offset += Array.from(node.attrs.prefix || '').length; record(pos + 1) }
    if (node.isText && node.text !== undefined) {
      let pm = pos; record(pm)
      for (const char of node.text) { offset++; pm += char.length; record(pm) }
    }
  })
  return !ambiguous && found.size === wanted.size ? found : null
}

function boundary(chars: string[], pos: number): boolean {
  return pos >= 0 && pos <= chars.length && !(pos > 0 && pos < chars.length && chars[pos - 1] === '\r' && chars[pos] === '\n')
}

/** 旧活动范围经服务端校验；未保存草稿的片段单独保留，不能冒充服务端事实。 */
export function snapshotInvalidatedMarker(marker: RichMarker, chars: string[], saved?: RichDocument | null): RichMarker {
  const excerpt = (item: RichMarker, source: string[]) => {
    const start = item.start!; const end = item.end!
    let limit = Math.min(end, start + 5000)
    const truncated = limit < end
    if (truncated && source[limit - 1] === '\r' && source[limit] === '\n') limit--
    return { text: source.slice(start, limit).join(''), truncated, start }
  }
  const draft = excerpt(marker, chars)
  const old = saved?.markers.find((item) => item.key === marker.key && item.section_key === marker.section_key && item.status === 'active')
  const savedText = marker.section_key === null ? saved?.content : saved?.sections.find((item) => item.key === marker.section_key)?.content
  const original = old && savedText !== undefined ? excerpt(old, Array.from(savedText)) : draft
  const differs = !!old && (draft.text !== original.text || draft.truncated !== original.truncated || draft.start !== original.start)
  return { ...marker, status: 'stale', start: null, end: null, last_start: original.start,
    excerpt: original.text, excerpt_truncated: original.truncated,
    draft_excerpt: differs ? draft.text : null, draft_excerpt_truncated: differs && draft.truncated,
    draft_last_start: differs ? draft.start : null }
}

export function replaceMarkers(
  markers: RichMarker[], old: string[], start: number, end: number, size: number,
  originals: Map<string, RichMarker>, originalText: string[], saved?: RichDocument | null, scope: string | null = null,
): RichMarker[] {
  const delta = size - (end - start)
  return markers.map((marker) => {
    if (marker.status !== 'active' || marker.section_key !== scope) return marker
    const left = marker.start!; const right = marker.end!
    if (right <= start) return marker
    if (left >= end) return { ...marker, start: left + delta, end: right + delta }
    if (start === end || left < start && right > end) return { ...marker, end: right + delta }
    if (left < start) return { ...marker, end: start }
    if (right > end) return { ...marker, start: start + size, end: right + delta }
    // 一个 StepMap 可能有多个删除区间，且同一事务可包含多个步骤。
    // 失效时取事务开始的完整范围，而不是已被前一删除缩短的范围。
    const original = originals.get(marker.key)
    return original && original.status === 'active'
      ? snapshotInvalidatedMarker(original, originalText, saved) : snapshotInvalidatedMarker(marker, old, saved)
  })
}

interface Edit { oldStart: number; oldEnd: number; newStart: number; newEnd: number }

/**
 * 仅依赖事务的实际 StepMap；一个端点不可见、变化不能完整复原或元数据触及 CRLF 接缝时返回 null。
 */
export function mapMarkerTransaction(transaction: Transaction, input: RichMarker[], saved?: RichDocument | null): RichMarker[] | null {
  let markers = input.map((item) => ({ ...item }))
  const originals = new Map(markers.map((item) => [item.key, item]))
  let originalText: string[]
  try { originalText = Array.from(projectEditorDocument(transaction.before.toJSON()).content) } catch { return null }
  // 纯结构环绕保持文本 gap；列表转换可能临时重置前缀，校验可恢复后无需移动码点。
  if (transaction.steps.length && transaction.steps.every((item) => {
    const step = item.toJSON() as { stepType?: string; structure?: boolean }
    return step.stepType === 'replaceAround' && step.structure === true
  })) {
    const fixed = repairRichPrefixes(transaction.before, transaction.doc)
    try {
      if (fixed && projectEditorDocument(transaction.before.toJSON()).content === projectEditorDocument(fixed.doc.toJSON()).content) return markers
    } catch { return null }
  }
  for (let index = 0; index < transaction.steps.length; index++) {
    const before = transaction.docs[index]
    const after = index + 1 < transaction.docs.length ? transaction.docs[index + 1] : transaction.doc
    let beforeText: string; let afterText: string
    try {
      beforeText = projectEditorDocument(before.toJSON()).content
      afterText = projectEditorDocument(after.toJSON()).content
    } catch { return null }
    const raw: { a: number; b: number; c: number; d: number }[] = []
    transaction.steps[index].getMap().forEach((a, b, c, d) => raw.push({ a, b, c, d }))
    const step = transaction.steps[index].toJSON() as { stepType?: string; structure?: boolean }
    // 纯标记步骤无位置变化；structure-only 的 ReplaceAround 保留整个原文 gap。
    // 同字替换即使结果完全相同也是删除，不能按相同词面假装旧标记仍在。
    if (beforeText === afterText && (!raw.length || step.stepType === 'replaceAround' && step.structure === true)) continue
    const oldOffsets = offsets(before, raw.flatMap((item) => [item.a, item.b]))
    const newOffsets = offsets(after, raw.flatMap((item) => [item.c, item.d]))
    if (!raw.length || !oldOffsets || !newOffsets) return null
    const edits: Edit[] = raw.map(({ a, b, c, d }) => ({
      oldStart: oldOffsets.get(a)!, oldEnd: oldOffsets.get(b)!, newStart: newOffsets.get(c)!, newEnd: newOffsets.get(d)!,
    })).sort((a, b) => a.oldStart - b.oldStart || a.oldEnd - b.oldEnd)
    const old = Array.from(beforeText); const next = Array.from(afterText)
    let previous = 0; const rebuilt: string[] = []
    for (const edit of edits) {
      if (edit.oldStart < previous || edit.oldEnd < edit.oldStart || edit.newEnd < edit.newStart) return null
      rebuilt.push(old.slice(previous, edit.oldStart).join(''), next.slice(edit.newStart, edit.newEnd).join(''))
      previous = edit.oldEnd
    }
    rebuilt.push(old.slice(previous).join(''))
    if (rebuilt.join('') !== afterText) return null
    for (const edit of edits.reverse()) markers = replaceMarkers(markers, old, edit.oldStart, edit.oldEnd, edit.newEnd - edit.newStart, originals, originalText, saved)
    if (markers.some((marker) => marker.status === 'active' && marker.section_key === null
      && (!boundary(next, marker.start!) || !boundary(next, marker.end!) || marker.start! >= marker.end!))) return null
  }
  return markers
}
