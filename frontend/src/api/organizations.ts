// 组织与成员

import { api, request } from './client'
import type {
  Member,
  MemberInput,
  MemberStatus,
  MemberUpdate,
  ProgressionPositionInput,
  Organization,
  OrganizationGenerateRequest,
  OrganizationInput,
  OrganizationUpdate,
  OrganizationWithMembers,
  OrganizationWithMembersOut,
  Task,
} from './types'
import { toPositionInput } from '../components/progressions/position'

export interface MemberState {
  position: string
  status: MemberStatus
  note: string
}

export type MemberIntent = Pick<MemberInput, 'edit_mode' | 'expected_revision' | 'anchor_kind' | 'chapter_id' | 'chapter_no' | 'boundary'> & {
  edit_mode: 'baseline' | 'progression'
}

export type MemberSnapshot = { exists: false } | { exists: true; state: MemberState }

/** 不用最新投影补齐缺失的时点字段。 */
export function memberSnapshot(fields: Record<string, unknown>): MemberSnapshot {
  if (fields.exists === false) return { exists: false }
  if (fields.exists !== true || typeof fields.position !== 'string' || typeof fields.note !== 'string' ||
      typeof fields.status !== 'string' || !Object.hasOwn(MEMBER_STATUS, fields.status)) {
    throw new Error('成员设定快照不完整，请刷新重试')
  }
  return { exists: true, state: { position: fields.position, status: fields.status as MemberStatus, note: fields.note } }
}

/** 比较当前所编辑的基础或指定章节快照，只传真正修改的字段。 */
export function memberChanges(initial: MemberState, values: MemberState): Partial<MemberState> {
  const next = { ...values, position: values.position.trim() }
  const previous = { ...initial, position: initial.position.trim() }
  return Object.fromEntries(Object.entries(next).filter(([key, value]) => value !== previous[key as keyof MemberState])) as Partial<MemberState>
}

export function memberPositionIntent(position: ProgressionPositionInput, revision: number): MemberIntent {
  const normalized = toPositionInput(position)
  if (!normalized || !Number.isSafeInteger(revision) || revision < 1) throw new Error('请选择有效章节并刷新设定版本')
  return {
    edit_mode: 'progression', expected_revision: revision,
    anchor_kind: normalized.anchor_kind,
    ...(normalized.anchor_kind === 'chapter' ? { chapter_id: normalized.chapter_id! } : { chapter_no: normalized.chapter_no! }),
    boundary: normalized.boundary,
  }
}

const base = (pid: number) => `/projects/${pid}/organizations`

export const organizationApi = {
  list: (pid: number) => api.get<Organization[]>(base(pid)),
  get: (pid: number, oid: number, signal?: AbortSignal) => request<Organization>(`${base(pid)}/${oid}`, { signal }),
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
  create: (pid: number, oid: number, body: MemberInput & Partial<MemberIntent>) => api.post<Member>(`${base(pid)}/${oid}/members`, body),
  update: (pid: number, oid: number, mid: number, body: MemberUpdate & Partial<MemberIntent>) =>
    api.patch<Member>(`${base(pid)}/${oid}/members/${mid}`, body),
  remove: (pid: number, oid: number, mid: number, intent: Omit<MemberIntent, 'edit_mode'> & { edit_mode: 'progression' | 'delete' }) =>
    api.del(`${base(pid)}/${oid}/members/${mid}`, { ...intent }),
  removeRecord: (pid: number, oid: number, characterId: number, expectedRevision: number) =>
    api.del(`${base(pid)}/${oid}/membership-records/${characterId}`, { expected_revision: expectedRevision }),
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
