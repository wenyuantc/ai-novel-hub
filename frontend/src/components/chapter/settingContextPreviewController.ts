import { errorMessage } from '../../api/client'
import type { ChapterSettingContext } from '../../api/types'

interface PreviewSnapshot {
  loading: boolean
  data: ChapterSettingContext | null
  error: string | null
}

type FetchPreview = (projectId: number, chapterId: number, signal: AbortSignal) => Promise<ChapterSettingContext>

/** A controller is scoped to one project, chapter and authorization identity. */
export class SettingContextPreviewController {
  private active = false
  private generation = 0
  private request: AbortController | null = null
  private snapshot: PreviewSnapshot = { loading: false, data: null, error: null }
  private listeners = new Set<() => void>()

  constructor(
    private readonly projectId: number,
    private readonly chapterId: number,
    private readonly fetchPreview: FetchPreview,
    private readonly canReadNow: () => boolean,
  ) {}

  getSnapshot = () => this.snapshot

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  activate() {
    this.active = true
  }

  deactivate() {
    this.active = false
    this.cancelRequest()
    this.reset()
  }

  async load() {
    if (!this.active) return
    if (!this.canReadNow()) {
      this.cancelRequest()
      this.reset()
      return
    }

    this.cancelRequest()
    const generation = this.generation
    const request = new AbortController()
    this.request = request
    this.update({ loading: true, data: null, error: null })

    try {
      const data = await this.fetchPreview(this.projectId, this.chapterId, request.signal)
      if (!this.mayPublish(generation, request)) return
      if (data.position.chapter_id !== this.chapterId) {
        this.update({ loading: false, data: null, error: '返回的章节与当前章节不一致，请重试。' })
        return
      }
      this.update({ loading: false, data, error: null })
    } catch (error) {
      if (!this.mayPublish(generation, request)) return
      this.update({ loading: false, data: null, error: errorMessage(error) })
    } finally {
      if (this.request === request) this.request = null
    }
  }

  private mayPublish(generation: number, request: AbortController) {
    if (!this.active || generation !== this.generation || request.signal.aborted) return false
    if (!this.canReadNow()) {
      this.cancelRequest()
      this.reset()
      return false
    }
    return true
  }

  private cancelRequest() {
    this.generation += 1
    this.request?.abort()
    this.request = null
  }

  private reset() {
    this.update({ loading: false, data: null, error: null })
  }

  private update(snapshot: PreviewSnapshot) {
    this.snapshot = snapshot
    this.listeners.forEach((listener) => listener())
  }
}
