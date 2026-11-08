import type { JSONContent } from '@tiptap/core'
import type { RichBlockStyle, RichDocument, RichFormatting, RichInlineStyle, RichSection } from '../../api/richDocument'
import { UnsupportedRichDocument } from './richTextProjection'

type Kind = 'paragraph' | RichBlockStyle['kind']
type InlineKind = RichInlineStyle['kind']
interface Line { prefix: string; start: number; end: number }

function lines(chars: string[]): Line[] {
  const result: Line[] = []
  let start = 0; let prefix = ''; let pos = 0
  while (pos < chars.length) {
    if (chars[pos] !== '\r' && chars[pos] !== '\n') { pos++; continue }
    result.push({ prefix, start, end: pos })
    prefix = chars[pos] === '\r' && chars[pos + 1] === '\n' ? '\r\n' : chars[pos]
    pos += Array.from(prefix).length; start = pos
  }
  result.push({ prefix, start, end: pos })
  return result
}

/** 返回首个大于目标的下标；所有位置均为 Unicode 码点。 */
function upperBound(values: readonly number[], target: number): number {
  let low = 0; let high = values.length
  while (low < high) { const middle = (low + high) >>> 1; if (values[middle] <= target) low = middle + 1; else high = middle }
  return low
}

function orderedInline(items: RichInlineStyle[]): RichInlineStyle[] {
  const result: RichInlineStyle[] = []
  for (const item of [...items].sort((a, b) => a.kind.localeCompare(b.kind) || a.start - b.start || a.end - b.end)) {
    const last = result.at(-1)
    if (last && last.kind === item.kind && last.end === item.start) last.end = item.end
    else result.push({ ...item })
  }
  return result
}

function orderedBlocks(items: RichBlockStyle[]): RichBlockStyle[] {
  return [...items].sort((a, b) => a.start - b.start || a.end - b.end || a.kind.localeCompare(b.kind)
    || (a.level ?? 0) - (b.level ?? 0))
}

/** 比较各目标文本、格式范围及有序身份；同样式的相邻独立块不能合并。 */
export function sameSectionDocument(a: RichDocument, b: RichDocument): boolean {
  const formatting = (value: RichFormatting) => ({ version: value.version, boundaries: value.boundaries,
    inline: orderedInline(value.inline), blocks: orderedBlocks(value.blocks) })
  const section = (item: RichSection) => ({ key: item.key, anchor: item.anchor, order: item.order, title: item.title,
    content: item.content, formatting: formatting(item.formatting), include_in_ai: item.include_in_ai })
  const order = (x: RichSection, y: RichSection) => x.anchor - y.anchor || x.order - y.order || x.key.localeCompare(y.key)
  return a.content === b.content && JSON.stringify(formatting(a.formatting)) === JSON.stringify(formatting(b.formatting))
    && JSON.stringify([...a.sections].sort(order).map(section)) === JSON.stringify([...b.sections].sort(order).map(section))
    && JSON.stringify([...a.retired_section_keys].sort()) === JSON.stringify([...b.retired_section_keys].sort())
    && JSON.stringify([...a.markers].sort((x, y) => x.key.localeCompare(y.key)))
      === JSON.stringify([...b.markers].sort((x, y) => x.key.localeCompare(y.key)))
}

