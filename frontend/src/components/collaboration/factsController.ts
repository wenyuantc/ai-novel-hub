import { ApiError, currentRequestGeneration, errorMessage } from '../../api/client'

export interface FactsState<T> {
  data: T | null
  loading: boolean
  busy: boolean
  stale: boolean
  accessEpoch: number
  error: string | null
  notice: string | null
}

export function emptyFacts<T>(): FactsState<T> {
  return { data: null, loading: false, busy: false, stale: false, accessEpoch: 0, error: null, notice: null }
}

/** 仅服务端事实可解锁下一次写入；任何写入都不自动重放。 */
export class FactsController<T> {
  private state = emptyFacts<T>()
  private alive = true
  private seq = 0
  private request: AbortController | null = null
  private readonly generation = currentRequestGeneration()

  constructor(
    private readonly read: (signal: AbortSignal) => Promise<T>,
    private readonly validIdentity: () => boolean,
    private readonly publish: (state: FactsState<T>) => void,
    private readonly onSettled?: () => void,
  ) {}

  private valid(seq: number): boolean {
    return this.alive && seq === this.seq && this.generation === currentRequestGeneration() && this.validIdentity()
  }

  isCurrent(): boolean { return this.valid(this.seq) }

  private set(patch: Partial<FactsState<T>>) {
    this.state = { ...this.state, ...patch }
    this.publish(this.state)
  }

  dispose() {
    this.alive = false
    this.seq += 1
    this.request?.abort()
    this.request = null
  }

  private async readFacts(seq: number, signal: AbortSignal): Promise<boolean> {
    try {
      const data = await this.read(signal)
      if (!this.valid(seq)) return false
      this.set({ data, loading: false, stale: false, error: null })
      return true
    } catch (error) {
      if (!this.valid(seq)) return false
      const denied = error instanceof ApiError && [401, 403, 404].includes(error.status)
      this.set({ ...(denied ? { data: null, accessEpoch: this.state.accessEpoch + 1 } : {}),
        loading: false, stale: true, error: `无法刷新状态：${errorMessage(error)}` })
      return false
    }
  }

  async refresh(): Promise<void> {
    if (!this.valid(this.seq) || this.state.busy) return
    this.request?.abort()
    const controller = new AbortController()
    this.request = controller
    const seq = ++this.seq
    this.set({ loading: true, error: null })
    await this.readFacts(seq, controller.signal)
  }

  async mutate(action: (signal: AbortSignal) => Promise<unknown>, success: string): Promise<boolean> {
    if (!this.valid(this.seq) || this.state.busy || this.state.loading || this.state.stale || this.state.data === null) return false
    this.request?.abort()
    const controller = new AbortController()
    this.request = controller
    const seq = ++this.seq
    this.set({ busy: true, stale: true, error: null, notice: null })
    let succeeded = false
    let notice = success
    try {
      await action(controller.signal)
      succeeded = true
    } catch (error) {
      if (!this.valid(seq)) return false
      notice = error instanceof ApiError && error.status === 409
        ? '状态已变化，请核对刷新后的信息，再决定是否重新操作。'
        : error instanceof ApiError && error.status >= 400 && error.status < 500
          ? `操作未完成：${errorMessage(error)}；请核对最新状态。`
          : '请求结果尚未确认，请核对刷新后的信息，再决定是否重新操作。'
    }
    if (!this.valid(seq)) return false
    this.set({ loading: true, notice })
    await this.readFacts(seq, controller.signal)
    if (!this.valid(seq)) return false
    this.set({ busy: false })
    this.onSettled?.()
    return this.valid(seq) && succeeded
  }
}
