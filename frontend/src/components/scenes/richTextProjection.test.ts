import { describe, expect, it } from 'vitest'
import type { RichDocument } from '../../api/richDocument'
import { editorFromDocument, projectEditorDocument, sameSupportedDocument } from './richTextProjection'

const empty = (content: string): RichDocument => ({
  content, formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [],
})

describe('富编辑器文本投影', () => {
  it.each(['', '甲\r\n乙\n末', '\r\n\n', '\r甲\r乙\r', '🙂e\u0301\t  \n尾  ', 'a\0b'])('不编辑时保留全部正文 %j', (text) => {
    const doc = empty(text)
    expect(projectEditorDocument(editorFromDocument(doc))).toEqual(doc)
  })
  it('可独立还原正文粗斜体和完整段落标题', () => {
    const doc = empty('甲🙂\r\n乙丙')
    doc.formatting.inline = [{ kind: 'bold', start: 0, end: 2 }, { kind: 'italic', start: 4, end: 5 }]
    doc.formatting.blocks = [{ kind: 'heading', start: 4, end: 6, level: 2 }]
    expect(projectEditorDocument(editorFromDocument(doc))).toEqual(doc)
  })
  it('无法表达跨换行样式和活动区块时明确拒绝，不简化', () => {
    const doc = empty('甲\r\n乙')
    doc.formatting.inline = [{ kind: 'bold', start: 0, end: 4 }]
    expect(() => editorFromDocument(doc)).toThrow()
    doc.formatting.inline = []
    doc.sections = [{ key: '12345678-1234-4123-8123-123456789abc', anchor: 0, order: 0, title: '备注',
      content: '', formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false }]
    expect(() => editorFromDocument(doc)).toThrow()
  })
  it('合法的倒序/交叉样式和跨多行列表按语义往返', () => {
    const doc = empty('甲乙\n丙丁\n戊己')
    doc.formatting.inline = [
      { kind: 'italic', start: 1, end: 2 }, { kind: 'bold', start: 6, end: 8 },
      { kind: 'bold', start: 0, end: 2 },
    ]
    doc.formatting.blocks = [{ kind: 'bullet_list', start: 0, end: 8, level: null }]
    const projected = projectEditorDocument(editorFromDocument(doc))
    expect(sameSupportedDocument(projected, doc)).toBe(true)
    expect(projected.formatting.blocks).toEqual(doc.formatting.blocks)
  })
  it('连续引用或列表中同一范围与不同范围均能区分并原样往返', () => {
    for (const kind of ['bullet_list', 'ordered_list', 'blockquote'] as const) {
      const single = empty('甲\r\n乙\n丙')
      single.formatting.blocks = [{ kind, start: 0, end: 6, level: null }]
      const split = empty(single.content)
      split.formatting.blocks = [
        { kind, start: 0, end: 1, level: null }, { kind, start: 3, end: 6, level: null },
      ]
      const fromSingle = projectEditorDocument(editorFromDocument(single))
      const fromSplit = projectEditorDocument(editorFromDocument(split))
      expect(fromSingle.formatting.blocks).toEqual(single.formatting.blocks)
      expect(fromSplit.formatting.blocks).toEqual(split.formatting.blocks)
      expect(sameSupportedDocument(fromSingle, split)).toBe(false)
    }
  })

  it('仅有退场区块身份时可编辑并完整保留墓碑，不需猜锚点', () => {
    const doc = empty('甲\r\n乙')
    doc.retired_section_keys = ['12345678-1234-4123-8123-123456789abc', '12345678-1234-4123-8123-123456789def']
    expect(projectEditorDocument(editorFromDocument(doc))).toEqual(doc)
  })

})
