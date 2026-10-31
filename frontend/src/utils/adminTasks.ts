import { TASK_KIND_LABELS, TASK_STATUS } from '../api/tasks'
import type { AdminTaskBrief, AdminTaskError, AdminTaskListQuery, AdminTaskSort, AdminTaskStatus, TaskKind, TaskStatus } from '../api/types'
import { auditDateRange } from './auditLogs'

export const ADMIN_TASK_SORT: Record<AdminTaskSort, string> = {
  created_desc: '创建时间倒序', queue_longest: '本轮排队最久优先', running_longest: '本轮运行最久优先',
}
export const ADMIN_TASK_ERROR: Record<AdminTaskError, string> = {
  execution_failed: '执行失败', cancelled: '任务已取消', interrupted: '服务重启，任务已中断', cleanup_pending: '取消收尾尚未完成',
}
export const ADMIN_TASK_LIFECYCLE: Record<AdminTaskBrief['project_lifecycle'], string> = {
  active: '正常作品', deleting: '删除协调中', recycled: '回收站', none: '无关联作品',
}
export const ADMIN_TASK_STATUSES: AdminTaskStatus[] = [...Object.keys(TASK_STATUS) as TaskStatus[], 'unknown']
export const adminTaskStatus = (status: AdminTaskStatus) => Object.hasOwn(TASK_STATUS, status) ? TASK_STATUS[status as TaskStatus] : { label: '未知状态', color: 'default' }
export const adminTaskKind = (kind: AdminTaskBrief['kind']) => Object.hasOwn(TASK_KIND_LABELS, kind) ? TASK_KIND_LABELS[kind as TaskKind] : '其他任务'
export const adminTaskTitle = (task: Pick<AdminTaskBrief, 'kind' | 'id'>) => `${adminTaskKind(task.kind)} #${task.id}`
export const adminTaskLink = (taskKey: string) => `/admin/tasks?task_key=${encodeURIComponent(taskKey)}`

/** 不使用原始错误文案；未来未知类别同样固定回退。 */
export function adminTaskError(task: Pick<AdminTaskBrief, 'error_code'>): string | null {
  return task.error_code === null ? null : Object.hasOwn(ADMIN_TASK_ERROR, task.error_code) ? ADMIN_TASK_ERROR[task.error_code] : '执行失败'
}

export function adminTaskDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '未记录'
  const n = Math.floor(seconds)
  if (n < 60) return `${n} 秒`
  if (n < 3600) return `${Math.floor(n / 60)} 分 ${n % 60} 秒`
  if (n < 86400) return `${Math.floor(n / 3600)} 小时 ${Math.floor(n % 3600 / 60)} 分`
  return `${Math.floor(n / 86400)} 天 ${Math.floor(n % 86400 / 3600)} 小时`
}

export function adminTaskCurrentDuration(task: Pick<AdminTaskBrief, 'status' | 'queued_seconds' | 'running_seconds'>): string {
  if (task.status === 'queued') return `本轮排队：${adminTaskDuration(task.queued_seconds)}`
  if (task.status === 'running') return `本轮运行：${adminTaskDuration(task.running_seconds)}`
  return task.status === 'paused' ? '审核等待，不占执行并发' : '—'
}

export interface AdminTaskFilterValues extends Omit<AdminTaskListQuery, 'from_at' | 'to_at' | 'page' | 'page_size'> {
  fromDate?: string
  toDate?: string
}
export function adminTaskFilterQuery({ fromDate, toDate, q, ...values }: AdminTaskFilterValues): AdminTaskListQuery {
  const dates = fromDate || toDate ? auditDateRange(fromDate ?? '', toDate ?? '') : {}
  return { ...values, q: q?.trim() || undefined, ...dates, page: 1 }
}

/** URL 仅接受已知状态、类型、正整数和 UUID；错误深链接不退回整数任务定位。 */
export function adminTaskInitialQuery(params: URLSearchParams): AdminTaskListQuery {
  const positive = (key: string) => {
    const raw = params.get(key)
    const value = Number(raw)
    return raw && /^\d+$/.test(raw) && Number.isSafeInteger(value) && value > 0 ? value : undefined
  }
  const kind = params.get('kind')
  const sort = params.get('sort')
  const statuses = params.getAll('status').filter((status): status is TaskStatus => Object.hasOwn(TASK_STATUS, status))
  return {
    q: params.get('q')?.slice(0, 200) || undefined,
    owner_user_id: positive('owner_user_id'), project_id: positive('project_id'), task_id: positive('task_id'),
    kind: kind && Object.hasOwn(TASK_KIND_LABELS, kind) ? kind as TaskKind : undefined,
    status: [...new Set(statuses)], sort: sort && Object.hasOwn(ADMIN_TASK_SORT, sort) ? sort as AdminTaskSort : 'created_desc',
    page: 1, page_size: 20,
  }
}
export const isAdminTaskKey = (value: string | null): value is string => !!value && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)

/** 与后端 Python len 一致按码点计数，不接受控制/格式字符。 */
export function adminTaskCancelReason(value: string): string {
  const reason = value.trim()
  if (!reason || Array.from(reason).length > 200 || /\p{C}/u.test(reason)) throw new Error('取消原因须为1–200字纯文本，不能包含控制字符')
  return reason
}

export function adminTaskCancellationOutcome(task: Pick<AdminTaskBrief, 'status' | 'cancellation_pending'>): string | null {
  if (task.cancellation_pending) return '取消请求已受理，正在安全收尾；尚未确认任务已取消。'
  switch (task.status) {
    case 'cancelled': return '任务已取消；已保存内容和已执行效果保留。'
    case 'succeeded': return '任务已完成，完成保存先于取消收尾；已提交内容未撤销。'
    case 'paused': return '任务实际进入待审核，未被取消；待审核不占执行并发。'
    case 'interrupted': return '任务实际已中断，未报告为已取消。'
    case 'failed': return '任务实际执行失败，未报告为已取消。'
    default: return null
  }
}
