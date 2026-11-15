import { request } from './client'

/** 领域类型与 schemas/collaboration.py 一致；系列成员固定为 editor。 */
export type CollaborationScope = 'project' | 'series'
export type CollaborationRole = 'viewer' | 'editor'
export type MemberStatus = 'pending' | 'accepted' | 'declined' | 'revoked'

export interface CollaborationMember {
  user_id: number
  username: string
  role: CollaborationRole
  status: MemberStatus
  allow_owner_ai: boolean | null
  invitation_id: string
  revision: number
  responded_at: string | null
  created_at: string
  updated_at: string
}

export interface CollaborationInvitation {
  scope: CollaborationScope
  resource_id: number
  title: string
  owner: string
  role: CollaborationRole
  allow_owner_ai: boolean | null
  invitation_id: string
  revision: number
}

export interface ProjectInvite {
  username: string
  role: CollaborationRole
  allow_owner_ai: boolean
}

export interface ProjectMemberUpdate {
  expected_revision: number
  role?: CollaborationRole
  allow_owner_ai?: boolean
}

const membersPath = (scope: CollaborationScope, resourceId: number) =>
  `/${scope === 'project' ? 'projects' : 'series'}/${resourceId}/collaborators`

export const collaborationApi = {
  invitations: (signal?: AbortSignal) =>
    request<CollaborationInvitation[]>('/collaboration/invitations', { signal }),
  respond: (scope: CollaborationScope, invitationId: string, action: 'accept' | 'decline', revision: number, signal?: AbortSignal) =>
    request<CollaborationMember>(`/collaboration/invitations/${scope}/${encodeURIComponent(invitationId)}/${action}`, {
      method: 'POST', body: { expected_revision: revision }, signal,
    }),
  members: (scope: CollaborationScope, resourceId: number, signal?: AbortSignal) =>
    request<CollaborationMember[]>(membersPath(scope, resourceId), { signal }),
  inviteProject: (resourceId: number, body: ProjectInvite, signal?: AbortSignal) =>
    request<CollaborationMember>(membersPath('project', resourceId), { method: 'POST', body, signal }),
  inviteSeries: (resourceId: number, username: string, signal?: AbortSignal) =>
    request<CollaborationMember>(membersPath('series', resourceId), { method: 'POST', body: { username }, signal }),
  updateProject: (resourceId: number, userId: number, body: ProjectMemberUpdate, signal?: AbortSignal) =>
    request<CollaborationMember>(`${membersPath('project', resourceId)}/${userId}`, { method: 'PATCH', body, signal }),
  revoke: (scope: CollaborationScope, resourceId: number, userId: number, revision: number, signal?: AbortSignal) =>
    request<void>(`${membersPath(scope, resourceId)}/${userId}`, {
      method: 'DELETE', query: { expected_revision: revision }, signal,
    }),
}
