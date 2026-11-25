import { renderToStaticMarkup } from 'react-dom/server'
import { createElement, type ReactElement } from 'react'
import { StaticRouter } from 'react-router-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { taskApi } from '../../api/tasks'
import { ApiError } from '../../api/client'
import { consistencyApi } from '../../api/consistency'
import type { CollaborationInvitation } from '../../api/collaboration'
import type { UsageBudget } from '../../api/aiUsage'
import type { UsageSession } from '../aiUsage/useAiUsageRequest'
import UsageBudgets from '../aiUsage/UsageBudgets'
import Invitations from '../collaboration/Invitations'
import TaskPanel from '../TaskPanel'
import ConsistencyPage from '../../pages/project/ConsistencyPage'

const state = vi.hoisted(() => ({
  read: null as null | ((signal: AbortSignal) => Promise<unknown>), canRead: true, missing: false,
  invitations: [] as CollaborationInvitation[], budgets: [] as UsageBudget[],
}))
vi.mock('./useNotificationTarget', () => ({
  useNotificationTarget: (read: (signal: AbortSignal) => Promise<unknown>) => { state.read = read; return { result: state.missing ? { missing: true } : undefined } },
}))
vi.mock('../collaboration/useCollaborationFacts', () => ({
  useCollaborationFacts: () => ({ data: state.invitations, identityKey: 'actor' }),
}))
vi.mock('../aiUsage/useAiUsageRequest', () => ({
  useAiUsageRequest: () => ({ data: state.budgets, loading: false }),
  useAiUsageMutation: () => ({ commit: vi.fn(), loading: false }),
}))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ ready: true, canRead: state.canRead }) }))
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 8 }))

const render = (url: string, element: ReactElement) => renderToStaticMarkup(createElement(StaticRouter, { location: url }, element))
const uuid = '5bf799fd-f325-44ce-933d-f2df985fbabc'
beforeEach(() => { vi.restoreAllMocks(); state.read = null; state.canRead = true; state.missing = false })

describe('从通知精确打开业务目标', () => {
  it('书架只显示当次邀请，撤回再邀不能复活旧链接', () => {
    const row: CollaborationInvitation = { scope: 'series', invitation_id: uuid, title: '新邀请', owner: '作者', resource_id: 10, role: 'editor',
      allow_owner_ai: false, revision: 1 }
    state.invitations = [row, { ...row, title: '其他邀请', scope: 'project' }]
    const renderTarget = () => render('/projects', createElement(Invitations, { invitationId: uuid, invitationScope: 'series', onChanged: vi.fn() }))
    expect(renderTarget()).toContain('新邀请')
    expect(renderTarget()).not.toContain('其他邀请')
    state.invitations = [{ ...row, invitation_id: 'replacement' }]
    expect(renderTarget()).toContain('内容已不可用')
    expect(renderTarget()).not.toContain('新邀请')
  })

  it('币种只定位相应预算，删除后显示失效提示', () => {
    const row: UsageBudget = { instance_key: uuid, scope: 'account', currency: 'USD', limit: '10', threshold_percent: 80, spent: '8',
      unpriced_calls: 0, state: 'near', period_start: null, period_end: null }
    state.budgets = [row, { ...row, currency: 'CNY' }]
    const renderTarget = () => render(`/settings/ai-usage?budget_currency=USD&budget_key=${uuid}`, createElement(UsageBudgets, { scope: { kind: 'account' },
      session: {} as UsageSession }))
    expect(renderTarget()).toContain('美元（USD）')
    expect(renderTarget()).not.toContain('人民币（CNY）')
    state.budgets = [{ ...row, instance_key: 'replacement' }]
    expect(renderTarget()).toContain('内容已不可用')
  })

  it('个人任务绕过列表分页，同时核对数字 ID 和 UUID', async () => {
    const get = vi.spyOn(taskApi, 'get').mockRejectedValue(new Error('测试请求'))
    render(`/projects?task_id=76&task_key=${uuid}`, createElement(TaskPanel, { hideTrigger: true }))
    const signal = new AbortController().signal
    await expect(state.read!(signal)).rejects.toThrow('测试请求')
    expect(get).toHaveBeenCalledWith({ id: 76, task_key: uuid }, signal)
    state.read = null
    render('/projects?task_id=76&task_key=bad', createElement(TaskPanel, { hideTrigger: true }))
    expect(state.read).toBeNull()
  })

  it('只获取指定检查运行，撤权后不发起请求', async () => {
    const get = vi.spyOn(consistencyApi, 'run').mockRejectedValue(new ApiError(404, '通知不可用'))
    const renderRun = (id: string) => render(`/projects/8/consistency?run_id=2&notification_id=${id}`, createElement(ConsistencyPage))
    const signal = new AbortController().signal
    renderRun(uuid)
    await expect(state.read!(signal)).rejects.toMatchObject({ status: 404 })
    expect(get).toHaveBeenCalledWith(8, 2, signal, uuid)
    state.missing = true
    expect(renderRun(uuid)).toContain('内容已不可用')
    // 同一数值运行 ID 的另一封通知必须向服务端提交自己的身份。
    const another = '4b984cca-b210-44ca-9900-c201c6f6e729'
    renderRun(another)
    await expect(state.read!(signal)).rejects.toMatchObject({ status: 404 })
    expect(get).toHaveBeenCalledWith(8, 2, signal, another)
    state.read = null
    state.canRead = false
    expect(render(`/projects/8/consistency?run_id=2&notification_id=${uuid}`, createElement(ConsistencyPage))).toContain('内容已不可用')
    expect(state.read).toBeNull()
  })
})
