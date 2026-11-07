import { describe, expect, it } from 'vitest'
import { chapterOptions, describeAnchor, describeBoundary, toPositionInput } from './position'

const chapters = [
  { id: 12, chapter_no: 20, title: '晋升' },
  { id: 7, chapter_no: 5, title: '初遇' },
  { id: 8, chapter_no: 5, title: '' },
]

describe('设定变化的章节位置', () => {
  it('按章号和 ID 稳定展示已有章节，同章号仍可用各自的 ID 选择', () => {
    const options = chapterOptions(chapters)
    expect(options).toEqual([
      { value: 7, label: '第 5 章 · 初遇' },
      { value: 8, label: '第 5 章 · 未命名' },
      { value: 12, label: '第 20 章 · 晋升' },
    ])
    expect(chapters[0].id).toBe(12)
  })

  it('绑定真实章节时发送稳定 ID，不把章号当作章节身份', () => {
    expect(toPositionInput({ anchor_kind: 'chapter', chapter_id: 12, chapter_no: 20, boundary: 'end' })).toEqual({
      anchor_kind: 'chapter', chapter_id: 12, chapter_no: null, boundary: 'end',
    })
    expect(toPositionInput({ anchor_kind: 'chapter', chapter_id: null, chapter_no: 20, boundary: 'start' })).toBeNull()
  })

  it('计划章保留独立正整数章号，不虚构可跳转的章节 ID', () => {
    expect(toPositionInput({ anchor_kind: 'planned', chapter_id: 12, chapter_no: 30, boundary: 'start' })).toEqual({
      anchor_kind: 'planned', chapter_id: null, chapter_no: 30, boundary: 'start',
    })
    for (const chapter_no of [null, 0, -1, 1.5, 2147483648, Number.NaN]) {
      expect(toPositionInput({ anchor_kind: 'planned', chapter_id: null, chapter_no, boundary: 'start' })).toBeNull()
    }
  })

  it('显示关联章的当前标题，计划章与已删除章节有不同提示', () => {
    expect(describeAnchor({ anchor_kind: 'chapter', chapter_id: 7, chapter_no: null, anchor_label: '', resolved_chapter_no: 5, orphaned: false }, chapters)).toBe('第 5 章 · 初遇')
    expect(describeAnchor({ anchor_kind: 'planned', chapter_id: null, chapter_no: 9, anchor_label: '', resolved_chapter_no: 9, orphaned: false }, chapters)).toBe('第 9 章（计划章节）')
    expect(describeAnchor({ anchor_kind: 'chapter', chapter_id: null, chapter_no: null, anchor_label: '第 5 章 · 旧标题', resolved_chapter_no: null, orphaned: true }, chapters)).toBe('第 5 章 · 旧标题（关联章节已删除）')
  })
  it.each([
    { anchor_kind: 'scene' as const, boundary: 'start' as const, label: '从本场景起' },
    { anchor_kind: 'scene' as const, boundary: 'end' as const, label: '本场景结束后' },
    { anchor_kind: 'chapter' as const, boundary: 'start' as const, label: '从本章起' },
    { anchor_kind: 'chapter' as const, boundary: 'end' as const, label: '本章结束后' },
    { anchor_kind: 'planned' as const, boundary: 'start' as const, label: '从本章起' },
    { anchor_kind: 'planned' as const, boundary: 'end' as const, label: '本章结束后' },
  ])('$anchor_kind $boundary 显示真实生效边界', ({ anchor_kind, boundary, label }) => {
    expect(describeBoundary({ anchor_kind, boundary })).toBe(label)
  })
  it('职业历史保留旧章初／章末短标签，但真实场景仍显示场景边界', () => {
    for (const anchor_kind of ['chapter', 'planned'] as const) {
      expect(describeBoundary({ anchor_kind, boundary: 'start' }, 'short')).toBe('章初')
      expect(describeBoundary({ anchor_kind, boundary: 'end' }, 'short')).toBe('章末')
    }
    expect(describeBoundary({ anchor_kind: 'scene', boundary: 'start' }, 'short')).toBe('从本场景起')
    expect(describeBoundary({ anchor_kind: 'scene', boundary: 'end' }, 'short')).toBe('本场景结束后')
  })
  it.each(['start', 'end'] as const)('孤儿场景保留 %s 语义，不误称章已删除或章边界', (boundary) => {
    for (const scene_id of [null, 29]) {
      const event = { anchor_kind: 'scene' as const, scene_id, chapter_id: null, chapter_no: null, anchor_label: '第 1 章 · 场景 2 · 桥头初遇', resolved_chapter_no: null, orphaned: true, boundary }
      expect(describeAnchor(event, chapters)).toBe('第 1 章 · 场景 2 · 桥头初遇（场景已退场或失联）')
      expect(describeBoundary(event)).toBe(boundary === 'start' ? '从本场景起' : '本场景结束后')
      expect(describeAnchor(event, chapters)).not.toContain('关联章节已删除')
    }
  })
})
