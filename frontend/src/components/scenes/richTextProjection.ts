import type { JSONContent } from '@tiptap/core'
import type { RichBlockStyle, RichDocument, RichInlineStyle } from '../../api/richDocument'

/** 无法完整表达时保持只读，不能把作者数据降级成纯文本。 */
export class UnsupportedRichDocument extends Error {}
type Kind = 'paragraph' | RichBlockStyle['kind']
interface Line { prefix: string; start: number; end: number }

function lines(text: string): { chars: string[]; lines: Line[] } {
  const chars = Array.from(text)
  const result: Line[] = []
  let start = 0; let prefix = ''; let index = 0
  while (index < chars.length) {
    if (chars[index] !== '\r' && chars[index] !== '\n') { index++; continue }
    result.push({ prefix, start, end: index })
    prefix = chars[index] === '\r' && chars[index + 1] === '\n' ? '\r\n' : chars[index]
    index += Array.from(prefix).length
    start = index
  }
  result.push({ prefix, start, end: index })
  return { chars, lines: result }
}

function normalizedInline(items: RichInlineStyle[]): RichInlineStyle[] {
  const ordered = [...items].sort((a, b) => a.kind.localeCompare(b.kind) || a.start - b.start || a.end - b.end)
  const result: RichInlineStyle[] = []
  for (const item of ordered) {
    const last = result.at(-1)
    if (last && last.kind === item.kind && last.end === item.start) last.end = item.end
    else result.push({ ...item })
  }
  return result
}

function normalizedBlocks(document: RichDocument): RichBlockStyle[] {
  // 块范围的身份影响零宽边界处的列表／引用语义；不能拍平成逐行样式。
  return [...document.formatting.blocks].sort((a, b) => a.start - b.start || a.end - b.end
    || a.kind.localeCompare(b.kind) || (a.level ?? 0) - (b.level ?? 0))
}

/** 排序和连续段落范围有多种合法表示；比较其文本与逐字符/逐段语义。 */
export function sameSupportedDocument(a: RichDocument, b: RichDocument): boolean {
  if (a.content !== b.content || a.formatting.version !== b.formatting.version) return false
  if (a.sections.length || a.formatting.boundaries.length || b.sections.length || b.formatting.boundaries.length
    || JSON.stringify(a.retired_section_keys) !== JSON.stringify(b.retired_section_keys)) return false
  return JSON.stringify(normalizedInline(a.formatting.inline)) === JSON.stringify(normalizedInline(b.formatting.inline))
    && JSON.stringify(normalizedBlocks(a)) === JSON.stringify(normalizedBlocks(b)) && JSON.stringify(a.markers) === JSON.stringify(b.markers)
}

/** 区块、零宽边界及跨换行行内格式暂不开放写入。 */
export function editorFromDocument(document: RichDocument): JSONContent {
  if (document.sections.length || document.markers.some((item) => item.section_key !== null) || document.formatting.boundaries.length) {
    throw new UnsupportedRichDocument('该场景含区块或零宽结构，暂不能在此编辑器中安全编辑。')
  }
  const { chars, lines: paragraphs } = lines(document.content)
  for (const style of document.formatting.inline) {
    if (!paragraphs.some((line) => line.start <= style.start && style.end <= line.end)) {
      throw new UnsupportedRichDocument('跨换行样式暂不能安全编辑。')
    }
  }
  for (const style of document.formatting.blocks) {
    const matching = paragraphs.filter((line) => style.start <= line.start && line.end <= style.end)
    if (!matching.length || matching[0].start !== style.start || matching.at(-1)?.end !== style.end
      || style.kind === 'heading' && matching.length !== 1) throw new UnsupportedRichDocument('段落格式无法完整映射。')
  }
  const nodes: JSONContent[] = []
  for (const line of paragraphs) {
    const styleIndex = document.formatting.blocks.findIndex((item) => item.start <= line.start && line.end <= item.end)
    const style = document.formatting.blocks[styleIndex]
    const cuts = new Set([line.start, line.end])
    for (const item of document.formatting.inline) {
      if (line.start <= item.start && item.end <= line.end) { cuts.add(item.start); cuts.add(item.end) }
    }
    const positions = [...cuts].sort((a, b) => a - b)
    const content: JSONContent[] = []
    for (let index = 1; index < positions.length; index++) {
      const start = positions[index - 1]; const end = positions[index]
      const marks = (['bold', 'italic'] as const)
        .filter((kind) => document.formatting.inline.some((item) => item.kind === kind && item.start <= start && end <= item.end))
        .map((type) => ({ type }))
      content.push({ type: 'text', text: chars.slice(start, end).join(''), ...(marks.length ? { marks } : {}) })
    }
    const paragraph: JSONContent = { type: style?.kind === 'heading' ? 'heading' : 'paragraph',
      attrs: { prefix: line.prefix, blockKey: style ? `saved:${styleIndex}` : null, ...(style?.kind === 'heading' ? { level: style.level } : {}) },
      ...(content.length ? { content } : {}) }
    if (style?.kind === 'bullet_list' || style?.kind === 'ordered_list') {
      nodes.push({ type: style.kind === 'bullet_list' ? 'bulletList' : 'orderedList', content: [{ type: 'listItem', content: [paragraph] }] })
    } else if (style?.kind === 'blockquote') nodes.push({ type: 'blockquote', content: [paragraph] })
    else nodes.push(paragraph)
  }
  const result: JSONContent = { type: 'doc', attrs: { markers: document.markers, retiredSectionKeys: document.retired_section_keys }, content: nodes }
  const projected = projectEditorDocument(result)
  if (!sameSupportedDocument(projected, document)) throw new UnsupportedRichDocument('当前格式不能完整往返，原稿保持只读。')
  return result
}

