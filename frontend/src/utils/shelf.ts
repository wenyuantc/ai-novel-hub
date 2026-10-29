// 书架（项目列表）纯函数：筛选排序、统计、书封配色、阅读 / 继续写作入口
import type { Project, ProjectStatus, ReadingPosition } from '../api/types'
import { COVER_TEXT, type ResolvedTheme } from '../theme/palette'
import { parseServerTime } from './time'

export type ShelfSort = 'updated' | 'created' | 'title' | 'words'
export type ShelfStatusFilter = 'all' | ProjectStatus

export interface ShelfPrefs {
  query: string
  sort: ShelfSort
  status: ShelfStatusFilter
  view: 'shelf' | 'list'
}

export const SHELF_STORAGE_KEY = 'ainovel.shelf'
export const DEFAULT_SHELF_PREFS: ShelfPrefs = { query: '', sort: 'updated', status: 'all', view: 'shelf' }

export const SHELF_SORT_OPTIONS: { value: ShelfSort; label: string }[] = [
  { value: 'updated', label: '最近更新' },
  { value: 'created', label: '创建时间' },
  { value: 'title', label: '书名' },
  { value: 'words', label: '字数' },
]

const SORTS: ShelfSort[] = ['updated', 'created', 'title', 'words']
const STATUSES: ShelfStatusFilter[] = ['all', 'planning', 'writing', 'completed']

/** localStorage 的值 → 合法偏好（非法字段取默认）。 */
export function parseShelfPrefs(raw: string | null): ShelfPrefs {
  if (!raw) return { ...DEFAULT_SHELF_PREFS }
  try {
    const v: unknown = JSON.parse(raw)
    if (typeof v !== 'object' || v === null) return { ...DEFAULT_SHELF_PREFS }
    const o = v as Record<string, unknown>
    return {
      query: typeof o.query === 'string' ? o.query.slice(0, 100) : '',
      sort: SORTS.includes(o.sort as ShelfSort) ? (o.sort as ShelfSort) : DEFAULT_SHELF_PREFS.sort,
      status: STATUSES.includes(o.status as ShelfStatusFilter) ? (o.status as ShelfStatusFilter) : 'all',
      view: o.view === 'list' ? 'list' : 'shelf',
    }
  } catch {
    return { ...DEFAULT_SHELF_PREFS }
  }
}

export function readShelfPrefs(): ShelfPrefs {
  try {
    return parseShelfPrefs(localStorage.getItem(SHELF_STORAGE_KEY))
  } catch {
    return { ...DEFAULT_SHELF_PREFS }
  }
}

export function writeShelfPrefs(prefs: ShelfPrefs): void {
  try {
    localStorage.setItem(SHELF_STORAGE_KEY, JSON.stringify(prefs))
  } catch {
    // 不可写时忽略
  }
}

type ShelfProject = Pick<Project, 'id' | 'title' | 'tags' | 'status' | 'updated_at' | 'created_at' | 'total_words'> & { genre?: string }

/** 搜索（书名 + 题材 + 标签，大小写不敏感）→ 状态筛选 → 排序；不修改入参。 */
export function filterAndSort<T extends ShelfProject>(projects: T[], prefs: ShelfPrefs): T[] {
  const q = prefs.query.trim().toLowerCase()
  const list = projects.filter((p) => {
    if (prefs.status !== 'all' && p.status !== prefs.status) return false
    if (!q) return true
    return p.title.toLowerCase().includes(q) || (p.genre ?? '').toLowerCase().includes(q) || (p.tags ?? []).some((t) => t.toLowerCase().includes(q))
  })
  const byTime = (a: string, b: string) => parseServerTime(b).valueOf() - parseServerTime(a).valueOf() // 新在前
  list.sort((a, b) => {
    switch (prefs.sort) {
      case 'created':
        return byTime(a.created_at, b.created_at) || b.id - a.id
      case 'title':
        return a.title.localeCompare(b.title, 'zh') || b.id - a.id
      case 'words':
        return b.total_words - a.total_words || byTime(a.updated_at, b.updated_at)
      default:
        return byTime(a.updated_at, b.updated_at) || b.id - a.id
    }
  })
  return list
}

