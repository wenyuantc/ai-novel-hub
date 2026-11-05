import type { ChapterBrief, ProgressionPositionInput } from '../../api/types'

export type ChapterReference = Pick<ChapterBrief, 'id' | 'chapter_no' | 'title'>

interface AnchorSummary {
  anchor_kind: ProgressionPositionInput['anchor_kind']
  chapter_id: number | null
  chapter_no: number | null
  resolved_chapter_no: number | null
  anchor_label: string
  orphaned: boolean
}

const MAX_CHAPTER_NO = 2_147_483_647

function positiveInteger(value: number | null): value is number {
  return value !== null && Number.isSafeInteger(value) && value > 0 && value <= MAX_CHAPTER_NO
}

export function chapterOptions(chapters: readonly ChapterReference[]) {
  return [...chapters]
    .sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
    .map((chapter) => ({
      value: chapter.id,
      label: `第 ${chapter.chapter_no} 章 · ${chapter.title || '未命名'}`,
    }))
}

/** 返回规范化后的请求位置；无效值不进入 API。 */
export function toPositionInput(draft: ProgressionPositionInput): ProgressionPositionInput | null {
  if (draft.anchor_kind === 'chapter') {
    return positiveInteger(draft.chapter_id)
      ? { anchor_kind: 'chapter', chapter_id: draft.chapter_id, chapter_no: null, boundary: draft.boundary }
      : null
  }
  return positiveInteger(draft.chapter_no)
    ? { anchor_kind: 'planned', chapter_id: null, chapter_no: draft.chapter_no, boundary: draft.boundary }
    : null
}

export function describeAnchor(anchor: AnchorSummary, chapters: readonly ChapterReference[]): string {
  if (anchor.anchor_kind === 'planned') {
    return anchor.chapter_no === null ? '未设置计划章节' : `第 ${anchor.chapter_no} 章（计划章节）`
  }
  if (anchor.orphaned || anchor.chapter_id === null) {
    return anchor.anchor_label ? `${anchor.anchor_label}（关联章节已删除）` : '关联章节已删除'
  }
  const chapter = chapters.find((item) => item.id === anchor.chapter_id)
  if (chapter) return `第 ${chapter.chapter_no} 章 · ${chapter.title || '未命名'}`
  if (anchor.resolved_chapter_no !== null) return `第 ${anchor.resolved_chapter_no} 章`
  return anchor.anchor_label || '已关联章节'
}
