import type { AuthConfig } from '../api/types'
import type { PollingVisibility } from './visiblePolling'
import { startVisiblePolling } from './visiblePolling'

/** 公开开关的请求所有者；保存后应用的新配置使迟到的旧请求失效。 */
export class PublicConfigController {
  private generation = 0
  private current: { controller: AbortController; promise: Promise<void> } | null = null

  constructor(private readonly loader: (signal: AbortSignal) => Promise<AuthConfig>, private readonly accept: (value: AuthConfig) => void) {}

  refresh = (): Promise<void> => {
    if (this.current) return this.current.promise
    const controller = new AbortController()
    const generation = ++this.generation
    const pending = { controller, promise: Promise.resolve() }
    this.current = pending
    pending.promise = (async () => {
      try {
        const config = await this.loader(controller.signal)
        if (generation === this.generation && !controller.signal.aborted) this.accept(config)
      } catch {
        // 后台配置刷新失败保留最近成功值；服务端始终执行真实权限与开关。
      } finally {
        if (this.current?.controller === controller) this.current = null
      }
    })()
    return pending.promise
  }

  cancel = () => {
    this.generation += 1
    this.current?.controller.abort()
    this.current = null
  }

  apply = (config: AuthConfig) => { this.cancel(); this.accept(config) }
}

interface FocusTarget {
  addEventListener(type: 'focus', listener: () => void): void
  removeEventListener(type: 'focus', listener: () => void): void
}

/** 可见时30秒同步，切回前台立即同步；同一轮visibility/focus事件去重。 */
export function startPublicConfigPolling(refresh: () => void, visibility: PollingVisibility, focus: FocusTarget) {
  let lastRefresh = -Infinity
  const sync = () => {
    const elapsed = Date.now() - lastRefresh
    if (visibility.visibilityState !== 'visible' || (elapsed >= 0 && elapsed < 500)) return
    lastRefresh = Date.now()
    refresh()
  }
  const stop = startVisiblePolling(sync, visibility, 30_000)
  focus.addEventListener('focus', sync)
  return () => { stop(); focus.removeEventListener('focus', sync) }
}
