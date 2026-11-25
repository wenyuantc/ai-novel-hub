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
  if (!Number.isSafeInteger(s) || !Number.isSafeInteger(e) || e <= s) return null
  const origin = params.get(MENTION_FROM_PARAM)
  const anchor = { start: s, end: e, content_hash: hash }
  return origin === 'consistency' || origin === 'book-search' ? { ...anchor, origin } : anchor
}
/** 复用同一组章内坐标，依来源选择失效反馈。 */
export const MENTION_FROM_PARAM = 'mention_from'
export function staleAnchorMessage(anchor: Pick<MentionAnchor, 'origin'> | null | undefined, fallback: string): string {
  if (anchor?.origin === 'book-search') return '搜索结果的位置已失效，请重新搜索当前正文。'
  return anchor?.origin === 'consistency' ? '原文已变化，旧位置已失效，请回到来源页面重新定位。' : fallback
}
function anchorParams(anchor: MentionAnchor): URLSearchParams {
  const params = new URLSearchParams({ mention_start: String(anchor.start), mention_end: String(anchor.end), mention_hash: anchor.content_hash })
  if (anchor.origin) params.set(MENTION_FROM_PARAM, anchor.origin)
  return params
}
export function bookSearchHref(pid: number, chapterId: number, anchor: Pick<MentionAnchor, 'start' | 'end' | 'content_hash'>): string {
  return `/projects/${pid}/read/${chapterId}?${anchorParams({ ...anchor, origin: 'book-search' })}`
}
export function mentionHref(pid: number, source: MentionSource, anchor: MentionAnchor): string {
  const params = anchorParams(anchor)
  let path: string
  if (source.type === 'chapter') path = `read/${source.id}`
  else if (source.type === 'chat') { path = 'overview'; params.set('agent', 'open'); params.set('conversation_id', String(source.conversation_id)); params.set('message_id', String(source.id)) }
  else if (source.type === 'note') { path = 'notes'; params.set('id', String(source.id)) }
  else { path = 'knowledge'; params.set('document_id', String(source.id)) }
  return `/projects/${pid}/${path}?${params}`
}
export interface MentionSegment { start: number; end: number; text: string; entities: MentionEntity[]; focused: boolean }
/**
 * 原文码点范围转为 React 文本段；保留空白，忽略落在窗口外或重叠的错误范围。
 * 焦点高亮严格等于 focus 与窗口的交集：片段在焦点边界处切开，与焦点部分重叠的提及被切成两段（都保留设定，可点击），
 * 焦点不随提及或其余正文扩大。
 */
export function mentionSegments(text: string, spans: MentionSpan[], offset = 0, focus?: { start: number; end: number } | null): MentionSegment[] {
  const points = Array.from(text), end = offset + points.length
  const ranges = spans.filter((s) => s.start >= offset && s.end <= end && s.end > s.start).sort((a, b) => a.start - b.start || b.end - a.end)
  const target = focus && focus.end > focus.start ? focus : null
  const result: MentionSegment[] = []
  const add = (start: number, stop: number, entities: MentionEntity[] = []) => {
    if (stop <= start) return
    const bounds = [start, ...(target ? [target.start, target.end].filter((cut) => cut > start && cut < stop) : []), stop]
    for (let index = 0; index + 1 < bounds.length; index += 1) {
      const from = bounds[index], to = bounds[index + 1]
      result.push({ start: from, end: to, text: points.slice(from - offset, to - offset).join(''), entities, focused: !!target && from >= target.start && to <= target.end })
    }
  }
  let cursor = offset
  for (const span of ranges) { if (span.start < cursor) continue; add(cursor, span.start); add(span.start, span.end, span.entities); cursor = span.end }
  add(cursor, end)
  return result
}

/** 读者格式与提及均按原文排序，二分定位落在当前码点窗口内的片段。 */
export function firstEndingAfter(items: { end: number }[], start: number): number {
  let lo = 0, hi = items.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (items[mid].end <= start) lo = mid + 1
    else hi = mid
  }
  return lo
}
