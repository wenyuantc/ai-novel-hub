// 组织与成员

import { api } from './client'
import type {
  Member,
  MemberInput,
  MemberStatus,
  MemberUpdate,
  Organization,
  OrganizationGenerateRequest,
  OrganizationInput,
  OrganizationUpdate,
  OrganizationWithMembers,
  OrganizationWithMembersOut,
  Task,
} from './types'

const base = (pid: number) => `/projects/${pid}/organizations`

export const organizationApi = {
  list: (pid: number) => api.get<Organization[]>(base(pid)),
  get: (pid: number, oid: number) => api.get<Organization>(`${base(pid)}/${oid}`),
  create: (pid: number, body: OrganizationInput) => api.post<Organization>(base(pid), body),
  withMembers: (pid: number, body: OrganizationWithMembers) =>
    api.post<OrganizationWithMembersOut>(`${base(pid)}/with-members`, body),
  update: (pid: number, oid: number, body: OrganizationUpdate) => api.patch<Organization>(`${base(pid)}/${oid}`, body),
  remove: (pid: number, oid: number) => api.del(`${base(pid)}/${oid}`),
  /** AI 生成单个组织：202 任务（organization.generate），只产出草稿 */
  generate: (pid: number, body: OrganizationGenerateRequest) => api.post<Task>(`${base(pid)}/generate`, body),
}

export const memberApi = {
  list: (pid: number, oid: number) => api.get<Member[]>(`${base(pid)}/${oid}/members`),
  create: (pid: number, oid: number, body: MemberInput) => api.post<Member>(`${base(pid)}/${oid}/members`, body),
  update: (pid: number, oid: number, mid: number, body: MemberUpdate) =>
    api.patch<Member>(`${base(pid)}/${oid}/members/${mid}`, body),
  remove: (pid: number, oid: number, mid: number) => api.del(`${base(pid)}/${oid}/members/${mid}`),
}

export const MEMBER_STATUS: Record<MemberStatus, { label: string; color: string }> = {
  active: { label: '在任', color: 'green' },
  left: { label: '离开', color: 'default' },
  expelled: { label: '开除', color: 'orange' },
  dead: { label: '阵亡', color: 'red' },
}

export function memberStatusLabel(status: string): { label: string; color: string } {
  return status in MEMBER_STATUS ? MEMBER_STATUS[status as MemberStatus] : { label: status || '-', color: 'default' }
}