function makeTextBlocks(text: string, formatting: RichFormatting, sections: RichSection[]): JSONContent[] {
  const chars = Array.from(text); const result: JSONContent[] = []
  const grouped = new Map<number, RichSection[]>()
  for (const section of sections) {
    const items = grouped.get(section.anchor) ?? []; items.push(section); grouped.set(section.anchor, items)
  }
  for (const items of grouped.values()) items.sort((a, b) => a.order - b.order || a.key.localeCompare(b.key))
  const anchorPoints = [...grouped.keys()].sort((a, b) => a - b)
  const boundaryPoints = [...formatting.boundaries].sort((a, b) => a - b)
  const inlineByKind = new Map<InlineKind, RichInlineStyle[]>()
  const inlineStarts = new Map<InlineKind, number[]>()
  for (const kind of ['bold', 'italic'] as const) {
    const items = formatting.inline.filter((item) => item.kind === kind).sort((a, b) => a.start - b.start || a.end - b.end)
    inlineByKind.set(kind, items); inlineStarts.set(kind, items.map((item) => item.start))
  }
  const inlineCuts = [...new Set(formatting.inline.flatMap((item) => [item.start, item.end]))].sort((a, b) => a - b)
  const blocks = formatting.blocks.map((style, index) => ({ style, index })).sort((a, b) => a.style.start - b.style.start
    || a.style.end - b.style.end || a.index - b.index)
  const blockStarts = blocks.map((item) => item.style.start)
  const emitted = new Set<number>()
  const emitSections = (at: number) => {
    if (emitted.has(at)) return
    const items = grouped.get(at)
    if (!items) return
    items.forEach((section, index) => {
      // 同锚区块之间保留独立可见光标；它不增加正式正文或真实段落边界。
      if (index) result.push({ type: 'paragraph', attrs: { prefix: '', prefixMarks: [], blockKey: null,
        boundaryBefore: false, scaffold: true } })
      result.push({ type: 'authorSection', attrs: { key: section.key, title: section.title, include_in_ai: section.include_in_ai },
        content: makeTextBlocks(section.content, section.formatting, []) })
    })
    emitted.add(at)
  }
  const boundaries = new Set(formatting.boundaries)
  const markKinds = (start: number, end: number): InlineKind[] => (['bold', 'italic'] as const).filter((kind) => {
    const index = upperBound(inlineStarts.get(kind)!, start) - 1
    const item = inlineByKind.get(kind)![index]
    return !!item && item.start <= start && end <= item.end
  })
  const emitLine = (prefix: string, start: number, end: number, boundary: boolean, scaffold: boolean) => {
    let styleIndex = -1
    if (!scaffold) {
      for (let index = upperBound(blockStarts, start) - 1; index >= 0 && blocks[index].style.end >= start; index--) {
        const item = blocks[index]
        if (end <= item.style.end && (styleIndex < 0 || item.index < styleIndex)) styleIndex = item.index
      }
    }
    const style = formatting.blocks[styleIndex]
    const cuts = new Set([start, end])
    for (let index = upperBound(inlineCuts, start); index < inlineCuts.length && inlineCuts[index] < end; index++) cuts.add(inlineCuts[index])
    const positions = [...cuts].sort((a, b) => a - b); const content: JSONContent[] = []
    for (let i = 1; i < positions.length; i++) {
      const a = positions[i - 1]; const b = positions[i]
      const marks = markKinds(a, b).map((type) => ({ type }))
      content.push({ type: 'text', text: chars.slice(a, b).join(''), ...(marks.length ? { marks } : {}) })
    }
    const prefixLength = Array.from(prefix).length
    const node: JSONContent = { type: style?.kind === 'heading' ? 'heading' : 'paragraph', attrs: {
      prefix, prefixMarks: prefixLength ? markKinds(start - prefixLength, start) : [],
      blockKey: style ? `saved:${styleIndex}` : null, boundaryBefore: boundary, scaffold,
      ...(style?.kind === 'heading' ? { level: style.level } : {}),
    }, ...(content.length ? { content } : {}) }
    if (style?.kind === 'bullet_list' || style?.kind === 'ordered_list') {
      result.push({ type: style.kind === 'bullet_list' ? 'bulletList' : 'orderedList', content: [{ type: 'listItem', content: [node] }] })
    } else if (style?.kind === 'blockquote') result.push({ type: 'blockquote', content: [node] })
    else result.push(node)
  }
  let anchorIndex = 0; let boundaryIndex = 0
  for (const line of lines(chars)) {
    let prefix = line.prefix
    if (grouped.has(line.start)) {
      emitLine(prefix, line.start, line.start, false, true); prefix = ''
      emitSections(line.start)
    }
    const points = new Set([line.start, line.end])
    while (anchorIndex < anchorPoints.length && anchorPoints[anchorIndex] <= line.start) anchorIndex++
    while (anchorIndex < anchorPoints.length && anchorPoints[anchorIndex] < line.end) points.add(anchorPoints[anchorIndex++])
    while (boundaryIndex < boundaryPoints.length && boundaryPoints[boundaryIndex] <= line.start) boundaryIndex++
    while (boundaryIndex < boundaryPoints.length && boundaryPoints[boundaryIndex] < line.end) points.add(boundaryPoints[boundaryIndex++])
    const positions = [...points].sort((a, b) => a - b)
    if (line.start === line.end) {
      emitLine(prefix, line.start, line.end, boundaries.has(line.start), false)
    } else {
      for (let i = 1; i < positions.length; i++) {
        const start = positions[i - 1]; const end = positions[i]
        emitLine(prefix, start, end, boundaries.has(start), false); prefix = ''
        emitSections(end)
      }
      if (boundaries.has(line.end) || grouped.has(line.end)) emitLine('', line.end, line.end, boundaries.has(line.end), true)
    }
  }
  if (emitted.size !== grouped.size) throw new UnsupportedRichDocument('区块锚点无法映射到可见正文。')
  return result
}

