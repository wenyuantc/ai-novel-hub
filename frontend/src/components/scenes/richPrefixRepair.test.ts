import { Schema } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'
import { repairRichPrefixes, verifyRichPrefixRepair } from './richPrefixRepair'

const schema = new Schema({ nodes: {
  doc: { content: 'block+' },
  paragraph: { group: 'block', content: 'text*', attrs: {
    prefix: { default: '\n' }, prefixMarks: { default: [] }, blockKey: { default: null },
    boundaryBefore: { default: false }, scaffold: { default: false },
  } },
  blockquote: { group: 'block', content: 'paragraph+' },
  authorSection: { group: 'block', content: 'block+', attrs: { key: {} } },
  text: { group: 'inline' },
} })
const paragraph = (prefix: string, text = '相同') => schema.node('paragraph', { prefix }, schema.text(text))
const section = (key: string, prefix: string) => schema.node('authorSection', { key }, [
  schema.node('blockquote', null, [paragraph(prefix)]),
])

describe('正文及区块的段落前缀修复', () => {
  it('同字区块交换顺序时，只从对应区块身份恢复分隔符', () => {
    const before = schema.node('doc', null, [paragraph(''), section('a', '\r\n'), section('b', '\r')])
    const after = schema.node('doc', null, [paragraph('\n'), section('b', '\n'), section('a', '\n')])
    const result = repairRichPrefixes(before, after)
    expect(result).not.toBeNull()
    const nodes = result!.doc.toJSON().content!
    expect(nodes[0].attrs?.prefix).toBe('')
    expect(nodes[1].content?.[0].content?.[0].attrs?.prefix).toBe('\r')
    expect(nodes[2].content?.[0].content?.[0].attrs?.prefix).toBe('\r\n')
  })

  it('同一目标段落文字发生变化时不猜测修复', () => {
    const before = schema.node('doc', null, [paragraph(''), section('a', '\r\n')])
    const after = schema.node('doc', null, [paragraph(''), schema.node('authorSection', { key: 'a' }, [paragraph('\n', '其他')])])
    expect(repairRichPrefixes(before, after)).toBeNull()
  })

  it('重复身份及新增或删除区块时不跨目标猜测', () => {
    const before = schema.node('doc', null, [paragraph(''), section('a', '\r\n')])
    expect(repairRichPrefixes(before, schema.node('doc', null, [paragraph(''), section('a', '\n'), section('a', '\n')]))).toBeNull()
    expect(repairRichPrefixes(before, schema.node('doc', null, [paragraph(''), section('b', '\n')]))).toBeNull()
  })
  it('前缀修复只能修改原文字块的属性，不能夹带文字替换', () => {
    const state = EditorState.create({ schema, doc: schema.node('doc', null, [paragraph('\n')]) })
    const node = state.doc.firstChild!
    const transaction = state.tr.setNodeMarkup(0, undefined, { ...node.attrs, prefix: '' })
    expect(verifyRichPrefixRepair(transaction)).toBe(true)
    transaction.insertText('X', 1)
    expect(verifyRichPrefixRepair(transaction)).toBe(false)
  })
})
