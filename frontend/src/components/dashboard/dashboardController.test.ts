import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { adminDashboardApi } from '../../api/adminDashboard'
import type { AdminDashboardDays, AdminDashboardHistory, AdminDashboardOverview, AdminDashboardTasks } from '../../api/types'
import { DashboardController, startDashboardPolling } from './dashboardController'

const meta = { as_of: '2026-10-31T10:00:00Z', tracking_since: '2026-10-31T08:00:00Z', timezone: 'Asia/Shanghai' as const }
const overview = (users = 1): AdminDashboardOverview => ({ ...meta, users, projects: 0, chapters: 0, total_words: 0, pending_submissions: 0 })
const tasks = (): AdminDashboardTasks => ({ ...meta, baseline_tasks: 0, new_tasks: 0, queued: 0, running: 0, paused: 0, succeeded: 0, failed: 0, cancelled: 0, interrupted: 0, failed_24h: 0, interrupted_24h: 0, removed_unfinished: 0 })
const history = (days: AdminDashboardDays = 30): AdminDashboardHistory => ({ ...meta, days, start_date: '2026-10-02', end_date: '2026-10-31', points: [], comparison: { from_day: '2026-10-02', to_day: '2026-10-31', users: null, projects: null, chapters: null, total_words: null } })
function loaders() {
  return {
    overview: vi.fn<typeof adminDashboardApi.overview>(async () => overview()),
    tasks: vi.fn<typeof adminDashboardApi.tasks>(async () => tasks()),
    history: vi.fn<typeof adminDashboardApi.history>(async (days: AdminDashboardDays = 30) => history(days)),
  }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function settled() { await Promise.resolve(); await Promise.resolve() }
afterEach(() => vi.useRealTimers())

describe('管理员统计请求生命周期', () => {
  it('三个请求独立完成和失败，失败不阻断其他区块或造零', async () => {
    const api = loaders()
    api.overview.mockRejectedValue(new ApiError(500, '规模查询失败'))
    const controller = new DashboardController(1, api)
    controller.activate()
    controller.refreshAll()
    await settled()
    const state = controller.getSnapshot()
    expect(state.overview).toMatchObject({ data: null, loading: false, error: '规模查询失败' })
    expect(state.tasks.data?.new_tasks).toBe(0)
    expect(state.history.data?.days).toBe(30)
    expect(state.tasks.error).toBeNull()
    controller.deactivate()
  })

  it('同一区块请求未完成时不堆积请求，刷新失败保留上次成功值和错误', async () => {
    const api = loaders()
    const pending = deferred<AdminDashboardOverview>()
    api.overview.mockReturnValueOnce(pending.promise)
    const controller = new DashboardController(1, api)
    controller.activate()
    controller.refresh('overview')
    controller.refresh('overview')
    expect(api.overview).toHaveBeenCalledTimes(1)
    pending.resolve(overview(9))
    await settled()
    api.overview.mockRejectedValueOnce(new ApiError(0, '网络不可用'))
    controller.refresh('overview')
    await settled()
    expect(controller.getSnapshot().overview).toMatchObject({ data: { users: 9 }, error: '网络不可用', loading: false })
    controller.deactivate()
  })

  it('切换范围同步中止旧请求，迟到的30天结果不能覆盖7天结果', async () => {
    const api = loaders()
    const old = deferred<AdminDashboardHistory>()
    const latest = deferred<AdminDashboardHistory>()
    api.history.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const controller = new DashboardController(1, api)
    controller.activate()
    controller.refresh('history')
    const signal = api.history.mock.calls[0][1]
    controller.setDays(7)
    expect(signal?.aborted).toBe(true)
    expect(controller.getSnapshot().history.data).toBeNull()
    latest.resolve(history(7))
    await settled()
    old.resolve(history(30))
    await settled()
    expect(controller.getSnapshot()).toMatchObject({ days: 7, history: { data: { days: 7 }, loading: false } })
    controller.deactivate()
  })

  it.each([401, 403])('权限错误%s立即清空所有数据、同步中止其他请求并阻止继续刷新', async (status) => {
    const api = loaders()
    const pending = deferred<AdminDashboardTasks>()
    api.tasks.mockReturnValue(pending.promise)
    api.overview.mockRejectedValue(new ApiError(status, '权限已失效'))
    const controller = new DashboardController(1, api)
    controller.activate()
    controller.refreshAll()
    await settled()
    expect(controller.getSnapshot()).toMatchObject({ denied: true, overview: { data: null }, tasks: { data: null }, history: { data: null } })
    expect(api.tasks.mock.calls[0][0]?.aborted).toBe(true)
    pending.resolve(tasks())
    await settled()
    controller.refreshAll()
    expect(api.tasks).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().tasks.data).toBeNull()
  })

  it('卸载及同一实例再次激活使旧响应失效，新账号不复用旧账号数据', async () => {
    const api = loaders()
    const old = deferred<AdminDashboardOverview>()
    api.overview.mockReturnValueOnce(old.promise)
    const controller = new DashboardController(1, api)
    controller.activate()
    controller.refresh('overview')
    controller.deactivate()
    controller.activate()
    controller.refresh('overview')
    await settled()
    old.resolve(overview(999))
    await settled()
    expect(controller.getSnapshot().overview.data?.users).toBe(1)
    const another = new DashboardController(2, api)
    expect(another.getSnapshot().overview.data).toBeNull()
    controller.deactivate()
    another.deactivate()
  })
})

describe('管理员统计可见性轮询', () => {
  it('60秒刷新，hidden清除定时器，visible立即恢复，关闭/卸载彻底解绑', () => {
    vi.useFakeTimers()
    const target = new EventTarget()
    const visibility = {
      visibilityState: 'visible' as DocumentVisibilityState,
      addEventListener: (type: 'visibilitychange', listener: () => void) => target.addEventListener(type, listener),
      removeEventListener: (type: 'visibilitychange', listener: () => void) => target.removeEventListener(type, listener),
    }
    const refresh = vi.fn()
    const stop = startDashboardPolling(refresh, visibility)
    vi.advanceTimersByTime(59_999)
    expect(refresh).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(refresh).toHaveBeenCalledTimes(1)
    visibility.visibilityState = 'hidden'
    target.dispatchEvent(new Event('visibilitychange'))
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(180_000)
    expect(refresh).toHaveBeenCalledTimes(1)
    visibility.visibilityState = 'visible'
    target.dispatchEvent(new Event('visibilitychange'))
    expect(refresh).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(1)
    stop()
    expect(vi.getTimerCount()).toBe(0)
    target.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(60_000)
    expect(refresh).toHaveBeenCalledTimes(2)
  })
})
