import { describe, expect, it } from 'vitest'
import type { RichDocument, RichMarker, RichSection } from '../../api/richDocument'
import { equivalentSectionMaterials, planSectionCreation, planSectionDeletion, planSectionMove } from './richSectionOperations'

const format = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
const part = (key: string, anchor: number, order: number): RichSection => ({ key, anchor, order, title: key,
  content: '甲🙂\r\n乙', formatting: format(), include_in_ai: false })
const base = (): RichDocument => ({ content: '正🙂\r\n文', formatting: format(), sections: [part('a', 1, 3), part('b', 1, 9)],
  retired_section_keys: [], markers: [] })
const marker = (): RichMarker => ({ key: 'm', section_key: 'a', status: 'active', start: 0, end: 2, color: 'blue',
  excerpt: '', excerpt_truncated: false, last_start: null })
const cursor = (leftSection: string | null, rightSection: string | null) => ({ scope: 'prose' as const,
  offset: 1, leftSection, rightSection })

describe('区块操作计划', () => {
  it.each([
    [null, 'a', ['c', 'a', 'b']], ['a', 'b', ['a', 'c', 'b']], ['b', null, ['a', 'b', 'c']],
  ] as const)('同一正文码点按区块身份和边向插入', (left, right, expected) => {
    const original = base(); const result = planSectionCreation(original, cursor(left, right), 'c')
    expect(result.sections.map((item) => item.key)).toEqual(expected)
    expect(result.sections.map((item) => item.anchor)).toEqual([1, 1, 1])
    expect(result.sections.find((item) => item.key === 'c')).toMatchObject({ content: '', include_in_ai: false })
    expect(result.content).toBe(original.content)
    expect(original.sections.map((item) => item.order)).toEqual([3, 9])
  })
  it('过时或伪造的相邻身份及 CRLF 中点不能成为插入点', () => {
    expect(() => planSectionCreation(base(), cursor(null, 'b'), 'c')).toThrow()
    expect(() => planSectionCreation(base(), { scope: 'prose', offset: 3, leftSection: null, rightSection: null }, 'c')).toThrow()
    expect(() => planSectionCreation(base(), { scope: 'section', sectionKey: 'a', offset: 1 }, 'c')).toThrow()
  })
  it('按已确认的同锚边向移动，并保留原正文、区块内容和标记', () => {
    const original = base(); original.markers.push(marker())
    const moved = planSectionMove(original, 'b', cursor(null, 'a'))
    expect(moved.sections.map((item) => item.key)).toEqual(['b', 'a'])
    expect(moved.sections.find((item) => item.key === 'a')).toMatchObject({ anchor: 1, title: 'a', content: '甲🙂\r\n乙' })
    expect(moved.content).toBe(original.content)
    expect(moved.markers).toEqual(original.markers)
    expect(equivalentSectionMaterials(original, moved)).toBe(false)
  })
  it('跨码点移动只改变目标区块锚点，不改变正文、格式及其他材料', () => {
    const original = base(); original.markers.push(marker())
    const moved = planSectionMove(original, 'a', { scope: 'prose', offset: 4, leftSection: null, rightSection: null })
    expect(moved.sections.find((item) => item.key === 'a')?.anchor).toBe(4)
    expect(moved.sections.find((item) => item.key === 'b')).toEqual(original.sections[1])
    expect(moved.content).toBe(original.content)
    expect(moved.formatting).toEqual(original.formatting)
    expect(moved.markers).toEqual(original.markers)
  })
  it('删除已保存区块时保留同身份墓碑，并以该区块实际文本保留失效标记', () => {
    const saved = base(); saved.markers.push(marker())
    const result = planSectionDeletion(saved, 'a', saved)
    expect(result.sections.map((item) => item.key)).toEqual(['b'])
    expect(result.retired_section_keys).toEqual(['a'])
    expect(result.markers[0]).toMatchObject({ section_key: 'a', status: 'stale', excerpt: '甲🙂', last_start: 0 })
  })
  it('删除尚未保存但已有标记的新区块须先保存，不能制造悬空墓碑', () => {
    const saved = base(); const draft = planSectionCreation(saved, cursor('a', 'b'), 'c')
    draft.markers.push({ ...marker(), section_key: 'c' })
    expect(() => planSectionDeletion(draft, 'c', saved)).toThrow('请先保存')
    draft.markers = []
    expect(planSectionDeletion(draft, 'c', saved).retired_section_keys).toEqual([])
  })
  it('在未保存草稿中更改区块后删除，分别保留可校验基线和草稿末态片段', () => {
    const saved = base(); saved.markers.push(marker())
    const draft = { ...saved, sections: saved.sections.map((item) => item.key === 'a' ? { ...item, content: '甲X🙂\r\n乙' } : item),
      markers: [{ ...saved.markers[0], end: 3 }] }
    expect(planSectionDeletion(draft, 'a', saved).markers[0]).toMatchObject({ status: 'stale', excerpt: '甲🙂',
      draft_excerpt: '甲X🙂', draft_last_start: 0 })
  })
})
