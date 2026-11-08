import type { Node as PMNode } from '@tiptap/pm/model'

/** 正文同一偏移可在多个零宽区块两侧；必须保留身份与边向。 */
export type RichCursor =
  | { scope: 'prose'; offset: number; leftSection: string | null; rightSection: string | null }
  | { scope: 'section'; sectionKey: string; offset: number }
export interface RichCursorCandidate { pm: number; cursor: RichCursor }
interface RawCursor { pm: number; offset: number; sectionKey: string | null }
interface SectionAt { key: string; anchor: number; before: number; after: number }

function scan(doc: PMNode, wanted: (item: RawCursor) => boolean): RichCursorCandidate[] {
  let prose = 0
  const sections: SectionAt[] = []; const found: RawCursor[] = []
  const record = (item: RawCursor) => { if (wanted(item) && !found.some((old) => old.pm === item.pm
    && old.sectionKey === item.sectionKey && old.offset === item.offset)) found.push(item) }
  const scanNode = (node: PMNode, pos: number, sectionKey: string | null, get: () => number, set: (n: number) => void) => {
    if (node.isTextblock) {
      set(get() + Array.from(node.attrs.prefix || '').length)
      record({ pm: pos + 1, sectionKey, offset: get() })
    }
    if (node.isText && node.text !== undefined) {
      let pm = pos
      record({ pm, sectionKey, offset: get() })
      for (const char of node.text) { pm += char.length; set(get() + 1); record({ pm, sectionKey, offset: get() }) }
    }
  }
  doc.descendants((node, pos) => {
    if (node.type.name === 'authorSection') {
      const key: unknown = node.attrs.key
      if (typeof key !== 'string' || !key) return false
      sections.push({ key, anchor: prose, before: pos, after: pos + node.nodeSize })
      let offset = 0
      node.descendants((child, inner) => scanNode(child, pos + 1 + inner, key, () => offset, (n) => { offset = n }))
      return false
    }
    scanNode(node, pos, null, () => prose, (n) => { prose = n })
  })
  return found.map((item) => {
    if (item.sectionKey !== null) return { pm: item.pm, cursor: { scope: 'section', sectionKey: item.sectionKey, offset: item.offset } }
    let left: SectionAt | undefined; let right: SectionAt | undefined
    for (const section of sections) {
      if (section.anchor !== item.offset) continue
      if (section.after <= item.pm && (!left || section.after > left.after)) left = section
      if (section.before >= item.pm && (!right || section.before < right.before)) right = section
    }
    return { pm: item.pm, cursor: { scope: 'prose', offset: item.offset, leftSection: left?.key ?? null, rightSection: right?.key ?? null } }
  })
}

/** 单个 PM 位置如果无法唯一映射到作用域与码点则拒绝。 */
export function resolveRichCursor(doc: PMNode, pm: number): RichCursor | null {
  const entries = scan(doc, (item) => item.pm === pm)
  return entries.length === 1 ? entries[0].cursor : null
}

/** 返回所有可能位置，调用方必须保留区块身份／前后偏向或明确选择。 */
export function richCursorCandidates(doc: PMNode, cursor: Pick<RichCursor, 'scope' | 'offset'> & { sectionKey?: string }): RichCursorCandidate[] {
  return scan(doc, (item) => item.offset === cursor.offset && (cursor.scope === 'prose'
    ? item.sectionKey === null : item.sectionKey === cursor.sectionKey))
}

export function findRichCursor(doc: PMNode, cursor: RichCursor): number | null {
  const candidates = richCursorCandidates(doc, cursor).filter((item) => {
    const found = item.cursor
    if (found.scope === 'section' && cursor.scope === 'section') return found.sectionKey === cursor.sectionKey
    return found.scope === 'prose' && cursor.scope === 'prose'
      && found.leftSection === cursor.leftSection && found.rightSection === cursor.rightSection
  })
  return candidates.length === 1 ? candidates[0].pm : null
}

export function resolveRichSelection(doc: PMNode, from: number, to: number): { start: RichCursor; end: RichCursor } | null {
  const entries = scan(doc, (item) => item.pm === from || item.pm === to)
  const starts = entries.filter((item) => item.pm === from); const ends = entries.filter((item) => item.pm === to)
  if (starts.length !== 1 || ends.length !== 1) return null
  const start = starts[0].cursor; const end = ends[0].cursor
  if (start.scope !== end.scope
    || start.scope === 'section' && end.scope === 'section' && start.sectionKey !== end.sectionKey) return null
  return { start, end }
}
