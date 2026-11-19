import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CollaborationAuditEvent } from '../api/collaborationAudit'
import {
  adminCollaborationAuditQuery,
  authorCollaborationAuditQuery,
  collaborationAuditSummaryRows,
  collaborationResourceLabel,
  collaborationTargetLabel,
  collaborationUserLabel,
} from './collaborationAudit'

afterEach(() => vi.unstubAllEnvs())

const baseEvent = (over: Partial<CollaborationAuditEvent> = {}): CollaborationAuditEvent => ({
  id: 10,
  operation_id: '00000000-0000-4000-8000-000000000010',
  occurred_at: '2026-11-19T08:00:00Z',
  scope: 'project',
  resource_id: 7,
  resource_label: '星海纪元',
  invitation_id: '00000000-0000-4000-8000-000000000099',
  actor_user_id: 2,
  actor_username: 'owner',
  target_user_id: 5,
  target_username: 'alice',
  action: 'modify',
  result: 'success',
  error_code: null,
  summary: {
    before_role: 'viewer',
    after_role: 'editor',
    before_status: 'accepted',
    after_status: 'accepted',
    before_allow_owner_ai: false,
    after_allow_owner_ai: true,
  },
  ...over,
})

describe('协作审计查询与展示工具', () => {
  it('作者与管理员筛选均将本地日期（含结束日）转为 UTC 半开区间并裁剪空白', () => {
    vi.stubEnv('TZ', 'Asia/Shanghai')
    expect(authorCollaborationAuditQuery({
      member_q: '  alice  ',
      action: 'invite',
      result: 'success',
      fromDate: '2026-11-18',
      toDate: '2026-11-19',
    })).toEqual({
      member_q: 'alice',
      action: 'invite',
      result: 'success',
      from_at: '2026-11-17T16:00:00.000Z',
      to_at: '2026-11-19T16:00:00.000Z',
    })
    expect(authorCollaborationAuditQuery({ member_q: '   ' })).toEqual({ member_q: undefined })
    expect(() => authorCollaborationAuditQuery({ fromDate: '2026-11-18' })).toThrow('请选择完整、有效的起止日期')

    expect(adminCollaborationAuditQuery({
      scope: 'series',
      resource_id: 0,
      resource_q: ' 九州 ',
      actor_q: '  admin ',
      target_q: '   ',
      fromDate: '2026-11-19',
      toDate: '2026-11-19',
    })).toEqual({
      scope: 'series',
      resource_id: 0,
      resource_q: '九州',
      actor_q: 'admin',
      target_q: undefined,
      from_at: '2026-11-18T16:00:00.000Z',
      to_at: '2026-11-19T16:00:00.000Z',
    })
  })

  it('用户与资源快照不出现 #null，作者视角对脱敏失败邀请给出中性提示', () => {
    expect(collaborationUserLabel(null, null)).toBe('未知 / 不适用')
    expect(collaborationUserLabel(12, null)).toBe('历史用户 · #12')
    expect(collaborationUserLabel(12, 'alice')).toBe('alice · #12')

    const maskedInvite = baseEvent({
      action: 'invite',
      result: 'failure',
      error_code: 'invitee_unavailable',
      target_user_id: null,
      target_username: null,
    })
    expect(collaborationTargetLabel(maskedInvite, { ownerView: true })).toBe('未公开 / 不可用')
    expect(collaborationTargetLabel(maskedInvite, { ownerView: false })).toBe('未知 / 不适用')

    expect(collaborationResourceLabel(baseEvent({ resource_id: 0, resource_label: null })))
      .toBe('作品 · 未知资源（无效或非本人邀请） · #0')
    expect(collaborationResourceLabel(baseEvent({ scope: 'series', resource_id: 4, resource_label: '九州' })))
      .toBe('系列 #4 · 九州')
  })

  it('摘要投影区分作品/系列，且失败记录只表达操作前状态，不伪造未提交变更', () => {
    expect(collaborationAuditSummaryRows(baseEvent())).toEqual([
      { label: '成员角色', value: '查看者 → 编辑者' },
      { label: '邀请 / 成员状态', value: '已加入 → 已加入' },
      { label: '作者模型授权', value: '不允许使用作者模型 → 允许使用作者模型' },
    ])

    // 系列无作者模型授权，角色固定显示设定协作者。
    expect(collaborationAuditSummaryRows(baseEvent({
      scope: 'series',
      action: 'invite',
      summary: {
        before_role: null,
        after_role: 'editor',
        before_status: null,
        after_status: 'pending',
        before_allow_owner_ai: null,
        after_allow_owner_ai: null,
      },
    }))).toEqual([
      { label: '成员角色', value: '设定协作者' },
      { label: '邀请 / 成员状态', value: '待接受' },
    ])

    // 失败记录即便传入了残留 after 字段，也不得展示为已提交结果。
    expect(collaborationAuditSummaryRows(baseEvent({
      result: 'failure',
      summary: {
        before_role: 'viewer',
        after_role: 'editor',
        before_status: 'accepted',
        after_status: 'revoked',
        before_allow_owner_ai: false,
        after_allow_owner_ai: true,
      },
    }))).toEqual([
      { label: '操作前角色', value: '查看者' },
      { label: '操作前状态', value: '已加入' },
      { label: '操作前作者模型授权', value: '不允许使用作者模型' },
    ])
  })
})
