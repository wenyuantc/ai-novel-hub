import { adminDashboardApi } from '../../api/adminDashboard'
import { ApiError, errorMessage } from '../../api/client'
import type { AdminDashboardDays, AdminDashboardHistory, AdminDashboardOverview, AdminDashboardTasks } from '../../api/types'

interface DashboardData {
  overview: AdminDashboardOverview
  tasks: AdminDashboardTasks
  history: AdminDashboardHistory
}
export type DashboardSection = keyof DashboardData
export interface DashboardResource<T> {
  data: T | null
  loading: boolean
  error: string | null
}
type DashboardState = { [K in DashboardSection]: DashboardResource<DashboardData[K]> } & {
  days: AdminDashboardDays
  autoRefresh: boolean
  denied: boolean
}
type DashboardLoaders = typeof adminDashboardApi
const empty = <T>(): DashboardResource<T> => ({ data: null, loading: false, error: null })

/** 页面专用请求所有者：三个区块独立重试，换范围/账号/权限后同步失效旧响应。 */
export class DashboardController {
  readonly owner: number | null
  private readonly loaders: DashboardLoaders
  private listeners = new Set<() => void>()
  private active = false
  private inFlight: Record<DashboardSection, AbortController | null> = { overview: null, tasks: null, history: null }
  private generation: Record<DashboardSection, number> = { overview: 0, tasks: 0, history: 0 }
  private state: DashboardState = {
    overview: empty(), tasks: empty(), history: empty(), days: 30, autoRefresh: true, denied: false,
  }

  constructor(owner: number | null, loaders: DashboardLoaders = adminDashboardApi) {
    this.owner = owner
    this.loaders = loaders
  }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  activate() { this.active = true }

  deactivate() {
    this.active = false
    this.cancel('overview')
    this.cancel('tasks')
    this.cancel('history')
  }

  setDays(days: AdminDashboardDays) {
    if (days === this.state.days || this.state.denied) return
    this.cancel('history')
    this.state = { ...this.state, days, history: empty() }
    this.emit()
    this.refresh('history')
  }

  setAutoRefresh(autoRefresh: boolean) {
    this.state = { ...this.state, autoRefresh }
    this.emit()
  }

  refreshAll = () => {
    this.refresh('overview')
    this.refresh('tasks')
    this.refresh('history')
  }

  refresh(section: DashboardSection) {
    if (section === 'overview') void this.load('overview', (signal) => this.loaders.overview(signal))
    else if (section === 'tasks') void this.load('tasks', (signal) => this.loaders.tasks(signal))
    else {
      const days = this.state.days
      void this.load('history', (signal) => this.loaders.history(days, signal))
    }
  }

  private emit() { this.listeners.forEach((listener) => listener()) }

  private update<K extends DashboardSection>(section: K, resource: DashboardResource<DashboardData[K]>) {
    this.state = { ...this.state, [section]: resource }
    this.emit()
  }

  private setStatus(section: DashboardSection, patch: { loading: boolean; error?: string | null }) {
    this.state = { ...this.state, [section]: { ...this.state[section], ...patch } }
    this.emit()
  }

  private cancel(section: DashboardSection) {
    this.generation[section] += 1
    this.inFlight[section]?.abort()
    this.inFlight[section] = null
  }

  private deny(message: string) {
    this.deactivate()
    this.state = {
      ...this.state, denied: true,
      overview: { ...empty(), error: message }, tasks: { ...empty(), error: message }, history: { ...empty(), error: message },
    }
    this.emit()
  }

  private async load<K extends DashboardSection>(section: K, loader: (signal: AbortSignal) => Promise<DashboardData[K]>) {
    if (!this.active || this.state.denied || this.inFlight[section]) return
    const controller = new AbortController()
    const generation = ++this.generation[section]
    const valid = () => this.active && !this.state.denied && this.generation[section] === generation && !controller.signal.aborted
    this.inFlight[section] = controller
    this.setStatus(section, { loading: true })
    try {
      const data = await loader(controller.signal)
      if (valid()) this.update(section, { data, loading: false, error: null })
    } catch (err: unknown) {
      if (!valid()) return
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) this.deny(errorMessage(err))
      else this.setStatus(section, { loading: false, error: errorMessage(err) })
    } finally {
      if (this.inFlight[section] === controller) this.inFlight[section] = null
    }
  }
}

interface DashboardVisibility {
  readonly visibilityState: DocumentVisibilityState
  addEventListener(type: 'visibilitychange', listener: () => void): void
  removeEventListener(type: 'visibilitychange', listener: () => void): void
}

/** 只调度可见页面；关闭开关/卸载调用返回值即可彻底移除计时器与监听。 */
export function startDashboardPolling(refresh: () => void, visibility: DashboardVisibility, interval = 60_000) {
  let timer: ReturnType<typeof setInterval> | undefined
  const stopTimer = () => { if (timer !== undefined) clearInterval(timer); timer = undefined }
  const schedule = () => {
    stopTimer()
    if (visibility.visibilityState === 'visible') timer = setInterval(refresh, interval)
  }
  const onVisibility = () => {
    if (visibility.visibilityState === 'visible') refresh()
    schedule()
  }
  schedule()
  visibility.addEventListener('visibilitychange', onVisibility)
  return () => { stopTimer(); visibility.removeEventListener('visibilitychange', onVisibility) }
}
