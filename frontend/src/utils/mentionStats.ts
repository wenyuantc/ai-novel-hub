import type {
  MentionCategory,
  MentionChapter,
  MentionChapterCount,
  MentionEntity,
  MentionSummaryEntry,
  MentionSummaryQuery,
} from '../api/mentions'

export const MENTION_CHAPTER_WINDOW = 50
export const MENTION_ENTITY_PAGE_SIZE = 20

export interface MentionStatsFilter {
  category?: MentionCategory
  entity?: Pick<MentionEntity, 'kind' | 'id'>
  q?: string
  chapterFrom?: number
  chapterTo?: number
}

export interface MentionChapterRow extends MentionChapterCount {
  key: string
  entity: MentionEntity
  chapter: MentionChapter
}

export function mentionEntityKey(entity: Pick<MentionEntity, 'kind' | 'id'>): string {
  return `${entity.kind}:${entity.id}`
}

/** 稀疏计数按章节身份展开；未写章不伪造成已写零值。 */
export function mentionChapterRows(entries: MentionSummaryEntry[], chapters: MentionChapter[]): MentionChapterRow[] {
  return entries.flatMap((entry) => {
    const counts = new Map(entry.counts.map((count) => [count.chapter_id, count]))
    return chapters.map((chapter) => ({
      key: `${mentionEntityKey(entry.entity)}:${chapter.id}`,
      entity: entry.entity,
      chapter,
      chapter_id: chapter.id,
      count: counts.get(chapter.id)?.count ?? 0,
      ambiguous_count: counts.get(chapter.id)?.ambiguous_count ?? 0,
    }))
  })
}

/** 章节窗口只取真实存在的章，不把章号间隙补成空章。 */
export function chapterWindow(chapters: MentionChapter[], requestedPage: number) {
  const pages = Math.max(1, Math.ceil(chapters.length / MENTION_CHAPTER_WINDOW))
  const page = Math.max(1, Math.min(pages, Math.floor(requestedPage)))
  return {
    items: chapters.slice((page - 1) * MENTION_CHAPTER_WINDOW, page * MENTION_CHAPTER_WINDOW),
    page,
    pages,
    total: chapters.length,
  }
}

export function mentionTimeline(rows: MentionChapterRow[]): MentionChapterRow[] {
  return rows.filter((row) => row.chapter.has_content && (row.count > 0 || row.ambiguous_count > 0))
}

export function mentionCellLabel(row: MentionChapterRow): string {
  const position = `${row.entity.name}，第 ${row.chapter.chapter_no} 章 ${row.chapter.title}`
  return row.chapter.has_content
    ? `${position}：${row.count} 次确定提及，${row.ambiguous_count} 次有歧义`
    : `${position}：未写，不计入未提及间隔`
}

export function mentionRangeError(from?: number, to?: number): string {
  if ([from, to].some((value) => value !== undefined && (!Number.isSafeInteger(value) || value < 1))) {
    return '章节号须为大于 0 的整数'
  }
  return from !== undefined && to !== undefined && from > to ? '起始章不能大于结束章' : ''
}

export function mentionScopeLabel(from?: number, to?: number): string {
  if (from !== undefined && to !== undefined) return `第 ${from}–${to} 章的已保存正文`
  if (from !== undefined) return `从第 ${from} 章起的已保存正文`
  if (to !== undefined) return `截至第 ${to} 章的已保存正文`
  return '全书已保存正文'
}

export function mentionSummaryQuery(filter: MentionStatsFilter, page: number): MentionSummaryQuery {
  return {
    category: filter.category,
    entity_kind: filter.entity?.kind,
    entity_id: filter.entity?.id,
    q: filter.q?.trim() || undefined,
    chapter_from: filter.chapterFrom,
    chapter_to: filter.chapterTo,
    page,
    page_size: MENTION_ENTITY_PAGE_SIZE,
  }
}
