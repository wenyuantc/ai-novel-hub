import type { MarkerTimelineItem } from '../../api/richDocument'

export interface MarkerLocation { chapterId: number; sceneId: number; revision: number; start: number; end: number; sectionKey?: string }

export function markerLocation(item: MarkerTimelineItem): MarkerLocation | null {
  if (item.status !== 'active' || !item.scene_active || item.start === null || item.end === null) return null
  return { chapterId: item.chapter_id, sceneId: item.scene_id, revision: item.content_revision, start: item.start, end: item.end,
    ...(item.section_key === null ? {} : { sectionKey: item.section_key }) }
}

/** 路由 state 不是可信数据；位置仍须在目标章校验 revision 与正文边界。 */
export function readMarkerLocation(state: unknown, chapterId: number): MarkerLocation | null {
  if (!state || typeof state !== 'object' || !('markerLocation' in state)) return null
  const value = state.markerLocation
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const { sceneId, revision, start, end, sectionKey } = raw
  if (raw.chapterId !== chapterId || !Number.isSafeInteger(sceneId) || !Number.isSafeInteger(revision)
    || !Number.isSafeInteger(start) || !Number.isSafeInteger(end)
    || (sceneId as number) < 1 || (revision as number) < 0 || (start as number) < 0 || (end as number) <= (start as number)
    || sectionKey !== undefined && (typeof sectionKey !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(sectionKey))) return null
  return { chapterId, sceneId: sceneId as number, revision: revision as number, start: start as number, end: end as number,
    ...(sectionKey === undefined ? {} : { sectionKey: sectionKey as string }) }
}
