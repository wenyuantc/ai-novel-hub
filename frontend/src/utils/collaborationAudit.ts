import {
  COLLAB_AUDIT_ROLE,
  COLLAB_AUDIT_SCOPE,
  COLLAB_AUDIT_STATUS,
  type AdminCollaborationAuditQuery,
  type AuthorCollaborationAuditQuery,
  type CollaborationAuditEvent,
} from '../api/collaborationAudit'
import type { CollaborationRole, MemberStatus } from '../api/collaboration'
import { auditDateRange } from './auditLogs'

export interface AuthorCollaborationAuditFilterValues
  extends Omit<AuthorCollaborationAuditQuery, 'from_at' | 'to_at' | 'page' | 'page_size'> {
  fromDate?: string
  toDate?: string
}

export interface AdminCollaborationAuditFilterValues
  extends Omit<AdminCollaborationAuditQuery, 'from_at' | 'to_at' | 'page' | 'page_size'> {
  fromDate?: string
  toDate?: string
}

export interface CollaborationAuditSummaryRow {
  label: string
  value: string
}

const cleanText = (value?: string) => value?.trim() || undefined

/** 本地日历日期（含结束日）转换为后端 UTC 半开区间。 */
export function authorCollaborationAuditQuery({
  fromDate, toDate, member_q, ...rest
}: AuthorCollaborationAuditFilterValues): AuthorCollaborationAuditQuery {
  const dates = fromDate || toDate ? auditDateRange(fromDate ?? '', toDate ?? '') : {}
  return { ...rest, member_q: cleanText(member_q), ...dates }
}

export function adminCollaborationAuditQuery({
  fromDate, toDate, resource_q, actor_q, target_q, ...rest
}: AdminCollaborationAuditFilterValues): AdminCollaborationAuditQuery {
  const dates = fromDate || toDate ? auditDateRange(fromDate ?? '', toDate ?? '') : {}
  return {
    ...rest,
    resource_q: cleanText(resource_q),
    actor_q: cleanText(actor_q),
    target_q: cleanText(target_q),
    ...dates,
  }
}

/** 格式化用户快照，避免出现 #null；无用户名但有 ID 时展示历史用户标识。 */
export function collaborationUserLabel(
  userId: number | null,
  username: string | null,
  emptyLabel = '未知 / 不适用',
): string {
  if (userId === null && !username) return emptyLabel
  if (userId === null) return username ?? emptyLabel
  return `${username ?? '历史用户'} · #${userId}`
}

/** 作者视角下邀请失败未确认目标时，给出中性脱敏提示而不泄露账号是否存在。 */
export function collaborationTargetLabel(
  event: Pick<CollaborationAuditEvent, 'action' | 'result' | 'error_code' | 'target_user_id' | 'target_username'>,
  options: { ownerView?: boolean } = {},
): string {
  if (event.target_user_id === null && !event.target_username) {
    if (options.ownerView && event.action === 'invite' && event.result === 'failure') {
      return '未公开 / 不可用'
    }
    return '未知 / 不适用'
  }
  return collaborationUserLabel(event.target_user_id, event.target_username)
}

export function collaborationResourceLabel(
  event: Pick<CollaborationAuditEvent, 'scope' | 'resource_id' | 'resource_label'>,
): string {
  const scopeLabel = COLLAB_AUDIT_SCOPE[event.scope]
  if (event.resource_id === 0) return `${scopeLabel} · 未知资源（无效或非本人邀请） · #0`
  return `${scopeLabel} #${event.resource_id}${event.resource_label ? ` · ${event.resource_label}` : ''}`
}

const roleText = (scope: CollaborationAuditEvent['scope'], role: CollaborationRole | null) => {
  if (role === null) return '无'
  return scope === 'series' ? '设定协作者' : COLLAB_AUDIT_ROLE[role]
}

const statusText = (status: MemberStatus | null) => (status === null ? '无' : COLLAB_AUDIT_STATUS[status])

const ownerAiText = (value: boolean | null) =>
  value === null ? '无' : value ? '允许使用作者模型' : '不允许使用作者模型'

/** 协作审计安全摘要的唯一投影：仅读取白名单 6 键；失败记录只表达操作前状态。 */
export function collaborationAuditSummaryRows(
  event: Pick<CollaborationAuditEvent, 'scope' | 'result' | 'summary'>,
): CollaborationAuditSummaryRow[] {
  const { summary, scope, result } = event
  const rows: CollaborationAuditSummaryRow[] = []
  const failure = result === 'failure'

  if (summary.before_role !== null || (!failure && summary.after_role !== null)) {
    rows.push({
      label: failure ? '操作前角色' : '成员角色',
      value: failure
        ? roleText(scope, summary.before_role)
        : summary.before_role === null
          ? roleText(scope, summary.after_role)
          : `${roleText(scope, summary.before_role)} → ${roleText(scope, summary.after_role)}`,
    })
  }

  if (summary.before_status !== null || (!failure && summary.after_status !== null)) {
    rows.push({
      label: failure ? '操作前状态' : '邀请 / 成员状态',
      value: failure
        ? statusText(summary.before_status)
        : summary.before_status === null
          ? statusText(summary.after_status)
          : `${statusText(summary.before_status)} → ${statusText(summary.after_status)}`,
    })
  }

  if (
    scope === 'project'
    && (summary.before_allow_owner_ai !== null || (!failure && summary.after_allow_owner_ai !== null))
  ) {
    rows.push({
      label: failure ? '操作前作者模型授权' : '作者模型授权',
      value: failure
        ? ownerAiText(summary.before_allow_owner_ai)
        : summary.before_allow_owner_ai === null
          ? ownerAiText(summary.after_allow_owner_ai)
          : `${ownerAiText(summary.before_allow_owner_ai)} → ${ownerAiText(summary.after_allow_owner_ai)}`,
    })
  }

  return rows
}