/** 继续写作条：只选择有真实已写章节的作品，按现有项目更新时间排序。 */
export function latestWritingProject<T extends Pick<Project, 'id' | 'updated_at' | 'last_edited_chapter_id'>>(projects: T[]): T | null {
  return projects.filter((p) => p.last_edited_chapter_id !== null).sort((a, b) => parseServerTime(b.updated_at).valueOf() - parseServerTime(a.updated_at).valueOf() || b.id - a.id)[0] ?? null
}

export interface ShelfStats {
  count: number
  /** 状态为「连载中」（writing）的作品数 */
  writing: number
  words: number
}

export function shelfStats(projects: Pick<Project, 'status' | 'total_words'>[]): ShelfStats {
  return {
    count: projects.length,
    writing: projects.filter((p) => p.status === 'writing').length,
    words: projects.reduce((s, p) => s + (p.total_words || 0), 0),
  }
}

/** 「阅读」按钮文案 */
export function readingLabel(reading: ReadingPosition | null): string {
  return reading ? `读到第 ${reading.chapter_no} 章` : '开始阅读'
}

/** 是否有可读的章节（有已写章节时 last_edited_chapter_id 非空） */
export function canRead(p: Pick<Project, 'last_edited_chapter_id' | 'reading'>): boolean {
  return p.last_edited_chapter_id !== null || p.reading !== null
}

/** 「阅读」跳转：有进度到该章；否则到阅读器入口（自动从第一个已写章节开始） */
export function readTarget(p: Pick<Project, 'id' | 'reading'>): string {
  return p.reading ? `/projects/${p.id}/read/${p.reading.chapter_id}` : `/projects/${p.id}/read`
}

/** 「继续写作」跳转：最近编辑的章节，没有则到概览 */
export function continueTarget(p: Pick<Project, 'id' | 'last_edited_chapter_id'>): string {
  return p.last_edited_chapter_id !== null
    ? `/projects/${p.id}/chapters/${p.last_edited_chapter_id}`
    : `/projects/${p.id}/overview`
}

// ---------------- 书封配色 ----------------

/** 12 种精选色相（度） */
export const COVER_HUES = [0, 18, 32, 45, 95, 140, 168, 190, 210, 232, 265, 300]

/** FNV-1a 32 位哈希（按 UTF-16 码元），同一书名恒得同一值 */
export function hashString(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

function hslToHex(h: number, s: number, l: number): string {
  const sat = s / 100
  const lig = l / 100
  const k = (n: number) => (n + h / 30) % 12
  const a = sat * Math.min(lig, 1 - lig)
  const f = (n: number) => lig - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
  const hex = (x: number) =>
    Math.round(x * 255)
      .toString(16)
      .padStart(2, '0')
  return `#${hex(f(0))}${hex(f(8))}${hex(f(4))}`
}

function luminance(hex: string): number {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  if (!m) return 0
  const ch = [m[1], m[2], m[3]].map((x) => {
    const c = parseInt(x, 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
}

/** WCAG 对比度（1–21） */
export function contrastRatio(a: string, b: string): number {
  const la = luminance(a)
  const lb = luminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

export interface CoverColors {
  bg: string
  /** 书脊（更深） */
  spine: string
  text: string
}

/**
 * 书名哈希 → 12 色相之一；浅色主题明度高些、暗色主题更深。文字固定为浅色，
 * 对比度不足 4.5:1 时逐步压暗底色（黄绿等高亮色相会被压暗），保证对比度 ≥ 4.5。
 */
export function coverColor(title: string, resolved: ResolvedTheme): CoverColors {
  const hue = COVER_HUES[hashString(title.trim()) % COVER_HUES.length]
  const sat = resolved === 'dark' ? 38 : 48
  let light = resolved === 'dark' ? 30 : 40
  let bg = hslToHex(hue, sat, light)
  while (contrastRatio(bg, COVER_TEXT.onDark) < 4.5 && light > 10) {
    light -= 2
    bg = hslToHex(hue, sat, light)
  }
  return { bg, spine: hslToHex(hue, sat, Math.max(light - 10, 5)), text: COVER_TEXT.onDark }
}