/** 完整富文档与受限节点树之间的无损投影。 */
export function editorFromSectionDocument(document: RichDocument): JSONContent {
  const result: JSONContent = { type: 'doc', attrs: { markers: document.markers, retiredSectionKeys: document.retired_section_keys,
      sectionOrders: Object.fromEntries(document.sections.map((item) => [item.key, item.order])) },
    content: makeTextBlocks(document.content, document.formatting, document.sections) }
  const projected = projectSectionDocument(result)
  if (!sameSectionDocument(projected, document)) throw new UnsupportedRichDocument('富文档不能完整往返，原稿保持只读。')
  return result
}

interface Built { content: string; formatting: RichFormatting; sections: RichSection[] }
function build(nodes: JSONContent[], allowSections: boolean, oldOrders: Record<string, number> = {}): Built {
  let content = ''; let position = 0; let count = 0; let previousKey: string | null = null
  const boundaries: number[] = []; const inline: RichInlineStyle[] = []; const blocks: RichBlockStyle[] = []; const sections: RichSection[] = []
  const addMarks = (start: number, end: number, marks: unknown[]) => {
    if (end <= start) return
    for (const kind of marks) {
      if (kind !== 'bold' && kind !== 'italic') throw new UnsupportedRichDocument('文字样式无效。')
      let previous: RichInlineStyle | undefined
      for (let index = inline.length - 1; index >= 0; index--) {
        if (inline[index].kind === kind) { previous = inline[index]; break }
      }
      if (previous?.end === start) previous.end = end; else inline.push({ kind, start, end })
    }
  }
  const line = (node: JSONContent, kind: Kind, level: number | null = null) => {
    if (!['paragraph', 'heading'].includes(node.type ?? '')) throw new UnsupportedRichDocument('段落类型无效。')
    const prefix: unknown = node.attrs?.prefix
    if (typeof prefix !== 'string' || !['', '\n', '\r', '\r\n'].includes(prefix) || count === 0 && prefix !== '') throw new UnsupportedRichDocument('换行无法映射。')
    const prefixMarks: unknown = node.attrs?.prefixMarks ?? []
    if (!Array.isArray(prefixMarks)) throw new UnsupportedRichDocument('换行样式无效。')
    const prefixStart = position; content += prefix; position += Array.from(prefix).length; count++
    addMarks(prefixStart, position, prefixMarks)
    if (node.attrs?.boundaryBefore) boundaries.push(position)
    const start = position
    for (const child of node.content ?? []) {
      if (child.type !== 'text' || typeof child.text !== 'string' || /[\r\n]/.test(child.text)) throw new UnsupportedRichDocument('正文节点无效。')
      const end = position + Array.from(child.text).length
      addMarks(position, end, (child.marks ?? []).map((item) => item.type))
      content += child.text; position = end
    }
    if (node.attrs?.scaffold && start === position && kind === 'paragraph') return
    const key: unknown = node.attrs?.blockKey ?? null
    if (key !== null && typeof key !== 'string') throw new UnsupportedRichDocument('块范围身份无效。')
    if (kind !== 'paragraph') {
      const last = blocks.at(-1)
      if (key && key === previousKey && last?.kind === kind && last.level === level) last.end = position
      else blocks.push({ kind, start, end: position, level })
      previousKey = key
    } else previousKey = null
  }
  for (const node of nodes) {
    if (node.type === 'authorSection') {
      if (!allowSections || typeof node.attrs?.key !== 'string' || typeof node.attrs?.title !== 'string'
        || typeof node.attrs?.include_in_ai !== 'boolean' || !Array.isArray(node.content)) throw new UnsupportedRichDocument('区块结构无效。')
      const inner = build(node.content, false)
      const order = oldOrders[node.attrs.key] ?? -1
      sections.push({ key: node.attrs.key, anchor: position, order, title: node.attrs.title,
        include_in_ai: node.attrs.include_in_ai, content: inner.content, formatting: inner.formatting })
    } else if (node.type === 'paragraph') line(node, 'paragraph')
    else if (node.type === 'heading') line(node, 'heading', node.attrs?.level)
    else if (node.type === 'blockquote') for (const child of node.content ?? []) line(child, 'blockquote')
    else if (node.type === 'bulletList' || node.type === 'orderedList') {
      for (const item of node.content ?? []) {
        if (item.type !== 'listItem' || item.content?.length !== 1) throw new UnsupportedRichDocument('嵌套列表不受支持。')
        line(item.content[0], node.type === 'bulletList' ? 'bullet_list' : 'ordered_list')
      }
    } else throw new UnsupportedRichDocument('编辑器包含未支持的节点。')
  }
  if (!count) throw new UnsupportedRichDocument('正文缺少段落。')
  if (new Set(boundaries).size !== boundaries.length) throw new UnsupportedRichDocument('零宽边界重复。')
  for (let first = 0; first < sections.length;) {
    let end = first + 1
    while (end < sections.length && sections[end].anchor === sections[first].anchor) end++
    const group = sections.slice(first, end)
    if (group.some((item, index) => !Number.isSafeInteger(item.order) || item.order < 0
      || index > 0 && group[index - 1].order >= item.order)) group.forEach((item, index) => { item.order = index })
    first = end
  }
  return { content, formatting: { version: 1, boundaries: boundaries.sort((a, b) => a - b), inline: orderedInline(inline), blocks }, sections }
}

export function projectSectionDocument(json: JSONContent): RichDocument {
  if (json.type !== 'doc' || !Array.isArray(json.content)) throw new UnsupportedRichDocument('文档根节点无效。')
  const markers: unknown = json.attrs?.markers ?? []; const retired: unknown = json.attrs?.retiredSectionKeys ?? []
  if (!Array.isArray(markers) || !Array.isArray(retired) || retired.some((item) => typeof item !== 'string')) throw new UnsupportedRichDocument('文档身份无效。')
  const orders: unknown = json.attrs?.sectionOrders ?? {}
  if (!orders || typeof orders !== 'object' || Array.isArray(orders)
    || Object.values(orders).some((value) => !Number.isSafeInteger(value) || (value as number) < 0)) {
    throw new UnsupportedRichDocument('区块顺序数据无效。')
  }
  const result = build(json.content, true, orders as Record<string, number>)
  if (new Set(result.sections.map((item) => item.key)).size !== result.sections.length) {
    throw new UnsupportedRichDocument('区块身份重复。')
  }
  return { ...result, markers: markers as RichDocument['markers'], retired_section_keys: retired }
}
