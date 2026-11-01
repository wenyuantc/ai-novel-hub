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
