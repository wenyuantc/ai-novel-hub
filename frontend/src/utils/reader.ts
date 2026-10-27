// 阅读器纯函数：阅读设置（localStorage）、进度 ratio、目录分组 / 相邻章节、正文分段
import type { ReaderTocItem, ReadingPosition } from '../api/types'
import type { ReaderTone, ResolvedTheme } from '../theme/palette'

export type ReaderBackground = ReaderTone | 'theme'
export type ReaderFont = 'serif' | 'sans'

export interface ReaderSettings {
  fontSize: number
  lineHeight: number
  width: number
  background: ReaderBackground
  font: ReaderFont
}

export const READER_STORAGE_KEY = 'ainovel.reader'
export const READER_FONT_SIZE = { min: 14, max: 24 } as const
export const READER_LINE_HEIGHTS = [1.5, 1.8, 2.0, 2.2] as const
export const READER_WIDTHS = [560, 680, 800] as const
export const READER_BACKGROUNDS: { value: ReaderBackground; label: string }[] = [
  { value: 'theme', label: '跟随全局' },
  { value: 'light', label: '浅色' },
  { value: 'sepia', label: '护眼' },
  { value: 'dark', label: '暗色' },
]
export const READER_FONTS: Record<ReaderFont, { label: string; stack: string }> = {
  serif: { label: '宋体', stack: "'Songti SC', 'STSong', 'SimSun', 'Noto Serif SC', 'Source Han Serif SC', serif" },
  sans: {
    label: '黑体',
    stack: "'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans SC', 'Source Han Sans SC', sans-serif",
  },
}

export const DEFAULT_READER_SETTINGS: ReaderSettings = {
  fontSize: 18,
  lineHeight: 1.8,
  width: 680,
  background: 'theme',
  font: 'serif',
}

/** 任意输入 → 合法设置：字号取整并夹在 14–24，行距 / 宽度 / 背景 / 字体不在可选值中时取默认。 */
export function normalizeReaderSettings(raw: unknown): ReaderSettings {
  const d = DEFAULT_READER_SETTINGS
  if (typeof raw !== 'object' || raw === null) return { ...d }
  const o = raw as Record<string, unknown>
  const size = typeof o.fontSize === 'number' && Number.isFinite(o.fontSize) ? Math.round(o.fontSize) : d.fontSize
  return {
    fontSize: Math.min(READER_FONT_SIZE.max, Math.max(READER_FONT_SIZE.min, size)),
    lineHeight: (READER_LINE_HEIGHTS as readonly number[]).includes(o.lineHeight as number) ? (o.lineHeight as number) : d.lineHeight,
    width: (READER_WIDTHS as readonly number[]).includes(o.width as number) ? (o.width as number) : d.width,
    background: READER_BACKGROUNDS.some((b) => b.value === o.background) ? (o.background as ReaderBackground) : d.background,
    font: o.font === 'serif' || o.font === 'sans' ? o.font : d.font,
  }
}

export function readReaderSettings(): ReaderSettings {
  try {
    const raw = localStorage.getItem(READER_STORAGE_KEY)
    return normalizeReaderSettings(raw ? JSON.parse(raw) : null)
  } catch {
    return { ...DEFAULT_READER_SETTINGS }
  }
}

export function writeReaderSettings(s: ReaderSettings): void {
  try {
    localStorage.setItem(READER_STORAGE_KEY, JSON.stringify(s))
  } catch {
    // 不可写时忽略
  }
}

/** 实际使用的阅读背景：跟随全局时按全局主题取浅色 / 暗色 */
export function readerTone(background: ReaderBackground, resolved: ResolvedTheme): ReaderTone {
  return background === 'theme' ? resolved : background
}

// ---------------- 进度 ----------------

/** 滚动位置 → 章内进度 0–1（内容不足一屏时为 0），保留 4 位小数 */
export function scrollRatio(scrollTop: number, scrollHeight: number, clientHeight: number): number {
  const max = scrollHeight - clientHeight
  if (!(max > 0) || !Number.isFinite(scrollTop)) return 0
  const r = Math.min(1, Math.max(0, scrollTop / max))
  return Math.round(r * 10000) / 10000
}

/** 章内进度 → 滚动位置 */
export function scrollTopForRatio(ratio: number, scrollHeight: number, clientHeight: number): number {
  const max = scrollHeight - clientHeight
  if (!(max > 0) || !Number.isFinite(ratio)) return 0
  return Math.round(Math.min(1, Math.max(0, ratio)) * max)
}

/** 进入阅读器（没有指定章节）时从哪一章开始：有进度且该章在目录中 → 该章；否则第一个已写章节；目录为空 → null */
export function startChapterId(toc: Pick<ReaderTocItem, 'id'>[], reading: Pick<ReadingPosition, 'chapter_id'> | null): number | null {
  if (reading && toc.some((t) => t.id === reading.chapter_id)) return reading.chapter_id
  return toc[0]?.id ?? null
}

/** 进入某章时要恢复的进度：只有这一章就是上次读到的章节才恢复，否则从头 */
export function restoreRatio(chapterId: number, reading: Pick<ReadingPosition, 'chapter_id' | 'ratio'> | null): number {
  return reading && reading.chapter_id === chapterId ? reading.ratio : 0
}

// ---------------- 目录 ----------------

export interface TocGroup {
  /** null = 不属于任何卷 */
  volumeNo: number | null
  label: string
  items: ReaderTocItem[]
}

/** 按章节号排序后把相邻同卷的章节归为一组，卷名取该组第一个非空的 volume_title；没有卷号的组 label 为空 */
export function groupToc(toc: ReaderTocItem[]): TocGroup[] {
  const sorted = [...toc].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
  const groups: (TocGroup & { title: string })[] = []
  for (const t of sorted) {
    const last = groups[groups.length - 1]
    if (last && last.volumeNo === t.volume_no) {
      if (!last.title && t.volume_title) last.title = t.volume_title
      last.items.push(t)
    } else {
      groups.push({ volumeNo: t.volume_no, title: t.volume_title ?? '', label: '', items: [t] })
    }
  }
  return groups.map(({ title, ...g }) => ({ ...g, label: g.volumeNo === null ? '' : volumeText(g.volumeNo, title) }))
}

function volumeText(no: number, title: string): string {
  return `第${no}卷${title ? ` ${title}` : ''}`
}

/** 当前章（按章节号）的上一个 / 下一个已写章节；当前章本身不在目录中（如正文为空）也能定位 */
export function tocNeighbors(
  toc: ReaderTocItem[],
  chapterNo: number,
): { prev: ReaderTocItem | null; next: ReaderTocItem | null } {
  const sorted = [...toc].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
  let prev: ReaderTocItem | null = null
  let next: ReaderTocItem | null = null
  for (const t of sorted) {
    if (t.chapter_no < chapterNo) prev = t
    else if (t.chapter_no > chapterNo && next === null) next = t
  }
  return { prev, next }
}

/** 正文按换行切段，去掉首尾空白，空行忽略 */
export function splitParagraphs(content: string): string[] {
  return content
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** 键盘切章：焦点在输入控件 / 可编辑区域、或按着修饰键时不响应 */
export function arrowDirection(e: {
  key: string
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
  shiftKey?: boolean
  target?: { tagName?: string; isContentEditable?: boolean } | null
}): 'prev' | 'next' | null {
  if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return null
  const tag = e.target?.tagName?.toUpperCase()
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target?.isContentEditable) return null
  if (e.key === 'ArrowLeft') return 'prev'
  if (e.key === 'ArrowRight') return 'next'
  return null
}
