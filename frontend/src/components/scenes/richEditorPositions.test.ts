import { getSchema } from '@tiptap/core'
import { StarterKit } from '@tiptap/starter-kit'
import { describe, expect, it } from 'vitest'
import type { RichDocument, RichSection } from '../../api/richDocument'
import { findRichCursor, resolveRichCursor, resolveRichSelection, richCursorCandidates } from './richEditorPositions'
import { RichMarkerDocument } from './richMarkerState'
import { AuthorSection } from './richSectionNode'
import { editorFromSectionDocument } from './richSectionProjection'
import { FocusableListItem, RichPrefixes } from './richTextExtensions'

const format = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
const section = (key: string, anchor: number, order: number): RichSection => ({
  key, anchor, order, title: key, include_in_ai: false, content: '注🙂\r\n末', formatting: format(),
})
const schema = getSchema([StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
  horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
RichMarkerDocument, FocusableListItem, RichPrefixes, AuthorSection])
function make(content: string, sections: RichSection[]) {
  const document: RichDocument = { content, sections, formatting: format(), markers: [], retired_section_keys: [] }
  return schema.nodeFromJSON(editorFromSectionDocument(document))
}

describe('正文／区块结构化光标', () => {
  it('两个同锚区块两侧及中间的同一正文码点按身份和偏向双向映射', () => {
    const doc = make('甲乙', [section('a', 1, 3), section('b', 1, 9)])
    const candidates = richCursorCandidates(doc, { scope: 'prose', offset: 1 })
    expect(candidates.map((item) => item.cursor)).toEqual([
      { scope: 'prose', offset: 1, leftSection: null, rightSection: 'a' },
      { scope: 'prose', offset: 1, leftSection: 'a', rightSection: 'b' },
      { scope: 'prose', offset: 1, leftSection: 'b', rightSection: null },
    ])
    for (const item of candidates) {
      expect(resolveRichCursor(doc, item.pm)).toEqual(item.cursor)
      expect(findRichCursor(doc, item.cursor)).toBe(item.pm)
    }
  })
  it('区块内光标有独立目标身份；CRLF 中点和 UTF-16 代理对中点不可落位', () => {
    const doc = make('甲\r\n🙂乙', [section('a', 1, 0)])
    expect(richCursorCandidates(doc, { scope: 'prose', offset: 2 })).toEqual([])
    const before = richCursorCandidates(doc, { scope: 'prose', offset: 3 })[0]
    const after = richCursorCandidates(doc, { scope: 'prose', offset: 4 })[0]
    expect(after.pm - before.pm).toBe(2)
    expect(resolveRichCursor(doc, before.pm + 1)).toBeNull()
    const inner = richCursorCandidates(doc, { scope: 'section', sectionKey: 'a', offset: 1 })[0]
    expect(inner.cursor).toEqual({ scope: 'section', sectionKey: 'a', offset: 1 })
    expect(findRichCursor(doc, inner.cursor)).toBe(inner.pm)
    expect(resolveRichSelection(doc, before.pm, inner.pm)).toBeNull()
  })
})
