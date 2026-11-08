import type { Node as PMNode } from '@tiptap/pm/model'
import type { RichMarker } from '../../api/richDocument'

/** 段落分隔符不属于 text node；找出无法由行内装饰呈现的有效标记。 */
export function markersWithoutTextNodes(doc: PMNode): RichMarker[] {
  const markers = doc.attrs.markers as RichMarker[]
  if (!markers.length) return []
  const spans = new Map<string | null, { start: number; end: number }[]>([[null, []]])
  const offsets = new Map<string | null, number>([[null, 0]])
  const visit = (node: PMNode, scope: string | null) => {
    let offset = offsets.get(scope) ?? 0
    if (node.isTextblock) offset += Array.from(node.attrs.prefix || '').length
    if (node.isText && node.text !== undefined) {
      const end = offset + Array.from(node.text).length
      spans.get(scope)!.push({ start: offset, end }); offset = end
    }
    offsets.set(scope, offset)
  }
  doc.descendants((node) => {
    if (node.type.name === 'authorSection') {
      const key: unknown = node.attrs.key
      if (typeof key === 'string' && !spans.has(key)) {
        spans.set(key, []); offsets.set(key, 0)
        node.descendants((child) => { visit(child, key) })
      }
      return false
    }
    visit(node, null)
  })
  return markers.filter((marker) => {
    if (marker.status !== 'active') return false
    const target = spans.get(marker.section_key)
    if (!target) return false
    const start = marker.start!; const end = marker.end!
    let low = 0; let high = target.length
    while (low < high) {
      const middle = (low + high) >>> 1
      if (target[middle].end <= start) low = middle + 1
      else high = middle
    }
    return low === target.length || target[low].start >= end
  })
}
