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

/** 笔记栏的选中项：key 决定编辑器实例，新建保存后只补 id、不重建编辑器。 */
export interface SideNoteSelection { key: string; id: number | null }

/** 切换笔记栏目标：同一篇不动作；当前有未保存修改时必须先确认。 */
export function sideNoteSwitch(current: SideNoteSelection | null, target: number | 'new' | null, dirty: boolean): 'same' | 'confirm' | 'switch' {
  if (target !== 'new' && target !== null && current?.id === target) return 'same'
  if (target === null && current === null) return 'same'
  return dirty ? 'confirm' : 'switch'
}

/** 标记写入只取服务端返回的两个标记，保留编辑器自身的修订号与已保存正文，避免掩盖修订冲突。 */
export function mergeNoteFlags<T extends Pick<ProjectNote, 'is_favorite' | 'is_pinned'>>(
  saved: T, updated: Pick<ProjectNote, 'is_favorite' | 'is_pinned'>,
): T {
  return { ...saved, is_favorite: updated.is_favorite, is_pinned: updated.is_pinned }
}

export function noteListEmptyText(query: string, favoriteOnly: boolean): string {
  if (query) return favoriteOnly ? '没有找到相关的收藏笔记' : '没有找到相关笔记'
  return favoriteOnly ? '没有收藏的笔记' : '还没有笔记，点击「新建笔记」开始记录'
}

interface LeaveLocation { pathname: string; search: string }

/**
 * 章节编辑页离开拦截：正文类草稿在换页或换场景时拦截；
 * 笔记栏草稿只在离开页面时拦截（切场景不卸载笔记栏）。
 */
export function chapterLeaveBlocked(pageGuard: boolean, noteGuard: boolean, current: LeaveLocation, next: LeaveLocation): boolean {
  const pathChanged = current.pathname !== next.pathname
  const sceneChanged = new URLSearchParams(current.search).get('scene_id') !== new URLSearchParams(next.search).get('scene_id')
  return (pageGuard && (pathChanged || sceneChanged)) || (noteGuard && pathChanged)
}
