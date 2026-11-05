import type { NoteInput, ProjectNote } from '../../api/notes'
import { codePointLength } from '../../utils/textOffset'

/** 不把损坏的深链退化为任意一篇笔记。 */
export function noteId(value: string | null): number | null {
  if (!value || !/^[1-9]\d*$/.test(value)) return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) ? parsed : null
}

export function noteDraftError(draft: NoteInput): string | null {
  if (!draft.title.trim()) return '请输入笔记标题'
  if (codePointLength(draft.title.trim()) > 200) return '标题最多 200 字'
  if (codePointLength(draft.content) > 200_000) return '笔记正文最多 200000 字'
  return null
}

export function noteDirty(draft: NoteInput, saved: Pick<ProjectNote, 'title' | 'content'> | null): boolean {
  return saved ? draft.title !== saved.title || draft.content !== saved.content : !!(draft.title || draft.content)
}

/** 只清理笔记及原文定位参数，保留智能体等其他页面状态。 */
export function noteSearch(search: string, target: number | 'new' | null): string {
  const params = new URLSearchParams(search)
  for (const key of ['id', 'new', 'mention_start', 'mention_end', 'mention_hash']) params.delete(key)
  if (target === 'new') params.set('new', '1')
  else if (target !== null) params.set('id', String(target))
  return params.toString()
}

/** 同笔记更换提及定位不卸载草稿，换笔记或离开页面才触发保护。 */
export function noteTargetKey(pathname: string, search: string): string {
  const params = new URLSearchParams(search)
  return `${pathname}?id=${params.get('id') ?? ''}&new=${params.get('new') ?? ''}`
}
