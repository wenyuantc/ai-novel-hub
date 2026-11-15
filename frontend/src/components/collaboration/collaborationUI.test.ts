import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ButtonProps } from 'antd'
import type { CollaborationInvitation, CollaborationMember } from '../../api/collaboration'
import { InvitationRows } from './Invitations'
import { MEMBER_AI_ABORT_NOTICE, MemberChangeNotice, MemberRows } from './MemberPanel'

const state = vi.hoisted(() => ({ buttons: [] as ButtonProps[] }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Button: (props: ButtonProps) => {
    state.buttons.push(props)
    return createElement('button', { disabled: props.disabled, onClick: props.onClick }, props.children)
  } }
})

const invitation: CollaborationInvitation = {
  scope: 'project', resource_id: 12, title: '山海', owner: '作者甲', role: 'editor',
  allow_owner_ai: true, invitation_id: 'invitation-old', revision: 7,
}
const member: CollaborationMember = {
  user_id: 5, username: '小林', role: 'editor', status: 'pending', allow_owner_ai: true,
  invitation_id: 'invitation-old', revision: 7, responded_at: null, created_at: '', updated_at: '',
}

beforeEach(() => { state.buttons = [] })

describe('协作组件', () => {
  it('邀请卡显示角色与费用提示，并把确切邀请轮次交给确认动作', () => {
    const choose = vi.fn()
    const html = renderToStaticMarkup(createElement(InvitationRows, { invitations: [invitation], disabled: false, onSelect: choose }))
    expect(html).toContain('作者甲')
    expect(html).toContain('编辑者')
    expect(html).toContain('可能产生作者费用')
    state.buttons.find((button) => button.children === '接受')?.onClick?.({} as never)
    expect(choose).toHaveBeenCalledWith(invitation, 'accept')
    state.buttons.find((button) => button.children === '拒绝')?.onClick?.({} as never)
    expect(choose).toHaveBeenCalledWith(invitation, 'decline')
  })

  it('系列邀请和成员固定展示设定协作者，不出现费用授权或作品角色修改', () => {
    const edit = vi.fn()
    const revoke = vi.fn()
    const html = renderToStaticMarkup(createElement(MemberRows, {
      members: [member, { ...member, user_id: 6, username: '旧用户', status: 'revoked' }],
      scope: 'series', disabled: false, onEdit: edit, onRevoke: revoke,
    }))
    expect(html).toContain('设定协作者')
    expect(html).not.toContain('作者模型')
    expect(html).not.toContain('修改权限')
    expect(state.buttons.filter((button) => button.children === '撤销')).toHaveLength(1)
    state.buttons.find((button) => button.children === '撤销')?.onClick?.({} as never)
    expect(revoke).toHaveBeenCalledWith(member)
    expect(edit).not.toHaveBeenCalled()
  })

  it('修改或撤销已加入的作品成员时提示会中止其进行中的 AI 任务；待接受成员与系列成员不提示', () => {
    const accepted = { ...member, status: 'accepted' as const }
    expect(renderToStaticMarkup(createElement(MemberChangeNotice, { scope: 'project', member: accepted }))).toContain(MEMBER_AI_ABORT_NOTICE)
    expect(MEMBER_AI_ABORT_NOTICE).toContain('智能体')
    expect(renderToStaticMarkup(createElement(MemberChangeNotice, { scope: 'project', member }))).toBe('')
    expect(renderToStaticMarkup(createElement(MemberChangeNotice, { scope: 'series', member: accepted }))).toBe('')
  })

  it('旧事实未确认时成员操作全部禁用', () => {
    renderToStaticMarkup(createElement(MemberRows, {
      members: [member], scope: 'project', disabled: true, onEdit: vi.fn(), onRevoke: vi.fn(),
    }))
    expect(state.buttons.map((button) => button.disabled)).toEqual([true, true])
  })
})
