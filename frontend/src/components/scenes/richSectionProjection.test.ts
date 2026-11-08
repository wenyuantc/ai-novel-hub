import { getSchema } from '@tiptap/core'
import { Schema } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { StarterKit } from '@tiptap/starter-kit'
import { describe, expect, it } from 'vitest'
import type { RichDocument, RichSection } from '../../api/richDocument'
import { editorFromSectionDocument, projectSectionDocument, sameSectionDocument } from './richSectionProjection'
import { markerDecorations, RichMarkerDocument } from './richMarkerState'
import { AuthorSection } from './richSectionNode'
import { FocusableListItem, RichPrefixes } from './richTextExtensions'

const formatting = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
const section = (key: string, anchor: number, order: number): RichSection => ({
  key, anchor, order, title: `备注${key}`, content: '备选🙂\r\n末', formatting: formatting(), include_in_ai: false,
})
const document = (content: string): RichDocument => ({ content, formatting: formatting(), sections: [], retired_section_keys: [], markers: [] })

describe('区块投影原语（未接入产品）', () => {
  it.each([0, 1, 2, 3, 4, 5])('前缀、正文与换行两侧的区块锚点 %i 往返', (anchor) => {
    const doc = document('甲\r\n🙂乙')
    doc.sections = [section('one', anchor, 7)]
    if (anchor === 2) { expect(() => editorFromSectionDocument(doc)).toThrow(); return } // CRLF 内部不是落点
    expect(sameSectionDocument(projectSectionDocument(editorFromSectionDocument(doc)), doc)).toBe(true)
  })
  it('同一锚点的多个区块维持稀疏顺序值，退场身份和标记不丢', () => {
    const doc = document('甲乙')
    doc.sections = [section('a', 1, 3), section('b', 1, 9)]
    doc.retired_section_keys = ['retired']
    doc.markers = [{ key: 'marker', section_key: 'a', status: 'active', start: 0, end: 2,
      color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }]
    expect(sameSectionDocument(projectSectionDocument(editorFromSectionDocument(doc)), doc)).toBe(true)
  })
  it('比较时忽略退场身份和标记的数据库返回顺序，但不忽略内容变化', () => {
    const doc = document('甲乙')
    doc.retired_section_keys = ['b', 'a']
    doc.markers = [{ key: 'two', section_key: null, status: 'active', start: 0, end: 1,
      color: 'red', excerpt: '', excerpt_truncated: false, last_start: null },
    { key: 'one', section_key: null, status: 'active', start: 1, end: 2,
      color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }]
    const reordered = { ...doc, retired_section_keys: [...doc.retired_section_keys].reverse(), markers: [...doc.markers].reverse() }
    expect(sameSectionDocument(doc, reordered)).toBe(true)
    expect(sameSectionDocument(doc, { ...reordered, markers: reordered.markers.map((item) => ({ ...item, color: 'green' })) })).toBe(false)
  })
  it('跨隐藏区块的同一个列表范围和两个不同范围保持区分', () => {
    for (const single of [true, false]) {
      const doc = document('甲乙')
      doc.sections = [section('a', 1, 0)]
      doc.formatting.boundaries = single ? [] : [1]
      doc.formatting.blocks = single ? [{ kind: 'bullet_list', start: 0, end: 2, level: null }] : [
        { kind: 'bullet_list', start: 0, end: 1, level: null }, { kind: 'bullet_list', start: 1, end: 2, level: null },
      ]
      expect(sameSectionDocument(projectSectionDocument(editorFromSectionDocument(doc)), doc)).toBe(true)
    }
  })
  it('跨 CRLF、区块锚点的交叉粗斜体与区块内格式不丢', () => {
    const doc = document('甲\r\n🙂乙')
    doc.formatting.inline = [{ kind: 'bold', start: 0, end: 5 }, { kind: 'italic', start: 3, end: 5 }]
    const item = section('a', 3, 0)
    item.formatting.inline = [{ kind: 'italic', start: 0, end: 3 }]
    doc.sections = [item]
    expect(sameSectionDocument(projectSectionDocument(editorFromSectionDocument(doc)), doc)).toBe(true)
  })
  it('固定种子覆盖 Unicode、混合换行、任意合法零宽边界和同锚顺序', () => {
    let seed = 0x1234abcd
    const random = (n: number) => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % n }
    const glyphs = ['甲', '🙂', 'e', '\u0301', '\r', '\n', '\t', ' ']
    for (let round = 0; round < 500; round++) {
      const text = Array.from({ length: random(35) }, () => glyphs[random(glyphs.length)]).join('')
      const chars = Array.from(text)
      const legal = Array.from({ length: chars.length + 1 }, (_, index) => index)
        .filter((index) => !(index > 0 && index < chars.length && chars[index - 1] === '\r' && chars[index] === '\n'))
      const doc = document(text)
      const positions = new Set<number>()
      for (let i = 0; i < random(5); i++) positions.add(legal[random(legal.length)])
      doc.formatting.boundaries = [...positions].sort((a, b) => a - b)
      const anchors = new Map<number, number>()
      for (let i = 0; i < random(5); i++) {
        const anchor = legal[random(legal.length)]; const order = (anchors.get(anchor) ?? 0) + 2
        anchors.set(anchor, order); doc.sections.push(section(`s${i}`, anchor, order))
      }
      const projected = projectSectionDocument(editorFromSectionDocument(doc))
      expect(sameSectionDocument(projected, doc), `round=${round} ${JSON.stringify(doc)}`).toBe(true)
    }
  })

  it('固定种子覆盖倒序交叉行内样式、段落格式和区块锚点', () => {
    let seed = 0x7ea51
    const random = (n: number) => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % n }
    const glyphs = ['甲', '🙂', 'e', '\u0301', '\r', '\n', ' ']
    for (let round = 0; round < 500; round++) {
      const text = Array.from({ length: 5 + random(40) }, () => glyphs[random(glyphs.length)]).join('')
      const chars = Array.from(text)
      const legal = Array.from({ length: chars.length + 1 }, (_, index) => index)
        .filter((index) => !(index > 0 && index < chars.length && chars[index - 1] === '\r' && chars[index] === '\n'))
      const doc = document(text)
      for (const kind of ['italic', 'bold'] as const) {
        const a = random(legal.length - 1); const b = a + 1 + random(legal.length - a - 1)
        doc.formatting.inline.push({ kind, start: legal[a], end: legal[b] })
      }
      let start = 0
      for (let index = 0; index <= chars.length; index++) {
        if (index < chars.length && chars[index] !== '\r' && chars[index] !== '\n') continue
        if (start < index && random(3) === 0) doc.formatting.blocks.push({ kind: 'heading', start, end: index, level: 2 })
        if (chars[index] === '\r' && chars[index + 1] === '\n') index++
        start = index + 1
      }
      for (let i = 0; i < 1 + random(4); i++) doc.sections.push(section(`s${i}`, legal[random(legal.length)], i))
      expect(sameSectionDocument(projectSectionDocument(editorFromSectionDocument(doc)), doc), `round=${round} ${JSON.stringify(doc)}`).toBe(true)
    }
  })

  it('长文、多区块与稀疏格式按排序范围处理而非逐行扫描全部元数据', () => {
    const doc = document('甲\n'.repeat(12_000))
    for (let index = 0; index < 1_000; index++) doc.sections.push(section(`s${index}`, index * 20, 0))
    for (let index = 0; index < 6_000; index++) doc.formatting.inline.push({ kind: 'bold', start: index * 4, end: index * 4 + 1 })
    doc.formatting.inline.reverse()
    expect(sameSectionDocument(projectSectionDocument(editorFromSectionDocument(doc)), doc)).toBe(true)
  }, 20_000)

  it('经真实 ProseMirror schema 序列化仍保留段属性、区块和标记', () => {
    const attrs = { prefix: { default: '\n' }, prefixMarks: { default: [] }, blockKey: { default: null },
      boundaryBefore: { default: false }, scaffold: { default: false } }
    const schema = new Schema({ nodes: {
      doc: { content: 'block+', attrs: { markers: { default: [] }, retiredSectionKeys: { default: [] }, sectionOrders: { default: {} } } },
      paragraph: { group: 'block', content: 'text*', attrs },
      heading: { group: 'block', content: 'text*', attrs: { ...attrs, level: { default: 2 } } },
      blockquote: { group: 'block', content: 'paragraph+' },
      bulletList: { group: 'block', content: 'listItem+' }, orderedList: { group: 'block', content: 'listItem+' },
      listItem: { content: 'paragraph' },
      authorSection: { group: 'block', content: 'block+', attrs: { key: {}, title: {}, include_in_ai: { default: false } } },
      text: { group: 'inline' },
    }, marks: { bold: {}, italic: {} } })
    const doc = document('甲\r\n🙂乙')
    doc.sections = [section('a', 1, 3), section('b', 1, 9), section('c', 3, 1)]
    doc.formatting.inline = [{ kind: 'bold', start: 0, end: 5 }]
    doc.formatting.blocks = [{ kind: 'bullet_list', start: 0, end: 5, level: null }]
    const node = schema.nodeFromJSON(editorFromSectionDocument(doc))
    expect(sameSectionDocument(projectSectionDocument(node.toJSON()), doc)).toBe(true)
  })

  it('真实 Tiptap / AuthorSection schema 保留所有自定义属性与双向正文', () => {
    const schema = getSchema([StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
      horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
    RichMarkerDocument, FocusableListItem, RichPrefixes, AuthorSection])
    const doc = document('甲\r\n🙂乙')
    doc.sections = [section('a', 1, 3), section('b', 1, 9), section('c', 3, 1)]
    doc.formatting.inline = [{ kind: 'bold', start: 0, end: 5 }]
    doc.formatting.blocks = [{ kind: 'bullet_list', start: 0, end: 5, level: null }]
    const node = schema.nodeFromJSON(editorFromSectionDocument(doc))
    expect(sameSectionDocument(projectSectionDocument(node.toJSON()), doc)).toBe(true)
  })

  it('正文和区块标记按各自目标坐标装饰，不被另一目标文字污染', () => {
    const schema = getSchema([StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
      horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
    RichMarkerDocument, FocusableListItem, RichPrefixes, AuthorSection])
    const doc = document('甲乙')
    doc.sections = [section('a', 1, 0)]
    doc.markers = [
      { key: 'body', section_key: null, status: 'active', start: 1, end: 2, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null },
      { key: 'part', section_key: 'a', status: 'active', start: 0, end: 1, color: 'red', excerpt: '', excerpt_truncated: false, last_start: null },
    ]
    const node = schema.nodeFromJSON(editorFromSectionDocument(doc))
    const decorations = markerDecorations(EditorState.create({ schema, doc: node })).find()
    const chars = decorations.map((mark) => node.textBetween(mark.from, mark.to))
    expect(chars).toEqual(['备', '乙'])
  })

})
