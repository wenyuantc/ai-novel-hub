import { Schema } from '@tiptap/pm/model'
import { describe, expect, it } from 'vitest'
import type { RichMarker } from '../../api/richDocument'
import { markersWithoutTextNodes } from './richMarkerVisibility'

const schema = new Schema({ nodes: {
  doc: { content: 'block+', attrs: { markers: { default: [] } } },
  paragraph: { content: 'text*', group: 'block', attrs: { prefix: { default: '' } } },
  authorSection: { content: 'paragraph+', group: 'block', attrs: { key: {} } },
  text: { group: 'inline' },
} })
const marker = (key: string, start: number, end: number): RichMarker => ({
  key, section_key: null, status: 'active', start, end, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null,
})
describe('无法依靠文字节点呈现的标记', () => {
  it('区分只覆盖 CRLF、连续空段、末尾换行和同时覆盖 emoji 的标记', () => {
    const doc = schema.node('doc', { markers: [marker('crlf', 1, 3), marker('empty', 4, 6), marker('end', 7, 8), marker('emoji', 3, 4), marker('mixed', 1, 4)] }, [
      schema.node('paragraph', { prefix: '' }, schema.text('甲')),
      schema.node('paragraph', { prefix: '\r\n' }, schema.text('🙂')),
      schema.node('paragraph', { prefix: '\n' }),
      schema.node('paragraph', { prefix: '\n' }, schema.text('乙')),
      schema.node('paragraph', { prefix: '\n' }),
    ])
    expect(markersWithoutTextNodes(doc).map((item) => item.key)).toEqual(['crlf', 'empty', 'end'])
  })
  it('区块内仅换行标记不被相同偏移的正文文字遮盖', () => {
    const doc = schema.node('doc', { markers: [marker('body', 0, 1), { ...marker('section-crlf', 1, 3), section_key: 'a' }] }, [
      schema.node('paragraph', { prefix: '' }, schema.text('甲乙丙丁')),
      schema.node('authorSection', { key: 'a' }, [
        schema.node('paragraph', { prefix: '' }, schema.text('备')),
        schema.node('paragraph', { prefix: '\r\n' }, schema.text('🙂')),
      ]),
    ])
    expect(markersWithoutTextNodes(doc).map((item) => item.key)).toEqual(['section-crlf'])
  })
})
