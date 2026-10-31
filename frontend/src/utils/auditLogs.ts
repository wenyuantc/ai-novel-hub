import dayjs from 'dayjs'
import type { AuditCleanupPreview, AuditDateRange, AuditLogListQuery, AuditSafeState, AuditSummary } from '../api/types'
import { AUDIT_FIELD, AUDIT_STATE, AUDIT_TARGET } from '../api/auditLogs'
import { formatTime, parseServerTime } from './time'

export interface AuditFilterValues extends Omit<AuditLogListQuery, 'from_at' | 'to_at' | 'page' | 'page_size'> {
  fromDate?: string
  toDate?: string
}

/** 用户选的是本地日历日期：包含结束日，发送 UTC 半开区间；不用固定 24 小时推算次日。 */
export function auditDateRange(fromDate: string, toDate: string): AuditDateRange {
  const from = dayjs(fromDate).startOf('day')
  const to = dayjs(toDate).startOf('day')
  if (!/^\d{4}-\d{2}-\d{2}$/.test(fromDate) || !/^\d{4}-\d{2}-\d{2}$/.test(toDate)
    || !from.isValid() || !to.isValid() || from.format('YYYY-MM-DD') !== fromDate || to.format('YYYY-MM-DD') !== toDate) {
    throw new Error('请选择完整、有效的起止日期')
  }
  if (from.isAfter(to)) throw new Error('开始日期不能晚于结束日期')
  return { from_at: from.toISOString(), to_at: to.add(1, 'day').startOf('day').toISOString() }
}

export function auditFilterQuery({ fromDate, toDate, actor_q, ...values }: AuditFilterValues): AuditLogListQuery {
  const dates = fromDate || toDate ? auditDateRange(fromDate ?? '', toDate ?? '') : {}
  return { ...values, actor_q: actor_q?.trim() || undefined, ...dates }
}

export function auditRangeLabel(range: AuditDateRange): string {
  return `${formatTime(range.from_at, 'YYYY-MM-DD HH:mm [UTC]Z')}（含）至 ${formatTime(range.to_at, 'YYYY-MM-DD HH:mm [UTC]Z')}（不含）`
}

export function auditTargetLabel(log: { target_type: keyof typeof AUDIT_TARGET; target_id: number | null; target_label: string | null }): string {
  return `${AUDIT_TARGET[log.target_type]}${log.target_id === null ? '' : ` #${log.target_id}`}${log.target_label ? ` · ${log.target_label}` : ''}`
}

/** 系统清理没有用户 ID；不能展示 #null 或把它伪装成历史管理员。 */
export function auditActorLabel(log: { actor_username: string; actor_user_id: number | null }): string {
  return log.actor_user_id === null ? `${log.actor_username}（系统）` : `${log.actor_username} · #${log.actor_user_id}`
}

/** 清理后当前页失效时回退最后有效页，保留筛选和每页条数。 */
export function auditValidPage(page: number, total: number, pageSize: number): number {
  return Math.max(1, Math.min(page, Math.ceil(total / pageSize)))
}

export function cleanupPreviewUsable(preview: AuditCleanupPreview | null, range: AuditDateRange | null, now = Date.now()): boolean {
  return !!preview && !!range && preview.count > 0 && !!preview.preview_token
    && parseServerTime(preview.expires_at).valueOf() > now
    && parseServerTime(preview.from_at).valueOf() === parseServerTime(range.from_at).valueOf()
    && parseServerTime(preview.to_at).valueOf() === parseServerTime(range.to_at).valueOf()
}

/** 只用于本页异步交互；改日期、关闭、卸载会撤销旧请求，响应不得恢复旧令牌或正文。 */
export function createAuditRequestGate() {
  let generation = 0
  return {
    begin: () => ++generation,
    invalidate: () => { generation += 1 },
    isCurrent: (ticket: number) => generation === ticket,
  }
}

export interface AuditSummaryRow { label: string; value: string }

/** 审计摘要的唯一展示投影，不把摘要误称为全文差异，也不读取任意 JSON 字段。 */
export function auditSummaryRows(summary: AuditSummary): AuditSummaryRow[] {
  const rows: AuditSummaryRow[] = [{ label: '变更字段', value: summary.changed_fields.map((field) => AUDIT_FIELD[field]).join('、') || '无字段变更 / 不适用' }]
  const lifecycleLabels = { soft_delete: '移入回收站', restore: '恢复作品', purge: '手动彻底删除', expire: '到期自动清理' } as const
  if (summary.lifecycle_action) rows.push({ label: '作品生命周期动作', value: lifecycleLabels[summary.lifecycle_action] })
  if (summary.retention_days != null) rows.push({ label: '保留期限', value: `${summary.retention_days} 天` })
  if (summary.cancelled_task_count != null) rows.push({ label: '取消任务数', value: String(summary.cancelled_task_count) })
  const scalar = (value: boolean | number | AuditSafeState | null) => value === null ? '无' : typeof value === 'boolean' ? (value ? '是' : '否') : typeof value === 'string' ? AUDIT_STATE[value] : String(value)
  for (const change of summary.changes) rows.push({ label: AUDIT_FIELD[change.field], value: `${scalar(change.before)} → ${scalar(change.after)}` })
  for (const [label, before, after] of [
    ['正文字数', summary.word_count_before, summary.word_count_after],
    ['资源正文字符数', summary.body_chars_before, summary.body_chars_after],
    ['参考资料数', summary.reference_count_before, summary.reference_count_after],
  ] as const) {
    if (before !== null || after !== null) rows.push({ label, value: `${before ?? '—'} → ${after ?? '—'}` })
  }
  for (const [label, value] of [
    ['受影响数量', summary.affected_count], ['章节数', summary.chapter_count], ['版本数', summary.version_count],
    ['预计删除数', summary.expected_count], ['实际删除数', summary.deleted_count], ['预览高水位 ID', summary.high_water_id],
  ] as const) {
    if (value !== null) rows.push({ label, value: String(value) })
  }
  if (summary.affected_ids.length || summary.ids_truncated) rows.push({ label: '受影响 ID', value: `${summary.affected_ids.join('、')}${summary.ids_truncated ? '（仅展示有界列表，其余省略）' : ''}` })
  if (summary.from_at && summary.to_at) rows.push({ label: '清理范围', value: auditRangeLabel({ from_at: summary.from_at, to_at: summary.to_at }) })
  return rows
}
