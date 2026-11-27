import type { ChapterBrief } from '../../api/types'

type ChapterOrder = Pick<ChapterBrief, 'id' | 'chapter_no'>
function chapterOrder(chapters: readonly ChapterOrder[]) {
  return [...chapters].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
}

export function orderedChapterIds(chapters: readonly ChapterOrder[], selected: readonly number[]) {
  const ids = new Set(selected)
  return chapterOrder(chapters)
    .filter((chapter) => ids.has(chapter.id))
    .map((chapter) => chapter.id)
}

export function chapterSelectionError(chapters: readonly ChapterOrder[], selected: readonly number[]): string | null {
  if (selected.length > 20) return '一集最多选择 20 章'
  if (!selected.length) return null
  const ids = new Set(selected)
  const indexes = chapterOrder(chapters).flatMap((chapter, i) => (ids.has(chapter.id) ? [i] : []))
  if (indexes.length !== selected.length) return '所选章节已变化，请重新选择'
  return indexes.at(-1)! - indexes[0] + 1 === indexes.length ? null : '请选择连续的章节'
}
