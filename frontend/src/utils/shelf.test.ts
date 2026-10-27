import { describe, expect, it } from 'vitest'
import type { Project } from '../api/types'
import {
  canRead,
  continueTarget,
  contrastRatio,
  coverColor,
  COVER_HUES,
  DEFAULT_SHELF_PREFS,
  filterAndSort,
  parseShelfPrefs,
  readingLabel,
  readTarget,
  shelfStats,
} from './shelf'

function p(over: Partial<Project>): Project {
  return {
    id: 1,
    title: '书',
    genre: '',
    premise: '',
    status: 'planning',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    chapter_count: 0,
    total_words: 0,
    auto_analyze: true,
    wizard_progress: { done: [], skipped: [], current: null },
    default_style_id: null,
    default_style: null,
    synopsis: '',
    theme: '',
    tags: [],
    selling_points: '',
    audience: '',
    target_words: null,
    last_edited_chapter_id: null,
    reading: null,
    ...over,
  }
}

const list = [
  p({ id: 1, title: '星河尽头', tags: ['科幻', 'Space'], status: 'writing', total_words: 5000, updated_at: '2026-03-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z' }),
  p({ id: 2, title: '阿房宫', tags: ['历史'], status: 'planning', total_words: 100, updated_at: '2026-05-01T00:00:00Z', created_at: '2026-02-01T00:00:00Z' }),
  p({ id: 3, title: '仙途', tags: ['玄幻'], status: 'completed', total_words: 90000, updated_at: '2026-04-01T00:00:00Z', created_at: '2025-12-01T00:00:00Z' }),
]

describe('书架筛选排序', () => {
  it('搜索匹配书名与标签（大小写不敏感）', () => {
    expect(filterAndSort(list, { ...DEFAULT_SHELF_PREFS, query: 'space' }).map((x) => x.id)).toEqual([1])
    expect(filterAndSort(list, { ...DEFAULT_SHELF_PREFS, query: '仙' }).map((x) => x.id)).toEqual([3])
    expect(filterAndSort(list, { ...DEFAULT_SHELF_PREFS, query: '  ' })).toHaveLength(3)
  })

  it('状态筛选', () => {
    expect(filterAndSort(list, { ...DEFAULT_SHELF_PREFS, status: 'writing' }).map((x) => x.id)).toEqual([1])
  })

  it('四种排序', () => {
    const ids = (sort: 'updated' | 'created' | 'title' | 'words') => filterAndSort(list, { ...DEFAULT_SHELF_PREFS, sort }).map((x) => x.id)
    expect(ids('updated')).toEqual([2, 3, 1])
    expect(ids('created')).toEqual([2, 1, 3])
    expect(ids('words')).toEqual([3, 1, 2])
    expect(ids('title')).toEqual([...list].sort((a, b) => a.title.localeCompare(b.title, 'zh')).map((x) => x.id))
  })

  it('不修改入参', () => {
    const copy = [...list]
    filterAndSort(list, { ...DEFAULT_SHELF_PREFS, sort: 'words' })
    expect(list).toEqual(copy)
  })

  it('偏好解析容错', () => {
    expect(parseShelfPrefs(null)).toEqual(DEFAULT_SHELF_PREFS)
    expect(parseShelfPrefs('not json')).toEqual(DEFAULT_SHELF_PREFS)
    expect(parseShelfPrefs('{"query":"a","sort":"words","status":"writing"}')).toEqual({ query: 'a', sort: 'words', status: 'writing' })
    expect(parseShelfPrefs('{"sort":"x","status":"y","query":3}')).toEqual(DEFAULT_SHELF_PREFS)
  })
})

describe('书架统计与入口', () => {
  it('统计作品数、连载中数、总字数', () => {
    expect(shelfStats(list)).toEqual({ count: 3, writing: 1, words: 95100 })
    expect(shelfStats([])).toEqual({ count: 0, writing: 0, words: 0 })
  })

  it('继续写作：最近编辑章节，没有则概览', () => {
    expect(continueTarget({ id: 7, last_edited_chapter_id: 42 })).toBe('/projects/7/chapters/42')
    expect(continueTarget({ id: 7, last_edited_chapter_id: null })).toBe('/projects/7/overview')
  })

  it('阅读：文案、跳转与可用性', () => {
    const reading = { chapter_id: 9, chapter_no: 3, ratio: 0.5 }
    expect(readingLabel(reading)).toBe('读到第 3 章')
    expect(readingLabel(null)).toBe('开始阅读')
    expect(readTarget({ id: 7, reading })).toBe('/projects/7/read/9')
    expect(readTarget({ id: 7, reading: null })).toBe('/projects/7/read')
    expect(canRead({ last_edited_chapter_id: null, reading: null })).toBe(false)
    expect(canRead({ last_edited_chapter_id: 1, reading: null })).toBe(true)
  })
})

describe('书封配色', () => {
  it('同一书名颜色恒定，不同主题不同', () => {
    expect(coverColor('星河尽头', 'light')).toEqual(coverColor('星河尽头', 'light'))
    expect(coverColor(' 星河尽头 ', 'light')).toEqual(coverColor('星河尽头', 'light'))
    expect(coverColor('星河尽头', 'dark').bg).not.toBe(coverColor('星河尽头', 'light').bg)
  })

  it('所有色相在两种主题下文字对比度 ≥ 4.5', () => {
    // 找出落在每个色相上的书名，覆盖全部 12 种
    const seen = new Map<string, string>()
    for (let i = 0; i < 2000 && seen.size < COVER_HUES.length; i++) {
      const c = coverColor(`书${i}`, 'light')
      if (!seen.has(c.bg)) seen.set(c.bg, `书${i}`)
    }
    expect(seen.size).toBe(COVER_HUES.length)
    for (const title of seen.values()) {
      for (const theme of ['light', 'dark'] as const) {
        const c = coverColor(title, theme)
        expect(contrastRatio(c.bg, c.text)).toBeGreaterThanOrEqual(4.5)
        expect(c.spine).toMatch(/^#[0-9a-f]{6}$/)
      }
    }
  })

  it('对比度计算', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 0)
    expect(contrastRatio('#777777', '#777777')).toBeCloseTo(1, 5)
  })
})
