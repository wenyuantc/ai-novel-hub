import { afterEach, describe, expect, it, vi } from 'vitest'
import { COLLABORATOR_ACCESS_POLL_MS, startCollaboratorAccessPolling } from './visiblePolling'

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

describe('协作者作品权限前台复核', () => {
  it('协作者：可见时按间隔复核，切回前台立即复核，隐藏时暂停，清理后停止', () => {
    vi.useFakeTimers()
    const { events, focus, visibility } = surfaces()
    const refresh = vi.fn()
    const stop = startCollaboratorAccessPolling(true, refresh, visibility, focus)
    expect(stop).toBeTypeOf('function')
    vi.advanceTimersByTime(COLLABORATOR_ACCESS_POLL_MS - 1); expect(refresh).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1); expect(refresh).toHaveBeenCalledTimes(1)
    visibility.visibilityState = 'hidden'; events.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(COLLABORATOR_ACCESS_POLL_MS * 3); expect(refresh).toHaveBeenCalledTimes(1)
    visibility.visibilityState = 'visible'; events.dispatchEvent(new Event('visibilitychange'))
    focus.dispatchEvent(new Event('focus'))
    expect(refresh).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(600); focus.dispatchEvent(new Event('focus'))
    expect(refresh).toHaveBeenCalledTimes(3)
    stop?.()
    focus.dispatchEvent(new Event('focus'))
    vi.advanceTimersByTime(COLLABORATOR_ACCESS_POLL_MS * 3)
    expect(refresh).toHaveBeenCalledTimes(3)
  })

  it('作者与纯管理员不轮询', () => {
    vi.useFakeTimers()
    const { focus, visibility } = surfaces()
    const refresh = vi.fn()
    expect(startCollaboratorAccessPolling(false, refresh, visibility, focus)).toBeUndefined()
    focus.dispatchEvent(new Event('focus'))
    vi.advanceTimersByTime(COLLABORATOR_ACCESS_POLL_MS * 3)
    expect(refresh).not.toHaveBeenCalled()
  })
})
