import { describe, expect, it } from 'vitest'
import { formatWordCount, normalizeTags, projectInfoBody, tagsError, wordsProgress } from './projectFields'

describe('类型标签规范化', () => {
  it('去首尾空白、丢弃空串、去重并保持顺序', () => {
    expect(normalizeTags([' 修仙 ', '', '穿越', '修仙', '  ', '群像'])).toEqual(['修仙', '穿越', '群像'])
    expect(normalizeTags(undefined)).toEqual([])
    expect(normalizeTags(null)).toEqual([])
  })

  it('超过 8 个 / 单个超过 10 字给出错误，按码点计长度', () => {
    expect(tagsError(['a', 'b'])).toBeNull()
    expect(tagsError(['1', '2', '3', '4', '5', '6', '7', '8'])).toBeNull()
    expect(tagsError(['1', '2', '3', '4', '5', '6', '7', '8', '9'])).toBe('类型标签最多 8 个')
    // 去重后只有 8 个 → 合法
    expect(tagsError(['1', '2', '3', '4', '5', '6', '7', '8', '8 '])).toBeNull()
    expect(tagsError(['一二三四五六七八九十'])).toBeNull()
    expect(tagsError(['一二三四五六七八九十一'])).toBe('类型标签每个最多 10 字：一二三四五六七八九十一')
    // emoji / 扩展 B 区按 1 字计
    expect(tagsError(['𠮷'.repeat(10)])).toBeNull()
  })
})

describe('写作进度', () => {
  it('没有目标为 null；取整且不超过 100', () => {
    expect(wordsProgress(1000, null)).toBeNull()
    expect(wordsProgress(1000, 0)).toBeNull()
    expect(wordsProgress(0, 300000)).toBe(0)
    expect(wordsProgress(150000, 300000)).toBe(50)
    expect(wordsProgress(99999, 100000)).toBe(99)
    expect(wordsProgress(400000, 300000)).toBe(100)
  })

  it('字数简写', () => {
    expect(formatWordCount(3000)).toBe('3000 字')
    expect(formatWordCount(300000)).toBe('30 万字')
    expect(formatWordCount(12000)).toBe('1.2 万字')
  })
})

describe('projectInfoBody', () => {
  it('trim、规范化标签、目标字数缺省为 null（PATCH 显式清空）', () => {
    expect(projectInfoBody({ synopsis: ' 简介 ', tags: [' A ', 'A', 'B'] })).toEqual({
      synopsis: '简介',
      theme: '',
      tags: ['A', 'B'],
      selling_points: '',
      audience: '',
      target_words: null,
    })
    expect(projectInfoBody({ target_words: 300000 }).target_words).toBe(300000)
  })
})
