import { describe, expect, it } from 'vitest'
import { chapterOptions, describeAnchor, toPositionInput } from './position'

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
})
