export interface PollingVisibility {
  readonly visibilityState: DocumentVisibilityState
  addEventListener(type: 'visibilitychange', listener: () => void): void
  removeEventListener(type: 'visibilitychange', listener: () => void): void
}

/** 只轮询可见页面，恢复可见时立即刷新；调用返回值完整移除计时器与监听。 */
export function startVisiblePolling(refresh: () => void, visibility: PollingVisibility, interval: number) {
  let timer: ReturnType<typeof setInterval> | undefined
  const stop = () => { if (timer !== undefined) clearInterval(timer); timer = undefined }
  const schedule = () => {
    stop()
    if (visibility.visibilityState === 'visible') timer = setInterval(refresh, interval)
  }
  const onVisibility = () => {
    if (visibility.visibilityState === 'visible') refresh()
    schedule()
  }
  schedule()
  visibility.addEventListener('visibilitychange', onVisibility)
  return () => { stop(); visibility.removeEventListener('visibilitychange', onVisibility) }
}

export interface FocusTarget {
  addEventListener(type: 'focus', listener: () => void): void
  removeEventListener(type: 'focus', listener: () => void): void
}

/** 可见时按间隔刷新，切回前台（可见或聚焦）立即刷新；同一轮 visibility/focus 事件 500ms 内去重。 */
export function startForegroundPolling(refresh: () => void, visibility: PollingVisibility, focus: FocusTarget, interval: number) {
  let lastRefresh = -Infinity
  const sync = () => {
    const elapsed = Date.now() - lastRefresh
    if (visibility.visibilityState !== 'visible' || (elapsed >= 0 && elapsed < 500)) return
    lastRefresh = Date.now()
    refresh()
  }
  const stop = startVisiblePolling(sync, visibility, interval)
  focus.addEventListener('focus', sync)
  return () => { stop(); focus.removeEventListener('focus', sync) }
}

/**
 * 协作者停留在作品页时复核作品权限快照：作者单独调整角色或作者模型授权（allow_owner_ai）不会让本页请求失败，
 * 没有 403/404 触发刷新，只能靠前台轮询发现。作者与纯管理员不轮询。
 */
export const COLLABORATOR_ACCESS_POLL_MS = 15_000
export function startCollaboratorAccessPolling(
  isCollaborator: boolean, refresh: () => void, visibility: PollingVisibility, focus: FocusTarget,
): (() => void) | undefined {
  return isCollaborator ? startForegroundPolling(refresh, visibility, focus, COLLABORATOR_ACCESS_POLL_MS) : undefined
}
