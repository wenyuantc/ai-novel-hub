import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PollingVisibility } from '../../utils/visiblePolling'
import { ApiError } from '../../api/client'
import {
  AGENT_TURN_FALLBACK_POLL_MS, AgentReadSuperseded, agentProgressMode, createAgentAccessRecovery, startAgentTurnPolling,
} from './useAgentConversation'

function fakeVisibility(initial: DocumentVisibilityState = 'visible') {
  const listeners = new Set<() => void>()
  const target: PollingVisibility & { set: (state: DocumentVisibilityState) => void; size: () => number } = {
    visibilityState: initial,
    addEventListener: (_type, listener) => { listeners.add(listener) },
    removeEventListener: (_type, listener) => { listeners.delete(listener) },
    set(state) { (this as { visibilityState: DocumentVisibilityState }).visibilityState = state; for (const l of [...listeners]) l() },
    size: () => listeners.size,
  }
  return target
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('智能体轮次进度来源（实时订阅 + 兜底可见轮询）', () => {
  it('作者与协作者均对活动轮次（queued / running）实时订阅；待审核与已结束不订阅；失去权限后停止', () => {
    expect(agentProgressMode(false, false, { status: 'running' })).toBe('stream')
    expect(agentProgressMode(false, false, { status: 'queued' })).toBe('stream')
    expect(agentProgressMode(false, false, { status: 'paused' })).toBe('none')
    expect(agentProgressMode(true, false, { status: 'running' })).toBe('stream')
    expect(agentProgressMode(true, false, { status: 'queued' })).toBe('stream')
    expect(agentProgressMode(true, false, { status: 'paused' })).toBe('none')
    expect(agentProgressMode(true, false, { status: 'failed' })).toBe('none')
    expect(agentProgressMode(true, false, null)).toBe('none')
    expect(agentProgressMode(true, true, { status: 'running' })).toBe('none')
    expect(agentProgressMode(false, true, { status: 'running' })).toBe('none')
  })

  it('存在未结束轮次时，作者与协作者均在页面可见时每 5 秒兜底核对，隐藏暂停、回前台立即刷新；清理后完全停止', () => {
    for (const collaborator of [true, false]) {
      const refresh = vi.fn()
      const visibility = fakeVisibility()
      const stop = startAgentTurnPolling(refresh, { collaborator, lost: false, activeId: 8, activeOpen: true }, visibility)
      vi.advanceTimersByTime(AGENT_TURN_FALLBACK_POLL_MS * 2)
      expect(refresh).toHaveBeenCalledTimes(2)
      visibility.set('hidden')
      vi.advanceTimersByTime(AGENT_TURN_FALLBACK_POLL_MS * 3)
      expect(refresh).toHaveBeenCalledTimes(2)
      visibility.set('visible')
      expect(refresh).toHaveBeenCalledTimes(3)
      stop?.()
      expect(visibility.size()).toBe(0)
      vi.advanceTimersByTime(AGENT_TURN_FALLBACK_POLL_MS * 3)
      expect(refresh).toHaveBeenCalledTimes(3)
    }
  })

  it('无未结束轮次时只在回前台时刷新，不启动定时器', () => {
    for (const collaborator of [true, false]) {
      const idle = vi.fn()
      const visibility = fakeVisibility()
      const stopIdle = startAgentTurnPolling(idle, { collaborator, lost: false, activeId: undefined, activeOpen: false }, visibility)
      vi.advanceTimersByTime(AGENT_TURN_FALLBACK_POLL_MS * 4)
      expect(idle).not.toHaveBeenCalled()
      visibility.set('visible')
      expect(idle).toHaveBeenCalledTimes(1)
      stopIdle?.()
      expect(visibility.size()).toBe(0)
    }
  })

  it('中途失去权限（409 permission_denied / 404）后不再轮询或监听', () => {
    const refresh = vi.fn()
    const visibility = fakeVisibility()
    for (const collaborator of [true, false]) {
      expect(startAgentTurnPolling(refresh, { collaborator, lost: true, activeId: 8, activeOpen: true }, visibility)).toBeUndefined()
    }
    vi.advanceTimersByTime(30_000)
    visibility.set('visible')
    expect(refresh).not.toHaveBeenCalled()
    expect(visibility.size()).toBe(0)
  })
})

describe('中途失权的恢复（E4i-f 复核）', () => {
  const setup = (reread: () => Promise<unknown>, alive = () => true) => {
    const calls = { lost: [] as boolean[], stop: 0, project: 0, gone: 0, errors: [] as unknown[], reads: 0 }
    const recovery = createAgentAccessRecovery({
      alive, setLost: (value) => calls.lost.push(value), stop: () => { calls.stop += 1 },
      refreshProject: () => { calls.project += 1 }, reread: () => { calls.reads += 1; return reread() },
      onGone: () => { calls.gone += 1 }, onError: (err) => calls.errors.push(err),
    })
    return { recovery, calls }
  }
  const denied = () => new ApiError(409, '权限已变化', { detail: '权限已变化', code: 'permission_denied' })

  it('只进入一次核对：停止订阅、刷新作品权限、权威重读一次；仍可读则恢复（以服务端 writable 为准）', async () => {
    let release!: () => void
    const { recovery, calls } = setup(() => new Promise<void>((resolve) => { release = resolve }))
    const first = recovery.markLost()
    await recovery.markLost()
    expect(calls).toMatchObject({ lost: [true], stop: 1, project: 1, reads: 1 })
    expect(recovery.isLost()).toBe(true)
    release()
    await first
    expect(calls.lost).toEqual([true, false])
    expect(recovery.isLost()).toBe(false)
  })

  it('重读仍 404 / 409：清空并保持停止，后续失败不再自动重读（无循环）', async () => {
    for (const err of [new ApiError(404, '会话不存在'), denied()]) {
      const { recovery, calls } = setup(() => Promise.reject(err))
      await recovery.markLost()
      await recovery.markLost()
      expect(calls).toMatchObject({ lost: [true], gone: 1, reads: 1, project: 1 })
      expect(recovery.isLost()).toBe(true)
    }
  })

  it('重读暂时失败：保持停止并显示错误；之后一次成功的权威读取（手动刷新）恢复', async () => {
    const { recovery, calls } = setup(() => Promise.reject(new TypeError('Failed to fetch')))
    await recovery.markLost()
    expect(calls.errors).toHaveLength(1)
    expect(calls.gone).toBe(0)
    expect(recovery.isLost()).toBe(true)
    recovery.settle()
    expect(calls.lost).toEqual([true, false])
    expect(recovery.isLost()).toBe(false)
    recovery.settle()
    expect(calls.lost).toEqual([true, false])
  })

  it('被更新的读取取代不算失败；卸载后不再写状态', async () => {
    const superseded = setup(() => Promise.reject(new AgentReadSuperseded()))
    await superseded.recovery.markLost()
    expect(superseded.calls.errors).toEqual([])
    expect(superseded.calls.gone).toBe(0)
    let mounted = true
    const unmounted = setup(() => { mounted = false; return Promise.reject(new ApiError(404, '会话不存在')) }, () => mounted)
    await unmounted.recovery.markLost()
    expect(unmounted.calls.gone).toBe(0)
    unmounted.recovery.settle()
    expect(unmounted.calls.lost).toEqual([true])
  })
})
