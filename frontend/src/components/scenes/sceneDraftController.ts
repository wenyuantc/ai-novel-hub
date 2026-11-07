import { ApiError, errorMessage } from '../../api/client'
import type { Scene, ScenePage, SceneUpdate } from '../../api/types'

export interface SceneDraft { title: string; content: string; summary: string }
export interface SceneDraftSnapshot {
  scene: Scene | null
  draft: SceneDraft
  page: ScenePage | null
  loading: boolean
  saving: boolean
  error: string | null
  conflict: Scene | null
  externalChange: boolean
}
export interface SceneDraftApi {
  list: (signal: AbortSignal) => Promise<ScenePage>
  get: (sid: number, signal?: AbortSignal) => Promise<Scene>
  update: (sid: number, body: SceneUpdate) => Promise<Scene>
}
const empty = (): SceneDraft => ({ title: '', content: '', summary: '' })
const draftOf = (scene: Scene): SceneDraft => ({ title: scene.title, content: scene.content, summary: scene.summary })
export const sceneDraftDirty = (snapshot: SceneDraftSnapshot) => snapshot.scene !== null &&
  (snapshot.draft.title !== snapshot.scene.title || snapshot.draft.content !== snapshot.scene.content || snapshot.draft.summary !== snapshot.scene.summary)

/** 一章一授权身份的草稿控制器；保存失败不切场景，冲突只展示，核对前不替换 revision。 */
export class SceneDraftController {
  private active = false
  private generation = 0
  private request: AbortController | null = null
  private pendingRefresh = false
  private listeners = new Set<() => void>()
  private snapshot: SceneDraftSnapshot = { scene: null, draft: empty(), page: null, loading: true, saving: false, error: null, conflict: null, externalChange: false }
  constructor(private readonly chapterId: number, private readonly api: SceneDraftApi, private readonly canRead: () => boolean, private readonly canEdit: () => boolean) {}
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.active = true }
  deactivate() { this.active = false; this.generation += 1; this.request?.abort(); this.request = null; this.pendingRefresh = false }
  private publish(patch: Partial<SceneDraftSnapshot>) { this.snapshot = { ...this.snapshot, ...patch }; this.listeners.forEach((fn) => fn()) }
  private valid(generation: number) { return this.active && generation === this.generation && this.canRead() }
  change(patch: Partial<SceneDraft>) { if (this.active && this.canEdit() && !this.snapshot.saving) this.publish({ draft: { ...this.snapshot.draft, ...patch } }) }
  async load(preferredId?: number, preserveDraft = true): Promise<boolean> {
    if (!this.active || !this.canRead()) return false
    if (this.snapshot.saving) { this.pendingRefresh = true; return false }
    this.request?.abort()
    const request = new AbortController()
    this.request = request
    const generation = ++this.generation
    const previousId = this.snapshot.scene?.id
    this.publish({ loading: true, error: null })
    try {
      const page = await this.api.list(request.signal)
      if (!this.valid(generation)) return false
      if (preferredId !== undefined && !page.items.some((item) => item.id === preferredId)) throw new Error('指定场景已退场或不属于当前章节，请核对来源；不会自动定位到同序号的其他场景。')
      if (preserveDraft && previousId && (sceneDraftDirty(this.snapshot) || this.snapshot.conflict !== null) && !page.items.some((item) => item.id === previousId)) {
        this.publish({ page, loading: false, externalChange: true, error: '当前场景已退场或不可用，本地草稿仍保留，请先复制需要的内容；不会自动把草稿写入其他场景。' })
        return false
      }
      const sid = [preferredId, previousId, page.items[0]?.id].find((id) => id !== undefined && page.items.some((item) => item.id === id))
      if (sid === undefined) throw new Error('该章节缺少初始场景，请联系管理员检查迁移或建章入口。')
      const scene = await this.api.get(sid, request.signal)
      if (!this.valid(generation)) return false
      if (scene.chapter_id !== this.chapterId || scene.id !== sid) throw new Error('返回的场景与当前章节不一致，请重试。')
      if (page.content_revision !== scene.content_revision || page.planning_revision !== scene.planning_revision) throw new Error('加载期间场景版本已变化，请重新加载后核对。')
      if (preserveDraft && previousId === sid && (sceneDraftDirty(this.snapshot) || this.snapshot.conflict !== null)) {
        const sameContentRevision = scene.content_revision === this.snapshot.scene?.content_revision
        this.publish({ page, loading: false, ...(sameContentRevision ? { scene } : {}), ...(this.snapshot.conflict ? { conflict: scene } : {}), externalChange: !sameContentRevision })
      } else {
        this.publish({ page, scene, draft: draftOf(scene), loading: false, conflict: null, externalChange: false })
      }
      return true
    } catch (error) {
      if (this.valid(generation)) this.publish({ loading: false, error: errorMessage(error) })
      return false
    } finally { if (this.request === request) this.request = null }
  }
  async save(): Promise<Scene | null> {
    const { scene, draft, saving, conflict, externalChange } = this.snapshot
    if (!scene || saving || conflict || externalChange || !this.active || !this.canEdit()) return null
    if (!sceneDraftDirty(this.snapshot)) return scene
    const generation = this.generation
    this.publish({ saving: true, error: null })
    try {
      const saved = await this.api.update(scene.id, { ...draft, expected_content_revision: scene.content_revision })
      if (!this.valid(generation) || !this.canEdit() || this.snapshot.scene?.id !== scene.id) return null
      this.publish({ scene: saved, draft: draftOf(saved), externalChange: false, page: this.snapshot.page ? {
        ...this.snapshot.page, content_revision: saved.content_revision, planning_revision: saved.planning_revision,
        items: this.snapshot.page.items.map((item) => item.id === saved.id ? saved : { ...item, content_revision: saved.content_revision, planning_revision: saved.planning_revision }),
      } : null })
      return saved
    } catch (error) {
      if (!this.valid(generation) || !this.canEdit()) return null
      this.publish({ error: errorMessage(error) })
      if (error instanceof ApiError && error.status === 409) await this.reviewLatest()
      return null
    } finally {
      if (this.valid(generation)) {
        this.publish({ saving: false })
        if (this.pendingRefresh) { this.pendingRefresh = false; void this.load() }
      }
    }
  }
  async select(sid: number): Promise<boolean> {
    if (this.snapshot.saving) return false
    if (this.snapshot.scene?.id === sid) return true
    if (sceneDraftDirty(this.snapshot) && !(await this.save())) return false
    return this.load(sid, false)
  }
  async reviewLatest(): Promise<void> {
    const sid = this.snapshot.scene?.id
    const generation = this.generation
    if (!sid || !this.active || !this.canRead()) return
    try {
      const latest = await this.api.get(sid)
      if (this.valid(generation) && latest.chapter_id === this.chapterId && this.snapshot.scene?.id === sid) this.publish({ conflict: latest })
    } catch (error) { if (this.valid(generation)) this.publish({ error: errorMessage(error), externalChange: true }) }
  }
  /** 作者看过对比后显式换用最新版本；本地输入不变，下一次保存仍由作者触发。 */
  acknowledgeLatest() {
    if (!this.snapshot.conflict || !this.active || !this.canEdit()) return
    this.publish({ scene: this.snapshot.conflict, conflict: null, externalChange: false, error: null })
  }
}