export function projectEditorDocument(json: JSONContent): RichDocument {
  if (json.type !== 'doc' || !Array.isArray(json.content) || json.content.length === 0) throw new UnsupportedRichDocument('编辑器根结构无效。')
  let content = ''; let position = 0; let count = 0
  const runs: Record<'bold' | 'italic', RichInlineStyle[]> = { bold: [], italic: [] }
  const blocks: RichBlockStyle[] = []
  let previousBlockKey: string | null = null
  const line = (node: JSONContent, kind: Kind, level: number | null = null) => {
    if (!['paragraph', 'heading'].includes(node.type ?? '')) throw new UnsupportedRichDocument('段落结构不受支持。')
    const prefix: unknown = node.attrs?.prefix
    if (typeof prefix !== 'string' || !['', '\n', '\r', '\r\n'].includes(prefix) || (count === 0 && prefix !== '') || (count > 0 && prefix === '')) {
      throw new UnsupportedRichDocument('段落分隔无法可靠映射。')
    }
    content += prefix; position += Array.from(prefix).length; count++
    const start = position
    for (const child of node.content ?? []) {
      if (child.type !== 'text' || typeof child.text !== 'string' || /[\r\n]/.test(child.text)) throw new UnsupportedRichDocument('正文节点无法可靠映射。')
      const kinds = new Set((child.marks ?? []).map((mark) => mark.type))
      if ([...kinds].some((mark) => mark !== 'bold' && mark !== 'italic')) throw new UnsupportedRichDocument('包含未支持的文字样式。')
      const end = position + Array.from(child.text).length
      for (const mark of ['bold', 'italic'] as const) {
        if (!kinds.has(mark)) continue
        const list = runs[mark]; const last = list.at(-1)
        if (last && last.end === position) last.end = end
        else list.push({ kind: mark, start: position, end })
      }
      content += child.text; position = end
    }
    const blockKey: unknown = node.attrs?.blockKey ?? null
    if (blockKey !== null && typeof blockKey !== 'string') throw new UnsupportedRichDocument('段落身份无效。')
    if (kind !== 'paragraph') {
      const last = blocks.at(-1)
      if (blockKey && blockKey === previousBlockKey && last?.kind === kind && last.level === level) last.end = position
      else blocks.push({ kind, start, end: position, level })
      previousBlockKey = blockKey
    } else previousBlockKey = null
  }
  for (const node of json.content) {
    if (node.type === 'paragraph') line(node, 'paragraph')
    else if (node.type === 'heading') {
      const level = node.attrs?.level
      if (!Number.isInteger(level) || level < 1 || level > 6) throw new UnsupportedRichDocument('标题层级无效。')
      line(node, 'heading', level)
    } else if (node.type === 'blockquote') {
      for (const child of node.content ?? []) line(child, 'blockquote')
    } else if (node.type === 'bulletList' || node.type === 'orderedList') {
      for (const item of node.content ?? []) {
        if (item.type !== 'listItem' || item.content?.length !== 1) throw new UnsupportedRichDocument('嵌套列表暂不支持。')
        line(item.content[0], node.type === 'bulletList' ? 'bullet_list' : 'ordered_list')
      }
    } else throw new UnsupportedRichDocument('编辑器包含未支持的块。')
  }
  if (!count) throw new UnsupportedRichDocument('正文缺少段落。')
  const markers: unknown = json.attrs?.markers ?? []
  if (!Array.isArray(markers)) throw new UnsupportedRichDocument('标记元数据无效。')
  const retired: unknown = json.attrs?.retiredSectionKeys ?? []
  if (!Array.isArray(retired) || retired.some((item) => typeof item !== 'string')) throw new UnsupportedRichDocument('退场区块身份无效。')
  return { content, formatting: { version: 1, boundaries: [], inline: [...runs.bold, ...runs.italic], blocks }, sections: [], retired_section_keys: retired, markers: markers as RichDocument['markers'] }
}
