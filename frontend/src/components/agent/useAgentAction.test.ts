import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { AgentConversationDetail } from '../../api/types'
import { AgentReadSuperseded, runAgentAction, type AgentActionDeps } from './useAgentConversation'

const detail = { messages: [], changes: [] } as unknown as AgentConversationDetail

function deps(over: Partial<AgentActionDeps> = {}) {
  const calls = { settle: 0, fail: [] as unknown[], reread: 0, rejectRefresh: 0 }
  const value: AgentActionDeps = {
    operation: async () => undefined,
    refreshOnReject: async () => { calls.rejectRefresh += 1 },
    current: () => true,
    reread: async () => { calls.reread += 1; return detail },
    settle: () => { calls.settle += 1 },
    fail: (err) => { calls.fail.push(err) },
    ...over,
  }
  return { value, calls }
}

describe('智能体写操作：受理即视为已发送（E4i-h 问题 1）', () => {
  it('正常：受理并重读完成', async () => {
    const { value, calls } = deps()
    expect(await runAgentAction(value)).toEqual({ accepted: true, ok: true })
    expect(calls).toMatchObject({ settle: 1, reread: 1, fail: [] })
  })

  it('受理后重读被并发读取（协作者轮询 / 回前台 / 可见性刷新）取代：仍算已受理，不提示「权限已变化」，再读一次后照常收尾', async () => {
    let reads = 0
    const { value, calls } = deps({
      reread: async () => { reads += 1; if (reads === 1) throw new AgentReadSuperseded(); return detail },
    })
    expect(await runAgentAction(value)).toEqual({ accepted: true, ok: true })
    expect(calls.fail).toEqual([])
    expect(calls.settle).toBe(1)
  })

  it('受理后重读失败（网络 / 5xx）：仍算已受理（输入可清空），错误照常提示', async () => {
    const boom = new TypeError('Failed to fetch')
    const { value, calls } = deps({ reread: async () => { throw boom } })
    expect(await runAgentAction(value)).toEqual({ accepted: true, ok: false })
    expect(calls.fail).toEqual([boom])
  })

  it('受理后作用域失效（重挂 / 换授权轮次）：仍算已受理，不再写状态', async () => {
    let live = true
    const { value, calls } = deps({ operation: async () => { live = false }, current: () => live })
    expect(await runAgentAction(value)).toEqual({ accepted: true, ok: false })
    expect(calls).toMatchObject({ reread: 0, settle: 0, fail: [] })
  })

  it('受理后挂载已卸载导致重读取代：已受理且静默', async () => {
    let live = true
    const { value, calls } = deps({ reread: async () => { live = false; throw new AgentReadSuperseded() }, current: () => live })
    expect(await runAgentAction(value)).toEqual({ accepted: true, ok: false })
    expect(calls.fail).toEqual([])
  })

  it('服务端拒绝：未受理，保留输入；按事实重读一次且不重放', async () => {
    const denied = new ApiError(409, '权限已变化', { detail: '权限已变化', code: 'permission_denied' })
    const operation = vi.fn(async () => { throw denied })
    const { value, calls } = deps({ operation })
    expect(await runAgentAction(value)).toEqual({ accepted: false, ok: false })
    expect(operation).toHaveBeenCalledTimes(1)
    expect(calls).toMatchObject({ rejectRefresh: 1, reread: 0, fail: [denied] })
  })

  it('排空拒绝保留中文说明和输入，关闭后由用户重新提交', async () => {
    const denied = new ApiError(503, '系统正在维护，暂不受理新任务，请稍后再试', { code: 'task_admission_closed' })
    const operation = vi.fn(async () => { throw denied })
    const { value, calls } = deps({ operation })
    expect(await runAgentAction(value)).toEqual({ accepted: false, ok: false })
    expect(operation).toHaveBeenCalledTimes(1)
    expect(calls).toMatchObject({ rejectRefresh: 0, reread: 0, fail: [denied] })
  })

  it('服务端撤权拒绝后按事实重读被取代：仍把原 409 permission_denied 交给失败处理（触发撤权恢复），不被静默吞掉', async () => {
    const denied = new ApiError(409, '权限已变化', { detail: '权限已变化', code: 'permission_denied' })
    const { value, calls } = deps({
      operation: async () => { throw denied },
      refreshOnReject: async () => { throw new AgentReadSuperseded() },
    })
    expect(await runAgentAction(value)).toEqual({ accepted: false, ok: false })
    expect(calls.fail).toEqual([denied])
  })

  it('受理后重读返回 404（发送后立即撤权）：仍算已受理，错误交给失败处理以进入撤权恢复', async () => {
    const gone = new ApiError(404, '会话不存在', { detail: '会话不存在' })
    const { value, calls } = deps({ reread: async () => { throw gone } })
    expect(await runAgentAction(value)).toEqual({ accepted: true, ok: false })
    expect(calls).toMatchObject({ settle: 0, fail: [gone] })
  })

  it('重读多次被取代：最多重试两次后放弃，仍算已受理且不提示', async () => {
    const reread = vi.fn(async (): Promise<AgentConversationDetail> => { throw new AgentReadSuperseded() })
    const { value, calls } = deps({ reread })
    expect(await runAgentAction(value)).toEqual({ accepted: true, ok: false })
    expect(reread).toHaveBeenCalledTimes(3)
    expect(calls.fail).toEqual([])
  })
})
