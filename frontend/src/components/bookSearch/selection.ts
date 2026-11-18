import type { ApplyRevision, SearchHit, SearchScope, SelectedHit } from '../../api/bookSearch'
import { ApiError, errorMessage } from '../../api/client'

export const MAX_SELECTED = 1000
export const hitKey = (hit: Pick<SearchHit, 'chapter_id' | 'start'>) => `${hit.chapter_id}:${hit.start}`
export function selectable(hit: SearchHit): hit is SearchHit & { scene_id: number; token: string } {
  return hit.scene_id !== null && !!hit.token && !hit.unavailable_reason
}

export function changeSelection(previous: ReadonlyMap<string, SelectedHit>, items: SearchHit[], checked: boolean) {
  const next = new Map(previous)
  for (const hit of items) {
    if (!selectable(hit)) continue
    const { chapter_id, chapter_no, chapter_title, scene_id, revision, start, end, token } = hit
    if (checked) next.set(hitKey(hit), { chapter_id, chapter_no, chapter_title, scene_id, revision, start, end, token })
    else next.delete(hitKey(hit))
  }
  return next
}

export interface SearchMode extends SearchScope { replacement?: string }
export function asApplication(mode: SearchMode, selected: ReadonlyMap<string, SelectedHit>, requestId: string): ApplyRevision {
  if (mode.replacement === undefined || mode.query === mode.replacement || selected.size === 0 || selected.size > MAX_SELECTED) {
    throw new Error('请在替换预览中勾选有效命中')
  }
  return { ...mode, replacement: mode.replacement, request_id: requestId, hits: [...selected.values()] }
}

export interface OperationError { text: string; conflicts: string[]; conflict: boolean; unknown: boolean }
export function describeError(error: unknown): OperationError {
  const conflict = error instanceof ApiError && error.status === 409
  const details = conflict ? error.body?.conflicts : null
  const conflicts: string[] = []
  if (Array.isArray(details)) {
    for (const item of details) {
      if (!item || typeof item.chapter_no !== 'number') continue
      const title = typeof item.chapter_title === 'string' ? item.chapter_title : ''
      const reason = typeof item.reason === 'string' ? item.reason : ''
      conflicts.push([`第 ${item.chapter_no} 章`, title, reason].filter(Boolean).join(' · '))
    }
  }
  return { text: errorMessage(error), conflicts, conflict,
    unknown: !(error instanceof ApiError) || error.status === 0 || error.status >= 500 }
}
