import { getSchema } from '@tiptap/core'
import { EditorState, TextSelection } from '@tiptap/pm/state'
import { StarterKit } from '@tiptap/starter-kit'
import { describe, expect, it } from 'vitest'
import type { RichDocument, RichMarker } from '../../api/richDocument'
import { richCursorCandidates } from './richEditorPositions'
import { RichMarkerDocument } from './richMarkerState'
import { mapSectionMarkerTransaction } from './richSectionMarkerMapping'
import { AuthorSection } from './richSectionNode'
import { editorFromSectionDocument } from './richSectionProjection'
import { FocusableListItem, RichPrefixes } from './richTextExtensions'

const schema = getSchema([StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
  horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
RichMarkerDocument, FocusableListItem, RichPrefixes, AuthorSection])
const marker = (key: string, section_key: string | null, start: number, end: number): RichMarker => ({
  key, section_key, status: 'active', start, end, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null,
})
function setup() {
  const format = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
  const document: RichDocument = { content: '甲乙', formatting: format(), sections: [
    { key: 'a', anchor: 1, order: 0, title: '备注', content: '备🙂\r\n末', formatting: format(), include_in_ai: false },
  ], retired_section_keys: [], markers: [marker('body', null, 1, 2), marker('part', 'a', 0, 2)] }
  return { document, state: EditorState.create({ schema, doc: schema.nodeFromJSON(editorFromSectionDocument(document)) }) }
}

describe('区块和正文标记的事务映射原语（未接入产品）', () => {
  it('正文在区块前插入只移动正文标记，不改变区块内码点', () => {
    const { document, state } = setup()
    const pos = richCursorCandidates(state.doc, { scope: 'prose', offset: 1 })[0].pm
    const result = mapSectionMarkerTransaction(state.tr.insertText('X', pos), document.markers, document)
    expect(result?.[0]).toMatchObject({ status: 'active', start: 2, end: 3 })
    expect(result?.[1]).toMatchObject({ status: 'active', start: 0, end: 2 })
  })
  it('区块内插入只改变区块标记，不改变正文标记', () => {
    const { document, state } = setup()
    const pos = richCursorCandidates(state.doc, { scope: 'section', sectionKey: 'a', offset: 1 })[0].pm
    const result = mapSectionMarkerTransaction(state.tr.insertText('X', pos), document.markers, document)
    expect(result?.[0]).toMatchObject({ status: 'active', start: 1, end: 2 })
    expect(result?.[1]).toMatchObject({ status: 'active', start: 0, end: 3 })
  })
  it('替换整个区块标记为相同字面也失效，保留该区块自身旧摘录', () => {
    const { document, state } = setup()
    const from = richCursorCandidates(state.doc, { scope: 'section', sectionKey: 'a', offset: 0 })[0].pm
    const to = richCursorCandidates(state.doc, { scope: 'section', sectionKey: 'a', offset: 2 })[0].pm
    const result = mapSectionMarkerTransaction(state.tr.insertText('备🙂', from, to), document.markers, document)
    expect(result?.[0]).toMatchObject({ status: 'active', start: 1, end: 2 })
    expect(result?.[1]).toMatchObject({ status: 'stale', excerpt: '备🙂', last_start: 0 })
  })
  it('跨区块删除不能以旧坐标冒充可靠映射', () => {
    const { document, state } = setup()
    const from = richCursorCandidates(state.doc, { scope: 'prose', offset: 0 })[0].pm
    const to = richCursorCandidates(state.doc, { scope: 'prose', offset: 2 })[0].pm
    expect(mapSectionMarkerTransaction(state.tr.delete(from, to), document.markers, document)).toBeNull()
  })
  it('整个区块被同字节点替换时，不能因字面相同而假装跟踪了内部标记', () => {
    const { document, state } = setup()
    let from = -1; let node = state.doc.firstChild!
    state.doc.forEach((child, pos) => {
      if (child.type.name === 'authorSection') { from = pos; node = child }
    })
    expect(from).toBeGreaterThanOrEqual(0)
    const transaction = state.tr.replaceWith(from, from + node.nodeSize, node)
    expect(transaction.doc.eq(state.doc)).toBe(true)
    expect(mapSectionMarkerTransaction(transaction, document.markers, document)).toBeNull()
  })
  it('其他区块中同字替换不能移动或失效正文和当前区块的标记', () => {
    const { document } = setup()
    document.sections.push({ ...document.sections[0], key: 'b', anchor: 2 })
    const state = EditorState.create({ schema, doc: schema.nodeFromJSON(editorFromSectionDocument(document)) })
    const from = richCursorCandidates(state.doc, { scope: 'section', sectionKey: 'b', offset: 0 })[0].pm
    const to = richCursorCandidates(state.doc, { scope: 'section', sectionKey: 'b', offset: 2 })[0].pm
    expect(mapSectionMarkerTransaction(state.tr.insertText('备🙂', from, to), document.markers, document)).toEqual(document.markers)
  })
  it('区块内 Enter 的拆段和显式 LF 前缀可证明为一次码点插入', () => {
    const { document, state } = setup()
    const pos = richCursorCandidates(state.doc, { scope: 'section', sectionKey: 'a', offset: 1 })[0].pm
    const transaction = state.tr.setSelection(TextSelection.create(state.doc, pos)).split(pos)
    const parent = transaction.selection.$from.parent
    transaction.setNodeMarkup(transaction.selection.$from.before(), undefined, { ...parent.attrs, prefix: '\n' })
    const result = mapSectionMarkerTransaction(transaction, document.markers, document)
    expect(result?.[0]).toMatchObject({ status: 'active', start: 1, end: 2 })
    expect(result?.[1]).toMatchObject({ status: 'active', start: 0, end: 3 })
  })
  it('大量不同区块的标记共享一次 StepMap 端点扫描', () => {
    const formatting = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
    const document: RichDocument = { content: '甲'.repeat(20_000), formatting: formatting(), sections: [], retired_section_keys: [], markers: [] }
    for (let index = 0; index < 1_000; index++) {
      const key = `s${index}`
      document.sections.push({ key, anchor: index * 20, order: 0, title: '备注', content: '备', formatting: formatting(), include_in_ai: false })
      document.markers.push(marker(`m${index}`, key, 0, 1))
    }
    const state = EditorState.create({ schema, doc: schema.nodeFromJSON(editorFromSectionDocument(document)) })
    const pos = richCursorCandidates(state.doc, { scope: 'prose', offset: 20_000 })[0].pm
    expect(mapSectionMarkerTransaction(state.tr.insertText('X', pos), document.markers, document)).toEqual(document.markers)
  }, 20_000)
})
