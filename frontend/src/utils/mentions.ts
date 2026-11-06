import type { MentionAnchor, MentionEntity, MentionSource, MentionSpan } from '../api/mentions'
import type { ProjectResource } from './projectRefresh'

export const MENTION_DICTIONARY_RESOURCES = ['setting-ai-visibility', 'characters', 'world', 'organizations', 'careers'] as const satisfies readonly ProjectResource[]

export function readPositiveId(value: string | null): number | null {
  if (!value || !/^\d+$/.test(value)) return null
  const result = Number(value)
  return Number.isSafeInteger(result) && result > 0 ? result : null
}
/** 尚未成功加载不能当成空列表，否则网络失败会吞掉深链目标。 */
export function resolveEntityDeepLink<T extends { id: number; project_id: number }>(
  projectId: number, items: T[] | undefined, loading: boolean, value: string | null,
): { status: 'waiting' } | { status: 'missing' } | { status: 'found'; item: T } {
  if (value === null || loading || items === undefined) return { status: 'waiting' }
  const id = readPositiveId(value)
  const item = items.find((entry) => entry.id === id && entry.project_id === projectId)
  return item ? { status: 'found', item } : { status: 'missing' }
}
export function readMentionAnchor(params: URLSearchParams): MentionAnchor | null {
  const start = params.get('mention_start'), end = params.get('mention_end'), hash = params.get('mention_hash')
  if (start === null || end === null || !/^\d+$/.test(start) || !/^\d+$/.test(end) || !hash || !/^[a-f0-9]{64}$/.test(hash)) return null
  const s = Number(start), e = Number(end)
  return Number.isSafeInteger(s) && Number.isSafeInteger(e) && e > s ? { start: s, end: e, content_hash: hash } : null
}
export function mentionHref(pid: number, source: MentionSource, anchor: MentionAnchor): string {
  const params = new URLSearchParams({ mention_start: String(anchor.start), mention_end: String(anchor.end), mention_hash: anchor.content_hash })
  let path: string
  if (source.type === 'chapter') path = `read/${source.id}`
  else if (source.type === 'chat') { path = 'overview'; params.set('agent', 'open'); params.set('conversation_id', String(source.conversation_id)); params.set('message_id', String(source.id)) }
  else if (source.type === 'note') { path = 'notes'; params.set('id', String(source.id)) }
  else { path = 'knowledge'; params.set('document_id', String(source.id)) }
  return `/projects/${pid}/${path}?${params}`
}
export interface MentionSegment { start: number; end: number; text: string; entities: MentionEntity[]; focused: boolean }
/** 原文码点范围转为 React 文本段；保留空白，忽略落在窗口外或重叠的错误范围。 */
export function mentionSegments(text: string, spans: MentionSpan[], offset = 0, focus?: { start: number; end: number } | null): MentionSegment[] {
  const points = Array.from(text), end = offset + points.length
  const ranges = spans.filter((s) => s.start >= offset && s.end <= end && s.end > s.start).sort((a, b) => a.start - b.start || b.end - a.end)
  if (focus && focus.start >= offset && focus.end <= end && !ranges.some((s) => s.start < focus.end && s.end > focus.start)) ranges.push({ ...focus, text: '', entities: [] })
  ranges.sort((a, b) => a.start - b.start)
  const result: MentionSegment[] = []
  const add = (start: number, stop: number, entities: MentionEntity[] = []) => {
    if (stop <= start) return
    result.push({ start, end: stop, text: points.slice(start - offset, stop - offset).join(''), entities, focused: !!focus && start < focus.end && stop > focus.start })
  }
  let cursor = offset
  for (const span of ranges) { if (span.start < cursor) continue; add(cursor, span.start); add(span.start, span.end, span.entities); cursor = span.end }
  add(cursor, end)
  return result
}
