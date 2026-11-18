import type { SerializationPublishRequest } from '../../api/serialization'
import { createIntentKey } from '../../utils/novelTemplateCreation'
import { parseServerTime } from '../../utils/time'

export function manuscriptTitle(manuscript: { chapter_no: number; title: string }) {
  return `第 ${manuscript.chapter_no} 章 · ${manuscript.title || '无标题'}`
}

const beijingParts = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

function beijingTimestamp(date: Date): string {
  const parts = Object.fromEntries(beijingParts.formatToParts(date).map(({ type, value: part }) => [type, part]))
  return `${parts.year.padStart(4, '0')}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`
}

export function beijingMinute(value: string | Date = new Date()): string {
  const date = typeof value === 'string' ? parseServerTime(value).toDate() : value
  return Number.isFinite(date.getTime()) ? beijingTimestamp(date).slice(0, 16) : ''
}

export function publicationRequest(
  previous: SerializationPublishRequest | null, revision: number, editionId: number,
  channel: string, localTime: string, now = Date.now(),
): SerializationPublishRequest {
  const normalized = channel.trim()
  if (!normalized) throw new Error('请输入发布平台')
  if (Array.from(normalized).length > 100) throw new Error('发布平台名称最多 100 个字符')
  const probe = new Date(`${localTime}:00+08:00`)
  if (!Number.isFinite(probe.getTime())) throw new Error('请输入有效的北京日期和时间')
  // 用该时刻的上海民用时间校正历史偏移，回读可排除夏令时跳过的时段。
  const correction = Date.parse(`${localTime}:00Z`) - Date.parse(`${beijingTimestamp(probe)}Z`)
  const date = new Date(probe.getTime() + correction)
  if (beijingMinute(date) !== localTime)
    throw new Error('请输入有效的北京日期和时间')
  if (date.getTime() > now) throw new Error('实际发布时间不能晚于现在，请为未来的发布设置排期')
  const published = date.toISOString()
  if (previous && previous.expected_revision === revision && previous.edition_id === editionId
      && previous.channel === normalized && previous.published_at === published) return previous
  return { edition_id: editionId, expected_revision: revision, channel: normalized, published_at: published, request_id: createIntentKey() }
}
