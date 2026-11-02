import type { BackupArtifactStatus, BackupCleanupStatus, BackupNotificationStatus, BackupRunOut, BackupRunStatus } from '../../api/types'
import { parseServerTime } from '../../utils/time'

export const BACKUP_RUN_STATUS: Record<BackupRunStatus, { label: string; color: string }> = {
  queued: { label: '已受理', color: 'processing' }, running: { label: '执行中', color: 'processing' },
  succeeded: { label: '全部成功', color: 'success' }, partial: { label: '部分成功', color: 'warning' },
  failed: { label: '失败', color: 'error' }, interrupted: { label: '已中断', color: 'warning' },
}
export const BACKUP_ARTIFACT_STATUS: Record<BackupArtifactStatus, string> = {
  pending: '等待保存', ready: '副本可用', failed: '保存失败', unknown: '结果未知', deleting: '清理中', deleted: '已清理', missing: '文件缺失',
}
export const BACKUP_MAIL_STATUS: Record<BackupNotificationStatus, string> = {
  not_configured: '未配置通知', pending: '等待通知', sending: '正在投递', sent: 'SMTP 已受理', failed: '投递失败', unknown: '投递结果未知',
}
/** 成功且无需提醒时没有投递动作，不能将未触发的通知说成SMTP未配置。 */
export function backupMailStatus(run: Pick<BackupRunOut, 'status' | 'cleanup_status' | 'notification_status'>): string {
  if (run.notification_status === 'not_configured' && run.status === 'succeeded'
    && (run.cleanup_status === 'not_required' || run.cleanup_status === 'succeeded')) return '无需失败提醒'
  return BACKUP_MAIL_STATUS[run.notification_status]
}
export const BACKUP_CLEANUP_STATUS: Record<BackupCleanupStatus, string> = {
  not_required: '无需清理', pending: '等待清理', succeeded: '清理完成', failed: '清理失败', unknown: '清理结果未知',
}

const scheduleFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

/** 每日计划使用北京时间；不能随浏览器所在时区改变计划日期与时刻。 */
export function formatBackupSchedule(value: string | null | undefined): string {
  if (!value) return '-'
  const date = parseServerTime(value)
  if (!date.isValid()) return value
  const parts = Object.fromEntries(scheduleFormatter.formatToParts(date.toDate()).map(({ type, value }) => [type, value]))
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}（北京时间）`
}

export function backupSize(bytes: number | null): string {
  if (bytes === null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`
}
