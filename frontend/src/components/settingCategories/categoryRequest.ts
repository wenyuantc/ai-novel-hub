import { errorMessage } from '../../api/client'

export interface CategoryRequestState<T> { data: T | null; loading: boolean; error: string | null }

/** 每次重试都重新读取；请求身份不匹配的结果不能进入页面。 */
export class CategoryRequest<T> {
  private state: CategoryRequestState<T> = { data: null, loading: false, error: null }
  private listeners = new Set<() => void>()
  private generation = 0
  private active = false
  private request: AbortController | null = null
  constructor(private readonly load: (signal: AbortSignal) => Promise<T>, private readonly allowed = () => true, private readonly identity = () => '') {}
  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private update(state: CategoryRequestState<T>) { this.state = state; this.listeners.forEach((listener) => listener()) }
  activate() { this.active = true }
  deactivate() { this.active = false; this.invalidate() }
  invalidate = () => { this.generation += 1; this.request?.abort(); this.request = null; this.update({ data: null, loading: false, error: null }) }
  refresh = async () => {
    this.invalidate()
    if (!this.active || !this.allowed()) return
    const generation = this.generation
    const identity = this.identity()
    const request = new AbortController()
    this.request = request
    const valid = () => this.active && generation === this.generation && !request.signal.aborted && identity === this.identity() && this.allowed()
    this.update({ data: null, loading: true, error: null })
    try {
      const data = await this.load(request.signal)
      if (valid()) this.update({ data, loading: false, error: null })
      else if (generation === this.generation) this.invalidate()
    } catch (error) {
      if (valid()) this.update({ data: null, loading: false, error: errorMessage(error) })
      else if (generation === this.generation) this.invalidate()
    }
  }
}
