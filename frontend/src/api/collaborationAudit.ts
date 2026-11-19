import { request, type QueryValue } from './client'
import type { CollaborationRole, CollaborationScope, MemberStatus } from './collaboration'

/** 协作审计传输类型；与 backend/app/schemas/collaboration_audit.py 保持一致。 */
export type CollaborationAuditAction = 'invite' | 'accept' | 'decline' | 'modify' | 'revoke'
export type CollaborationAuditResult = 'success' | 'failure'
export type CollaborationAuditErrorCode =
  | 'bad_request'
  | 'unauthorized'
  | 'conflict'
  | 'not_found'
  | 'permission_denied'
  | 'invitee_unavailable'
  | 'stale_invitation'
  | 'cancelled'
  | 'already_collaborator'
  | 'self_invite'
  | 'collaboration_busy'
  | 'system_error'

export interface CollaborationAuditSummary {
  before_role: CollaborationRole | null
  after_role: CollaborationRole | null
  before_status: MemberStatus | null
  after_status: MemberStatus | null
  before_allow_owner_ai: boolean | null
  after_allow_owner_ai: boolean | null
}

export interface CollaborationAuditEvent {
  id: number
  operation_id: string
  occurred_at: string
  scope: CollaborationScope
  resource_id: number
  resource_label: string | null
  invitation_id: string | null
  actor_user_id: number | null
  actor_username: string | null
  target_user_id: number | null
  target_username: string | null
  action: CollaborationAuditAction
  result: CollaborationAuditResult
  error_code: CollaborationAuditErrorCode | null
  summary: CollaborationAuditSummary
}

export interface CollaborationAuditPage {
  items: CollaborationAuditEvent[]
  total: number
}

export interface AuthorCollaborationAuditQuery {
  member_user_id?: number
  member_q?: string
  action?: CollaborationAuditAction
  result?: CollaborationAuditResult
  from_at?: string
  to_at?: string
  page?: number
  page_size?: number
}

export interface AdminCollaborationAuditQuery {
  scope?: CollaborationScope
  resource_id?: number
  resource_q?: string
  actor_user_id?: number
  actor_q?: string
  target_user_id?: number
  target_q?: string
  action?: CollaborationAuditAction
  result?: CollaborationAuditResult
  from_at?: string
  to_at?: string
  page?: number
  page_size?: number
}

export const COLLAB_AUDIT_SCOPE: Record<CollaborationScope, string> = {
  project: '作品',
  series: '系列',
}

export const COLLAB_AUDIT_ACTION: Record<CollaborationAuditAction, string> = {
  invite: '发出邀请',
  accept: '接受邀请',
  decline: '拒绝邀请',
  modify: '修改权限',
  revoke: '撤销权限',
}

export const COLLAB_AUDIT_RESULT: Record<CollaborationAuditResult, { label: string; color: string }> = {
  success: { label: '成功', color: 'success' },
  failure: { label: '失败', color: 'error' },
}

export const COLLAB_AUDIT_ROLE: Record<CollaborationRole, string> = {
  viewer: '查看者',
  editor: '编辑者',
}

export const COLLAB_AUDIT_STATUS: Record<MemberStatus, string> = {
  pending: '待接受',
  accepted: '已加入',
  declined: '已拒绝',
  revoked: '已撤销',
}

export const COLLAB_AUDIT_ERROR: Record<CollaborationAuditErrorCode, string> = {
  bad_request: '请求参数无效',
  unauthorized: '未登录或登录失效',
  conflict: '状态冲突',
  not_found: '对象不存在或无权限',
  permission_denied: '无权执行',
  invitee_unavailable: '被邀请账号不可用',
  stale_invitation: '邀请版本已过期',
  cancelled: '操作已取消',
  already_collaborator: '已是协作者或待接受',
  self_invite: '不能邀请自己',
  collaboration_busy: '协作状态并发变化',
  system_error: '系统错误',
}

const trimOptional = (value?: string) => value?.trim() || undefined

const toQuery = (query: Record<string, QueryValue>): Record<string, QueryValue> => query

const ownerAuditPath = (scope: CollaborationScope, resourceId: number) =>
  `/${scope === 'project' ? 'projects' : 'series'}/${resourceId}/collaborators/audit-logs`

export const collaborationAuditApi = {
  listForOwner: (
    scope: CollaborationScope,
    resourceId: number,
    { member_q, ...rest }: AuthorCollaborationAuditQuery = {},
    signal?: AbortSignal,
  ) =>
    request<CollaborationAuditPage>(ownerAuditPath(scope, resourceId), {
      query: toQuery({ ...rest, member_q: trimOptional(member_q) }),
      signal,
    }),

  listForAdmin: (
    { resource_q, actor_q, target_q, ...rest }: AdminCollaborationAuditQuery = {},
    signal?: AbortSignal,
  ) =>
    request<CollaborationAuditPage>('/admin/audit-logs/collaboration', {
      query: toQuery({
        ...rest,
        resource_q: trimOptional(resource_q),
        actor_q: trimOptional(actor_q),
        target_q: trimOptional(target_q),
      }),
      signal,
    }),
}
