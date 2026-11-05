import { describe, expect, it } from 'vitest'
import type { MentionChapter, MentionSummaryEntry } from '../api/mentions'
import {
  chapterWindow,
  mentionCellLabel,
  mentionChapterRows,
  mentionEntityKey,
  mentionRangeError,
  mentionScopeLabel,
  mentionSummaryQuery,
  mentionTimeline,
} from './mentionStats'

const chapters: MentionChapter[] = [
  { id: 30, chapter_no: 1, title: '初遇', has_content: true },
  { id: 7, chapter_no: 4, title: '计划章', has_content: false },
  { id: 8, chapter_no: 9, title: '归来', has_content: true },
]
const entry: MentionSummaryEntry = {
  entity: { kind: 'character', id: 3, category: 'character', name: '林远', aliases: ['阿远'] },
  mention_count: 2, ambiguous_count: 1, chapter_count: 1, last_chapter_no: 1, gap_chapters: 1,
  counts: [{ chapter_id: 30, count: 2, ambiguous_count: 0 }, { chapter_id: 8, count: 0, ambiguous_count: 1 }],
}

describe('提及统计展示', () => {
  it('按稳定章节 id 对齐稀疏结果，保留空章与歧义', () => {
    expect(mentionChapterRows([entry], chapters).map((row) => ({
      chapter: row.chapter.chapter_no, count: row.count, ambiguous: row.ambiguous_count, unwritten: !row.chapter.has_content,
    }))).toEqual([
      { chapter: 1, count: 2, ambiguous: 0, unwritten: false },
      { chapter: 4, count: 0, ambiguous: 0, unwritten: true },
      { chapter: 9, count: 0, ambiguous: 1, unwritten: false },
    ])
  })

  it('数值表保留已写零值，而时间线只显示实际命中且不把歧义计为确定', () => {
    const rows = mentionChapterRows([{ ...entry, counts: [{ chapter_id: 8, count: 0, ambiguous_count: 1 }] }], chapters)
    expect(rows[0].count).toBe(0)
    expect(mentionTimeline(rows).map((row) => row.chapter.chapter_no)).toEqual([9])
    expect(mentionTimeline(rows)[0].count).toBe(0)
  })

  it('键包含实体类型，角色与世界观相同 id 不混淆', () => {
    expect(mentionEntityKey(entry.entity)).toBe('character:3')
    expect(mentionEntityKey({ kind: 'world', id: 3 })).toBe('world:3')
  })

  it('一页最多显示50个实际章节，不按不连续章号补假章节', () => {
    const many = Array.from({ length: 103 }, (_, i) => ({ id: i + 1, chapter_no: i * 3 + 1, title: '', has_content: true }))
    const middle = chapterWindow(many, 2)
    expect(middle.page).toBe(2)
    expect(middle.items).toHaveLength(50)
    expect(middle.items[0].chapter_no).toBe(151)
    expect(chapterWindow(many, 99).items.map((chapter) => chapter.id)).toEqual([101, 102, 103])
    expect(chapterWindow(many, -1).page).toBe(1)
  })

  it('空章节列表不会出现负页号或虚构范围', () => {
    expect(chapterWindow([], 9)).toEqual({ items: [], page: 1, pages: 1, total: 0 })
    expect(mentionScopeLabel()).toBe('全书已保存正文')
    expect(mentionScopeLabel(4, 9)).toBe('第 4–9 章的已保存正文')
    expect(mentionScopeLabel(undefined, 9)).toBe('截至第 9 章的已保存正文')
  })

  it('可读标签明确说明空章与歧义，不能只依赖颜色', () => {
    const rows = mentionChapterRows([entry], chapters)
    expect(mentionCellLabel(rows[1])).toContain('未写')
    expect(mentionCellLabel(rows[2])).toContain('0 次确定提及')
    expect(mentionCellLabel(rows[2])).toContain('1 次有歧义')
  })
})

describe('统计筛选合同', () => {
  it('精确条目携带 kind/id，并在服务端分页前筛选类型和关键字', () => {
    expect(mentionSummaryQuery({ category: 'place', entity: { kind: 'world', id: 7 }, q: ' 旧都 ', chapterFrom: 2, chapterTo: 8 }, 3))
      .toEqual({ category: 'place', entity_kind: 'world', entity_id: 7, q: '旧都', chapter_from: 2, chapter_to: 8, page: 3, page_size: 20 })
  })

  it('未筛选的值不伪造为0或空条目', () => {
    const query = mentionSummaryQuery({ q: ' ' }, 1)
    expect(query.entity_id).toBeUndefined()
    expect(query.entity_kind).toBeUndefined()
    expect(query.q).toBeUndefined()
    expect(query.chapter_from).toBeUndefined()
  })

  it('拒绝倒置章节区间、非整数和非法章号', () => {
    expect(mentionRangeError(9, 2)).toBe('起始章不能大于结束章')
    expect(mentionRangeError(0, 2)).toBe('章节号须为大于 0 的整数')
    expect(mentionRangeError(1.5, 2)).toBe('章节号须为大于 0 的整数')
    expect(mentionRangeError(1, 2)).toBe('')
    expect(mentionRangeError(undefined, undefined)).toBe('')
  })
})
