import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CollaborationAuditEvent } from '../../api/collaborationAudit'

const state = vi.hoisted(() => ({ mobile: true }))
vi.mock('../../hooks/useIsMobile', () => ({
  useIsMobile: () => state.mobile,
  useModalWidth: (width: number | string) => (state.mobile ? 'calc(100vw - 24px)' : width),
}))
vi.mock('../../stores/auth', () => ({
  useAuthStore: (select: (s: unknown) => unknown) => select({
    status: 'authenticated',
    user: { id: 2, username: '作者A', is_admin: false, is_super_admin: false, effective_permissions: [], authorization_revision: 1, roles: [] },
  }),
}))

const { default: MemberPanel } = await import('./MemberPanel')
const { default: CollaborationAuditPanel, CollaborationAuditItems } = await import('./CollaborationAuditPanel')
const { CollaborationAuditDetailContent } = await import('../auditLogs/CollaborationAuditDetailDrawer')

const render = (element: ReactElement) => renderToString(createElement(App, null, element)).replaceAll('<!-- -->', '')
const noop = () => {}

const sampleEvent = (over: Partial<CollaborationAuditEvent> = {}): CollaborationAuditEvent => ({
  id: 42,
  operation_id: '00000000-0000-4000-8000-000000000042',
  occurred_at: '2026-11-19T08:00:00Z',
  scope: 'project',
  resource_id: 9,
  resource_label: '长作品名'.repeat(20),
  invitation_id: '00000000-0000-4000-8000-000000000099',
  actor_user_id: 2,
  actor_username: '作者A',
  target_user_id: 5,
  target_username: '协作者B',
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

afterEach(() => { state.mobile = true })

describe('作者侧协作审计界面', () => {
  it('MemberPanel 为作者同时呈现「成员管理」与「协作审计」标签页，无权时不渲染', () => {
    const projectHtml = render(createElement(MemberPanel, { scope: 'project', resourceId: 9, canManage: true }))
    expect(projectHtml).toContain('成员管理')
    expect(projectHtml).toContain('协作审计')

    const seriesHtml = render(createElement(MemberPanel, { scope: 'series', resourceId: 3, canManage: true }))
    expect(seriesHtml).toContain('成员管理')
    expect(seriesHtml).toContain('协作审计')

    const deniedMemberHtml = render(createElement(MemberPanel, { scope: 'project', resourceId: 9, canManage: false }))
    expect(deniedMemberHtml).not.toContain('成员管理')
    expect(deniedMemberHtml).not.toContain('协作审计')
  })

  it('作者审计面板展示筛选控件、隐私边界说明、本地日期输入，且 375px 下无大 min-width 或宽表', () => {
    const html = render(createElement(CollaborationAuditPanel, { scope: 'project', resourceId: 9, canManage: true }))
    for (const expected of [
      '作品协作审计', '成员用户名快照', '动作', '结果', '开始日期（本地，含）', '结束日期（本地，含）',
      '不包含其他用户的越权尝试', '不区分账号不存在或已禁用', '没有匹配的协作审计记录', 'type="date"',
    ]) {
      expect(html).toContain(expected)
    }
    expect(html).not.toContain('<table')
    expect(html).not.toMatch(/min-width:\s*\d{3,}px/)
    expect(html).not.toContain('100vw')
    expect(render(createElement(CollaborationAuditPanel, { scope: 'series', resourceId: 3, canManage: true })))
      .toContain('系列协作审计')
    const deniedAuditHtml = render(createElement(CollaborationAuditPanel, { scope: 'project', resourceId: 9, canManage: false }))
    expect(deniedAuditHtml).not.toContain('作品协作审计')
    expect(deniedAuditHtml).not.toContain('成员用户名快照')
  })

  it('作者记录列表与只读详情展示变更摘要、脱敏失败邀请中性文案，且无写操作按钮', () => {
    const maskedFail = sampleEvent({
      id: 43,
      action: 'invite',
      result: 'failure',
      error_code: 'invitee_unavailable',
      target_user_id: null,
      target_username: null,
      summary: {
        before_role: null, after_role: null, before_status: null, after_status: null,
        before_allow_owner_ai: null, after_allow_owner_ai: null,
      },
    })
    const listHtml = render(createElement(CollaborationAuditItems, {
      items: [sampleEvent(), maskedFail], loading: false, onDetail: noop,
    }))
    for (const expected of [
      '修改权限', '查看者 → 编辑者', '不允许使用作者模型 → 允许使用作者模型',
      '发出邀请', '失败', '被邀请账号不可用', '未公开 / 不可用',
    ]) {
      expect(listHtml).toContain(expected)
    }
    expect(listHtml).not.toContain('#null')

    const detailHtml = render(createElement(CollaborationAuditDetailContent, { event: maskedFail, ownerView: true }))
    for (const expected of ['未公开 / 不可用', 'invitee_unavailable', '被邀请账号不可用', '本次操作无状态变更摘要', '不区分账号不存在或已禁用']) {
      expect(detailHtml).toContain(expected)
    }
    expect(detailHtml).not.toContain('删除')
    expect(detailHtml).not.toContain('清理')
  })
})
