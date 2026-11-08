import type { RichDocument, RichFormatting } from '../../api/richDocument'

/** 与服务端相同的码点/CRLF 坐标边界，不把 UTF-16 码元误当位置。 */
const legal = (chars: string[], pos: number) => Number.isSafeInteger(pos) && pos >= 0 && pos <= chars.length
  && !(pos > 0 && pos < chars.length && chars[pos - 1] === '\r' && chars[pos] === '\n')

function validateFormatting(text: string, formatting: RichFormatting): boolean {
  const chars = Array.from(text); const boundaries = formatting.boundaries
  if (chars.some((char) => char.length === 1 && char.charCodeAt(0) >= 0xd800 && char.charCodeAt(0) <= 0xdfff)) return false
  if (formatting.version !== 1 || boundaries.some((point, index) => !legal(chars, point) || index > 0 && boundaries[index - 1] >= point)) return false
  const hasBoundary = new Set(boundaries)
  const inlineByKind = new Map<string, { start: number; end: number }[]>()
  for (const item of formatting.inline) {
    if (!['bold', 'italic'].includes(item.kind) || !legal(chars, item.start) || !legal(chars, item.end) || item.start >= item.end) return false
    const values = inlineByKind.get(item.kind) ?? []; values.push(item); inlineByKind.set(item.kind, values)
  }
  for (const values of inlineByKind.values()) {
    values.sort((a, b) => a.start - b.start || a.end - b.end)
    if (values.some((item, index) => index > 0 && values[index - 1].end > item.start)) return false
  }
  const blocks = [...formatting.blocks].sort((a, b) => a.start - b.start || a.end - b.end)
  for (let index = 0; index < blocks.length; index++) {
    const item = blocks[index]; const previous = blocks[index - 1]
    if (!['heading', 'bullet_list', 'ordered_list', 'blockquote'].includes(item.kind)
      || !legal(chars, item.start) || !legal(chars, item.end) || item.start > item.end
      || previous && (previous.end > item.start || previous.start === previous.end && previous.end === item.start && item.start === item.end)) return false
    if (item.kind === 'heading' ? !Number.isInteger(item.level) || item.level! < 1 || item.level! > 6 : item.level !== null) return false
    if (item.start !== 0 && !hasBoundary.has(item.start) && !['\r', '\n'].includes(chars[item.start - 1])) return false
    if (item.end !== chars.length && !hasBoundary.has(item.end) && !['\r', '\n'].includes(chars[item.end])) return false
    let low = 0; let high = boundaries.length
    while (low < high) { const middle = (low + high) >>> 1; if (boundaries[middle] <= item.start) low = middle + 1; else high = middle }
    if (item.kind === 'heading' && (chars.slice(item.start, item.end).some((char) => char === '\r' || char === '\n')
      || low < boundaries.length && boundaries[low] < item.end)) return false
  }
  return true
}

/** 在发送前拒绝当前编辑树无法持久化的锚点或范围；不自行修正作者资料。 */
export function validSectionDocument(document: RichDocument, checkMarkers = true): boolean {
  if (!validateFormatting(document.content, document.formatting)) return false
  const chars = Array.from(document.content); const sections = new Map<string, typeof document.sections[number]>()
  const positions = new Set<string>(); const retired = new Set(document.retired_section_keys)
  if (retired.size !== document.retired_section_keys.length) return false
  for (const section of document.sections) {
    const position = `${section.anchor}:${section.order}`
    if (!section.key || sections.has(section.key) || retired.has(section.key) || positions.has(position)
      || !legal(chars, section.anchor) || !Number.isSafeInteger(section.order) || section.order < 0
      || !validateFormatting(section.content, section.formatting)) return false
    positions.add(position); sections.set(section.key, section)
  }
  if (!checkMarkers) return true
  const markers = new Set<string>()
  const targetChars = new Map<string | null, string[]>([[null, chars]])
  for (const section of sections.values()) targetChars.set(section.key, Array.from(section.content))
  for (const marker of document.markers) {
    if (!marker.key || markers.has(marker.key) || !['yellow', 'green', 'blue', 'purple', 'red'].includes(marker.color)) return false
    markers.add(marker.key)
    const target = targetChars.get(marker.section_key)
    if (marker.status === 'active') {
      if (target === undefined || marker.start === null || marker.end === null
        || !legal(target, marker.start) || !legal(target, marker.end) || marker.start >= marker.end) return false
    } else if (marker.status !== 'stale' || marker.start !== null || marker.end !== null || marker.last_start === null
      || marker.section_key !== null && !sections.has(marker.section_key) && !retired.has(marker.section_key)) return false
  }
  return true
}
