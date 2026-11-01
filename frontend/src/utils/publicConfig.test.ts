import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuthConfig } from '../api/types'
import { deferred } from '../components/systemSettings/systemSettingsTestFixtures'
import { PublicConfigController, startPublicConfigPolling } from './publicConfig'
import { startVisiblePolling } from './visiblePolling'

afterEach(() => { vi.useRealTimers() })
function surfaces() {
  const events = new EventTarget()
  const focus = new EventTarget()
  const visibility = {
    visibilityState: 'visible' as DocumentVisibilityState,
    addEventListener: (type: 'visibilitychange', listener: () => void) => events.addEventListener(type, listener),
    removeEventListener: (type: 'visibilitychange', listener: () => void) => events.removeEventListener(type, listener),
  }
  return { events, focus, visibility }
}

describe('公开开关请求所有者', () => {
  it('慢请求single-flight，保存应用后旧响应不能倒退新值', async () => {
    const response = deferred<AuthConfig>()
    const loader = vi.fn(() => response.promise)
    const accept = vi.fn()
    const controller = new PublicConfigController(loader, accept)
    const first = controller.refresh()
    expect(controller.refresh()).toBe(first)
    expect(loader).toHaveBeenCalledTimes(1)
    controller.apply({ registration_enabled: false, mcp_enabled: false })
    response.resolve({ registration_enabled: true, mcp_enabled: true })
    await first
    expect(accept).toHaveBeenCalledExactlyOnceWith({ registration_enabled: false, mcp_enabled: false })
    expect(loader.mock.calls[0]).toHaveLength(1)
  })
  it('刷新错误保留最近值，后续可重试，旧后端省略MCP由store兼容', async () => {
    const loader = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ registration_enabled: false })
    const accept = vi.fn()
    const controller = new PublicConfigController(loader, accept)
    await controller.refresh()
    expect(accept).not.toHaveBeenCalled()
    await controller.refresh()
    expect(accept).toHaveBeenCalledExactlyOnceWith({ registration_enabled: false })
  })
  it('loader同步抛错也释放single-flight，不会永久停止轮询', async () => {
    const loader = vi.fn().mockImplementationOnce(() => { throw new Error('sync failure') }).mockResolvedValueOnce({ registration_enabled: false })
    const accept = vi.fn()
    const controller = new PublicConfigController(loader, accept)
    await controller.refresh(); await controller.refresh()
    expect(loader).toHaveBeenCalledTimes(2)
    expect(accept).toHaveBeenCalledExactlyOnceWith({ registration_enabled: false })
  })
  it('卸载取消的请求即使不尊重AbortSignal也不落地，重新加载可进行', async () => {
    const old = deferred<AuthConfig>()
    const loader = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce({ registration_enabled: false, mcp_enabled: false })
    const accept = vi.fn()
    const controller = new PublicConfigController(loader, accept)
    const first = controller.refresh()
    controller.cancel()
    await controller.refresh()
    old.resolve({ registration_enabled: true })
    await first
    expect(accept).toHaveBeenCalledExactlyOnceWith({ registration_enabled: false, mcp_enabled: false })
  })
})

describe('可见页面同步', () => {
  it('30秒、隐藏暂停、回前台立即同步，visibility/focus不重复，清理后不再刷新', () => {
    vi.useFakeTimers(); vi.setSystemTime(0)
    const { visibility, events, focus } = surfaces()
    const refresh = vi.fn()
    const stop = startPublicConfigPolling(refresh, visibility, focus)
    vi.advanceTimersByTime(29_999); expect(refresh).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1); expect(refresh).toHaveBeenCalledTimes(1)
    visibility.visibilityState = 'hidden'; events.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(90_000); focus.dispatchEvent(new Event('focus')); expect(refresh).toHaveBeenCalledTimes(1)
    visibility.visibilityState = 'visible'; events.dispatchEvent(new Event('visibilitychange')); focus.dispatchEvent(new Event('focus'))
    expect(refresh).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(30_000); expect(refresh).toHaveBeenCalledTimes(3)
    stop(); vi.advanceTimersByTime(60_000); focus.dispatchEvent(new Event('focus')); events.dispatchEvent(new Event('visibilitychange'))
    expect(refresh).toHaveBeenCalledTimes(3)
  })
  it('StrictMode清理重装后只有一套定时器，旧后台60/5秒兼容', () => {
    vi.useFakeTimers()
    const { visibility, focus } = surfaces()
    const refresh = vi.fn()
    startPublicConfigPolling(refresh, visibility, focus)()
    const stop = startPublicConfigPolling(refresh, visibility, focus)
    vi.advanceTimersByTime(30_000); expect(refresh).toHaveBeenCalledTimes(1); stop()
    for (const interval of [5_000, 60_000]) {
      refresh.mockClear()
      const close = startVisiblePolling(refresh, visibility, interval)
      vi.advanceTimersByTime(interval - 1); expect(refresh).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1); expect(refresh).toHaveBeenCalledTimes(1); close()
    }
  })
  it('系统时钟回拨不会永久暂停同步', () => {
    vi.useFakeTimers(); vi.setSystemTime(10_000)
    const { visibility, focus } = surfaces()
    const refresh = vi.fn()
    const stop = startPublicConfigPolling(refresh, visibility, focus)
    focus.dispatchEvent(new Event('focus')); expect(refresh).toHaveBeenCalledTimes(1)
    vi.setSystemTime(0)
    focus.dispatchEvent(new Event('focus')); expect(refresh).toHaveBeenCalledTimes(2)
    stop()
  })
})
