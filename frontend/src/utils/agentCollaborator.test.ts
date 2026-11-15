import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { isAgentAccessLost } from '../api/agent'
import type { Task } from '../api/types'
import {
  AGENT_RETRY_REJECTED_HINT, AGENT_MODEL_CHANGED_HINT, AGENT_VOIDED_ERROR, agentChangeState, agentReadOnlyNotice,
  agentRetryState, isAgentTurnOpen,
} from './agent'
import { changeFixture } from './agent.test'

const KEY = '00000000-0000-4000-8000-000000000003'
const turn = (status: Task['status'], over: Partial<Pick<Task, 'id' | 'task_key'>> = {}) => ({ id: 4, task_key: KEY, status, ...over })
const failedTurn = (error: string, kind: Task['kind'] = 'agent.turn') => ({ status: 'failed' as const, error, kind })

describe('协作者智能体：提案、重试与只读状态', () => {
  it('轮次未结束需同一实例且处于排队 / 运行 / 待确认', () => {
    const change = changeFixture()
    for (const status of ['queued', 'running', 'paused'] as const) expect(isAgentTurnOpen(change, turn(status))).toBe(true)
    for (const status of ['failed', 'cancelled', 'interrupted', 'succeeded'] as const) expect(isAgentTurnOpen(change, turn(status))).toBe(false)
    expect(isAgentTurnOpen(change, turn('paused', { task_key: null }))).toBe(false)
    expect(isAgentTurnOpen(change, turn('paused', { task_key: '00000000-0000-4000-8000-000000000009' }))).toBe(false)
    expect(isAgentTurnOpen(change, turn('paused', { id: 5 }))).toBe(false)
    expect(isAgentTurnOpen(change, null)).toBe(false)
  })

  it('作废提案显示「已作废」与原失败原因；已结束轮次的残留提案显示「轮次已结束」', () => {
    expect(agentChangeState(changeFixture({ status: 'failed', error: AGENT_VOIDED_ERROR }), null))
      .toMatchObject({ label: '已作废', ended: false })
    expect(agentChangeState(changeFixture({ status: 'failed', error: '其他错误' }), null)).toMatchObject({ label: '执行失败' })
    expect(agentChangeState(changeFixture(), turn('failed'))).toMatchObject({ label: '轮次已结束', ended: true })
    expect(agentChangeState(changeFixture(), turn('paused'))).toMatchObject({ label: '待确认', ended: false })
    // 不在时间线中（单独渲染卡片）时不推断轮次状态。
    expect(agentChangeState(changeFixture(), null, false)).toMatchObject({ label: '待确认', ended: false })
    expect(agentChangeState(changeFixture({ status: 'executed' }), turn('failed'))).toMatchObject({ label: '已执行', ended: false })
  })

  it('协作者：模型配置失效的失败隐藏重试并提示重新发起；被拒的重试统一提示', () => {
    expect(agentRetryState(failedTurn('模型配置已失效，请重新选择'), true, false)).toEqual({ retry: false, hint: AGENT_MODEL_CHANGED_HINT })
    expect(AGENT_MODEL_CHANGED_HINT).toBe('模型配置已变化（可能是本人或作者的配置），请在会话中重新发起')
    expect(agentRetryState(failedTurn('模型调用失败：超时'), true, false)).toEqual({ retry: true, hint: null })
    expect(agentRetryState(failedTurn('模型调用失败：超时'), true, true)).toEqual({ retry: false, hint: AGENT_RETRY_REJECTED_HINT })
    // 作者行为不变：同样文案仍可重试，也不因拒绝改变。
    expect(agentRetryState(failedTurn('模型配置已失效，请重新选择'), false, true)).toEqual({ retry: true, hint: null })
    // 只针对 agent.turn
    expect(agentRetryState(failedTurn('模型配置已失效，请重新选择', 'chapter.generate'), true, false)).toEqual({ retry: true, hint: null })
    expect(agentRetryState({ status: 'succeeded', error: '', kind: 'agent.turn' }, true, false)).toEqual({ retry: false, hint: null })
  })

  it('只读原因区分查看者与旧授权轮次会话；可写时无提示', () => {
    expect(agentReadOnlyNotice(true, true)).toBeNull()
    expect(agentReadOnlyNotice(false, false)).toContain('当前为查看者')
    expect(agentReadOnlyNotice(false, true)).toContain('之前的协作授权')
  })

  it('409 permission_denied 与 404 视为失去访问；其余 409 / 403 不是', () => {
    expect(isAgentAccessLost(new ApiError(409, '协作授权已变化', { code: 'permission_denied' }))).toBe(true)
    expect(isAgentAccessLost(new ApiError(404, '会话不存在'))).toBe(true)
    expect(isAgentAccessLost(new ApiError(409, '已变化', { code: 'agent_change_changed' }))).toBe(false)
    expect(isAgentAccessLost(new ApiError(403, '只读', { code: 'project_readonly' }))).toBe(false)
    expect(isAgentAccessLost(new Error('网络'))).toBe(false)
  })
})
