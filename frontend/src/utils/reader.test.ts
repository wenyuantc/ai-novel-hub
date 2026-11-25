import { describe, expect, it } from 'vitest'
import type { ReaderTocItem } from '../api/types'
import {
  arrowDirection,
  DEFAULT_READER_SETTINGS,
  groupToc,
  normalizeReaderSettings,
  readerTone,
  readerParagraphs,
  restoreRatio,
  scrollRatio,
  scrollTopForRatio,
  splitParagraphs,
  startChapterId,
  tocNeighbors,
} from './reader'

const t = (id: number, chapter_no: number, volume_no: number | null = null, volume_title: string | null = null): ReaderTocItem => ({
  id,
  chapter_no,
  title: `章${chapter_no}`,
  word_count: 100,
  volume_no,
  volume_title,
})

describe('阅读设置', () => {
  it('默认值', () => {
    expect(normalizeReaderSettings(null)).toEqual(DEFAULT_READER_SETTINGS)
    expect(normalizeReaderSettings('x')).toEqual(DEFAULT_READER_SETTINGS)
  })

  it('字号夹在 14–24 并取整，非法项回落默认', () => {
    expect(normalizeReaderSettings({ fontSize: 40 }).fontSize).toBe(24)
    expect(normalizeReaderSettings({ fontSize: 3 }).fontSize).toBe(14)
    expect(normalizeReaderSettings({ fontSize: 16.6 }).fontSize).toBe(17)
    expect(normalizeReaderSettings({ fontSize: Number.NaN }).fontSize).toBe(18)
    const s = normalizeReaderSettings({ lineHeight: 3, width: 1000, background: 'pink', font: 'mono' })
    expect(s).toEqual(DEFAULT_READER_SETTINGS)
  })

  it('合法值原样保留', () => {
    const v = { speechRate: 1.5, fontSize: 20, lineHeight: 2.2, width: 800, background: 'sepia', font: 'sans' }
    expect(normalizeReaderSettings(v)).toEqual(v)
  })

  it('旧设置和非法语速回落到正常语速', () => {
    for (const speechRate of [undefined, null, NaN, 10, '1.5']) {
      expect(normalizeReaderSettings({ speechRate }).speechRate).toBe(1)
    }
  })

  it('背景跟随全局', () => {
    expect(readerTone('theme', 'dark')).toBe('dark')
    expect(readerTone('theme', 'light')).toBe('light')
    expect(readerTone('sepia', 'dark')).toBe('sepia')
  })
})

describe('阅读进度', () => {
  it('ratio 计算与夹取', () => {
    expect(scrollRatio(0, 2000, 1000)).toBe(0)
    expect(scrollRatio(500, 2000, 1000)).toBe(0.5)
    expect(scrollRatio(1500, 2000, 1000)).toBe(1)
    expect(scrollRatio(100, 800, 1000)).toBe(0)
    expect(scrollRatio(1, 3001, 1000)).toBe(0.0005)
  })

  it('ratio → 滚动位置（往返一致）', () => {
    expect(scrollTopForRatio(0.5, 2000, 1000)).toBe(500)
    expect(scrollTopForRatio(2, 2000, 1000)).toBe(1000)
    expect(scrollTopForRatio(0.3, 500, 1000)).toBe(0)
    expect(scrollRatio(scrollTopForRatio(0.37, 5000, 800), 5000, 800)).toBeCloseTo(0.37, 3)
  })

  it('起始章节：有进度且在目录中 → 该章，否则第一个已写章节', () => {
    const toc = [t(11, 1), t(12, 2)]
    expect(startChapterId(toc, { chapter_id: 12 })).toBe(12)
    expect(startChapterId(toc, { chapter_id: 99 })).toBe(11)
    expect(startChapterId(toc, null)).toBe(11)
    expect(startChapterId([], null)).toBeNull()
  })

  it('只有上次读到的那一章才恢复位置', () => {
    expect(restoreRatio(5, { chapter_id: 5, ratio: 0.4 })).toBe(0.4)
    expect(restoreRatio(6, { chapter_id: 5, ratio: 0.4 })).toBe(0)
    expect(restoreRatio(6, null)).toBe(0)
  })
})

describe('目录', () => {
  it('按卷分组（相邻同卷合并，卷名取第一个非空）', () => {
    const groups = groupToc([t(3, 3, 2, '风起'), t(1, 1, 1, null), t(2, 2, 1, '初入'), t(4, 4, null, null), t(5, 5, 3, '')])
    expect(groups.map((g) => [g.label, g.items.map((i) => i.id)])).toEqual([
      ['第1卷 初入', [1, 2]],
      ['第2卷 风起', [3]],
      ['', [4]],
      ['第3卷', [5]],
    ])
  })

  it('相邻章节（当前章不在目录中也能定位）', () => {
    const toc = [t(1, 1), t(3, 3), t(5, 5)]
    expect(tocNeighbors(toc, 3)).toEqual({ prev: toc[0], next: toc[2] })
    expect(tocNeighbors(toc, 4)).toEqual({ prev: toc[1], next: toc[2] })
    expect(tocNeighbors(toc, 1)).toEqual({ prev: null, next: toc[1] })
    expect(tocNeighbors(toc, 5).next).toBeNull()
  })

  it('分段忽略空行', () => {
    expect(splitParagraphs('  第一段\n\n\r\n第二段  \n')).toEqual(['第一段', '第二段'])
  })

  it('方向键：输入框内与修饰键不响应', () => {
    expect(arrowDirection({ key: 'ArrowLeft' })).toBe('prev')
    expect(arrowDirection({ key: 'ArrowRight', target: { tagName: 'div' } })).toBe('next')
    expect(arrowDirection({ key: 'ArrowRight', target: { tagName: 'INPUT' } })).toBeNull()
    expect(arrowDirection({ key: 'ArrowRight', target: { tagName: 'DIV', isContentEditable: true } })).toBeNull()
    expect(arrowDirection({ key: 'ArrowRight', metaKey: true })).toBeNull()
    expect(arrowDirection({ key: 'a' })).toBeNull()
  })
})


describe('朗读段落', () => {
  it('跳过空段，保留缩进、重复段和换行后的原始码点位置', () => {
    expect(readerParagraphs('  🌸甲  \r\n \n  🌸甲  \r结')).toEqual([
      { start: 0, end: 6, text: '  🌸甲  ' },
      { start: 10, end: 16, text: '  🌸甲  ' },
      { start: 17, end: 18, text: '结' },
    ])
  })

  it('富文本限定在读者块范围内，整段引用可以有换行', () => {
    const block = { kind: 'blockquote' as const, level: null, runs: [] }
    expect(readerParagraphs('🌸\n甲\n乙\n尾', [{ ...block, start: 0, end: 1 }, { ...block, start: 2, end: 5 }])).toEqual([
      { start: 0, end: 1, text: '🌸' }, { start: 2, end: 5, text: '甲\n乙' },
    ])
  })
})
