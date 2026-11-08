import { describe, expect, it } from 'vitest'
import type { RichDocument } from '../../api/richDocument'
import { validSectionDocument } from './richSectionValidity'

const format = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
const document = (): RichDocument => ({ content: '甲🙂\r\n乙', formatting: format(), sections: [
  { key: 'a', anchor: 1, order: 0, title: '备选', content: '一\r\n二', formatting: format(), include_in_ai: false },
], retired_section_keys: [], markers: [{ key: 'm', section_key: 'a', status: 'active', start: 0, end: 3,
  color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }] })

describe('区块文档保存前的坐标与结构门禁', () => {
  it('合法 Unicode 正文、区块和标记通过，不接受任一目标的 CRLF 中点', () => {
    expect(validSectionDocument(document())).toBe(true)
    const anchor = document(); anchor.sections[0].anchor = 3
    expect(validSectionDocument(anchor)).toBe(false)
    const marker = document(); marker.markers[0].end = 2
    expect(validSectionDocument(marker)).toBe(false)
    const boundary = document(); boundary.formatting.boundaries = [3]
    expect(validSectionDocument(boundary)).toBe(false)
  })
  it('拒绝重复身份、悬空标记和同锚顺序冲突', () => {
    const duplicated = document(); duplicated.retired_section_keys = ['a']
    expect(validSectionDocument(duplicated)).toBe(false)
    const missing = document(); missing.markers[0].section_key = 'missing'
    expect(validSectionDocument(missing)).toBe(false)
    const order = document(); order.sections.push({ ...order.sections[0], key: 'b' })
    expect(validSectionDocument(order)).toBe(false)
  })
  it('拒绝不能完整覆盖段落的块格式与非法 Unicode', () => {
    const block = document(); block.formatting.blocks = [{ kind: 'heading', start: 1, end: 2, level: 2 }]
    expect(validSectionDocument(block)).toBe(false)
    const invalid = document(); invalid.content = '甲\ud800乙'
    expect(validSectionDocument(invalid)).toBe(false)
  })
})
