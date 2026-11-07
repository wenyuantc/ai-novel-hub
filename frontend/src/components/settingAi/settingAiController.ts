import { ApiError, errorMessage } from '../../api/client'
import { aiSettingsNeedsReview, settingAiVisibilityApi, type SettingAiSettings } from '../../api/settingAiVisibility'
import type { SettingEntryRef } from '../../api/types'
import { rebaseSettingAiDraft, settingAiDirty, settingAiDraft, settingAiDraftError, settingAiPatch, settingLinkKey, type SettingAiDraft } from './settingAiDraft'

export interface SettingAiAccess { identity: string; canRead: boolean; canEdit: boolean }
type SettingsApi = Pick<typeof settingAiVisibilityApi, 'get' | 'update'>
interface State {
  base: SettingAiSettings | null
  draft: SettingAiDraft | null
  latest: SettingAiSettings | null
  reviewRequired: boolean
  pending: 'read' | 'write' | null
  error: string | null
  notice: string | null
}
interface Request { controller: AbortController; identity: string }
const initial = (): State => ({ base: null, draft: null, latest: null, reviewRequired: false, pending: null, error: null, notice: null })

/** 设置拥有独立的起始版本和草稿；对照前不重放失败或结果未知的保存。 */
export class SettingAiController {
  private state = initial()
  private listeners = new Set<() => void>()
  private active = false
  private request: Request | null = null

  constructor(private readonly projectId: number, private readonly entry: SettingEntryRef, private readonly api: SettingsApi,
    private readonly access: () => SettingAiAccess, private readonly changed: () => void) {}

  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  getSnapshot = () => this.state
  private update(patch: Partial<State>) {
    this.state = { ...this.state, ...patch }
    this.listeners.forEach((listener) => listener())
  }
  private abort() { this.request?.controller.abort(); this.request = null }
  private begin() {
    this.abort()
    const request = { controller: new AbortController(), identity: this.access().identity }
    this.request = request
    return request
  }
  private valid(request: Request) {
    const current = this.access()
    return this.active && this.request === request && !request.controller.signal.aborted && current.canRead && current.identity === request.identity
  }
  private invalidate() {
    const unknown = this.state.pending === 'write'
    this.abort()
    if (!this.access().canRead) { this.update(initial()); return }
    this.update({ pending: null, latest: null, reviewRequired: this.state.reviewRequired || unknown,
      notice: unknown ? '保存结果尚不能确认，草稿已保留，请核对最新设置。' : this.state.notice })
  }
  open = async () => { this.active = true; await this.refresh() }
  close = () => { this.active = false; this.invalidate() }
  dispose = () => { this.active = false; this.abort(); this.update(initial()) }
  accessChanged = () => { this.invalidate(); if (this.active) void this.refresh() }

  refresh = async () => {
    if (!this.active || !this.access().canRead || this.state.pending === 'write') return
    const ticket = this.begin()
    this.update({ pending: 'read', error: null, latest: null })
    try {
      const latest = await this.api.get(this.projectId, this.entry, ticket.controller.signal)
      if (!this.valid(ticket)) return
      const { base, draft, reviewRequired } = this.state
      if (base && draft && (reviewRequired || settingAiDirty(base, draft))) {
        const needsReview = reviewRequired || base.ai_visibility_revision !== latest.ai_visibility_revision
        this.update({ pending: null, latest: needsReview ? latest : null, reviewRequired: needsReview })
      } else {
        this.update({ base: latest, draft: settingAiDraft(latest), latest: null, pending: null, reviewRequired: false })
      }
    } catch (error) {
      if (!this.valid(ticket)) return
      const staleDraft = error instanceof ApiError && error.status === 409 && settingAiDirty(this.state.base, this.state.draft)
      this.update({ pending: null, error: errorMessage(error), reviewRequired: this.state.reviewRequired || staleDraft })
    }
  }
  edit = (patch: Partial<SettingAiDraft>) => {
    const { draft, pending } = this.state
    if (this.active && this.access().canEdit && draft && !pending) this.update({ draft: { ...draft, ...patch }, error: null, notice: null })
  }
  reset = () => {
    const { base, pending, reviewRequired } = this.state
    if (this.active && this.access().canEdit && base && !pending && !reviewRequired) {
      this.update({ draft: settingAiDraft(base), error: null, notice: null })
    }
  }
  acceptLatest = (keepDraft: boolean) => {
    const { base, draft, latest, pending } = this.state
    if (!this.active || !this.access().canEdit || !base || !draft || !latest || pending) return
    this.update({ base: latest, draft: keepDraft ? rebaseSettingAiDraft(base, draft, latest) : settingAiDraft(latest),
      latest: null, reviewRequired: false, error: null, notice: keepDraft ? '已采用最新版本，草稿仍保留，请核对后再保存。' : null })
  }
  save = async (seriesManaged = false) => {
    const { base, draft, pending, reviewRequired } = this.state
    if (!this.active || !this.access().canEdit || !base || !draft || pending || reviewRequired) return
    const effectiveDraft = seriesManaged ? { ...base, links: draft.links } : draft
    if (!settingAiDirty(base, effectiveDraft)) return
    const invalid = settingAiDraftError(effectiveDraft)
    if (invalid) { this.update({ error: invalid }); return }
    const ticket = this.begin()
    this.update({ pending: 'write', error: null, notice: null })
    try {
      if (!this.valid(ticket) || !this.access().canEdit) { this.invalidate(); return }
      const saved = await this.api.update(this.projectId, this.entry, settingAiPatch(base, effectiveDraft), ticket.controller.signal)
      if (!this.valid(ticket)) return
      if (!this.access().canEdit) { this.invalidate(); return }
      this.update({ base: saved, draft: settingAiDraft(saved), pending: null, reviewRequired: false, notice: 'AI 与提及设置已保存。' })
      this.changed()
    } catch (error) {
      if (!this.valid(ticket)) return
      this.update({ pending: null, error: errorMessage(error), reviewRequired: aiSettingsNeedsReview(error) })
    }
  }
}

/** 草稿仅留在当前详情页会话，不跨账号／作品共享。 */
export class SettingAiSession {
  private readonly entries = new Map<string, SettingAiController>()
  constructor(readonly projectId: number, private readonly access: () => SettingAiAccess,
    private readonly changed: () => void, private readonly api: SettingsApi = settingAiVisibilityApi) {}

  forEntry(entry: SettingEntryRef) {
    const key = settingLinkKey(entry)
    let controller = this.entries.get(key)
    if (!controller) {
      controller = new SettingAiController(this.projectId, { ...entry }, this.api, this.access, this.changed)
      this.entries.set(key, controller)
    }
    return controller
  }
  forget(entry: SettingEntryRef) {
    const key = settingLinkKey(entry)
    this.entries.get(key)?.dispose()
    this.entries.delete(key)
  }
  refresh = () => { this.entries.forEach((entry) => { void entry.refresh() }) }
  accessChanged = () => { this.entries.forEach((entry) => entry.accessChanged()) }
  dispose = () => { this.entries.forEach((entry) => entry.dispose()) }
}
