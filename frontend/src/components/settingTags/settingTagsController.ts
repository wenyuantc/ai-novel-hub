import { ApiError, errorMessage } from '../../api/client'
import { settingCategoriesApi, settingCategoryConflictScope } from '../../api/settingCategories'
import type { SettingEntryRef, SettingTagsOut } from '../../api/types'
import { normalizeSettingTags, sameTags, tagSnapshot } from './tagDraft'

type TagsApi = Pick<typeof settingCategoriesApi, 'getTags' | 'updateTags' | 'suggestTags'>
export interface SettingTagsAccess { identity: string; canRead: boolean; canEdit: boolean }
export interface SettingTagsState {
  base: SettingTagsOut | null
  original: SettingTagsOut | null
  draft: string[]
  search: string
  latest: SettingTagsOut | null
  reviewRequired: boolean
  reviewed: boolean
  loading: boolean
  saving: boolean
  reviewing: boolean
  error: string | null
  draftError: string | null
  notice: string | null
  suggestions: string[]
  suggesting: boolean
  suggestionError: string | null
}
interface Ticket { request: AbortController; identity: string }
type RequestKind = 'read' | 'write' | 'suggestion'
const emptyState = (): SettingTagsState => ({ base: null, original: null, draft: [], search: '', latest: null, reviewRequired: false, reviewed: false,
  loading: false, saving: false, reviewing: false, error: null, draftError: null, notice: null, suggestions: [], suggesting: false, suggestionError: null })
const OPEN_ACCESS: SettingTagsAccess = { identity: 'local', canRead: true, canEdit: true }

export function tagsDraftDirty(state: SettingTagsState): boolean {
  if (!state.base) return false
  try { return !sameTags(normalizeSettingTags([...state.draft, state.search]), state.base.setting_tags) }
  catch { return true }
}

/** 每个条目独立持有原快照、CAS 版本和草稿；页面卸载才释放，关闭详情只暂停请求。 */
export class SettingTagsController {
  private state = emptyState()
  private listeners = new Set<() => void>()
  private active = false
  private requests: Record<RequestKind, Ticket | null> = { read: null, write: null, suggestion: null }

  constructor(private readonly pid: number, private readonly entry: SettingEntryRef, private readonly api: TagsApi,
    private readonly access: () => SettingTagsAccess, private readonly changed: (entry: SettingEntryRef) => void) {}

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private update(patch: Partial<SettingTagsState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  private readFailure(error: unknown) {
    if (error instanceof ApiError && error.status === 404) this.clear()
    this.update({ loading: false, reviewing: false, error: errorMessage(error) })
  }
  private cancel(kind: RequestKind) { this.requests[kind]?.request.abort(); this.requests[kind] = null }
  private cancelAll() { this.cancel('read'); this.cancel('write'); this.cancel('suggestion') }
  private allowed() {
    if (!this.access().canRead) { this.clear(); return false }
    return this.active
  }
  private begin(kind: RequestKind): Ticket {
    this.cancel(kind)
    const ticket = { request: new AbortController(), identity: this.access().identity }
    this.requests[kind] = ticket
    return ticket
  }
  private valid(ticket: Ticket, kind: RequestKind) {
    if (!this.active || this.requests[kind] !== ticket || ticket.request.signal.aborted) return false
    if (!this.allowed()) return false
    if (this.access().identity !== ticket.identity) { this.invalidate(); return false }
    return true
  }

  open = async () => { this.active = true; await this.refresh() }
  close = () => {
    this.active = false
    this.invalidate(false)
  }
  clear = () => { this.cancelAll(); this.state = emptyState(); this.listeners.forEach((listener) => listener()) }
  invalidate = (requireReview = true) => {
    const interrupted = this.state.saving
    this.cancelAll()
    if (!this.access().canRead) { this.clear(); return }
    const reviewRequired = this.state.reviewRequired || (requireReview && tagsDraftDirty(this.state)) || interrupted
    this.update({ loading: false, saving: false, reviewing: false, suggesting: false, suggestions: [], latest: null, reviewed: false, reviewRequired,
      notice: interrupted ? '保存结果尚不能确认，标签草稿已保留，请先读取最新标签并对照。' : this.state.notice })
  }
  accessChanged = () => { this.invalidate(); if (this.active) void this.refresh() }
  refreshIfOpen = () => { if (this.active) { this.invalidate(); void this.refresh() } }

  refresh = async () => {
    if (!this.allowed() || this.state.saving || this.state.reviewing) return
    const ticket = this.begin('read')
    this.update({ loading: true, error: null })
    try {
      const result = await this.api.getTags(this.pid, this.entry, ticket.request.signal)
      if (!this.valid(ticket, 'read')) return
      const latest = tagSnapshot(result, this.entry)
      const base = this.state.base
      // 读取期间发生的输入也必须保留，不能使用请求开始时的 dirty 快照。
      if (base && (tagsDraftDirty(this.state) || this.state.reviewRequired)) {
        const changed = latest.categories_revision !== base.categories_revision || !sameTags(latest.setting_tags, base.setting_tags)
        this.update({ loading: false, latest, reviewed: false, reviewRequired: this.state.reviewRequired || changed })
      } else {
        this.update({ loading: false, base: latest, original: tagSnapshot(latest, this.entry), draft: [...latest.setting_tags], latest: null, reviewed: false, reviewRequired: false })
      }
    } catch (error) { if (this.valid(ticket, 'read')) this.readFailure(error) }
  }

  edit = (tags: string[]) => {
    if (!this.allowed() || !this.access().canEdit || this.state.saving || !this.state.base) return
    this.cancel('suggestion')
    try { this.update({ draft: normalizeSettingTags(tags), search: '', draftError: null, error: null, notice: null, suggestions: [], suggesting: false }) }
    catch (error) { this.update({ draft: [...tags], search: '', draftError: errorMessage(error), notice: null, suggestions: [], suggesting: false }) }
  }
  setSearch = (search: string) => {
    if (!this.allowed() || !this.access().canEdit || this.state.saving || !this.state.base) return
    this.cancel('suggestion')
    let draftError: string | null = null
    try { normalizeSettingTags([...this.state.draft, search]) } catch (error) { draftError = errorMessage(error) }
    this.update({ search, suggestions: [], suggesting: false, suggestionError: null, draftError, notice: null })
  }
  suggest = async () => {
    if (!this.allowed() || !this.access().canEdit || !this.state.base || this.state.saving || this.state.reviewing) return
    const search = this.state.search
    let q: string
    try { q = normalizeSettingTags([search])[0] ?? '' }
    catch { return } // 非法的待添加标签已在输入区提示，不把超长查询送到建议接口。
    const ticket = this.begin('suggestion')
    this.update({ suggesting: true, suggestionError: null })
    try {
      const result = await this.api.suggestTags(this.pid, { q, limit: 20 }, ticket.request.signal)
      if (this.valid(ticket, 'suggestion') && this.access().canEdit && this.state.search === search) this.update({ suggestions: normalizeSettingTags(result.items.slice(0, 20)), suggesting: false })
    } catch (error) { if (this.valid(ticket, 'suggestion')) this.update({ suggesting: false, suggestionError: errorMessage(error) }) }
  }

  compare = async () => {
    if (!this.allowed() || !this.state.base || this.state.saving || this.state.reviewing) return
    const ticket = this.begin('read')
    this.update({ reviewing: true, loading: false, error: null, reviewed: false })
    try {
      const result = await this.api.getTags(this.pid, this.entry, ticket.request.signal)
      if (this.valid(ticket, 'read')) this.update({ latest: tagSnapshot(result, this.entry), reviewing: false, reviewed: true, reviewRequired: true })
    } catch (error) { if (this.valid(ticket, 'read')) this.readFailure(error) }
  }
  acceptComparison = () => {
    if (!this.allowed() || !this.access().canEdit || !this.state.reviewed || !this.state.latest || this.state.saving || this.state.reviewing) return
    let matches = false
    try { matches = sameTags(normalizeSettingTags([...this.state.draft, this.state.search]), this.state.latest.setting_tags) } catch { /* 非法草稿仍保留供作者修正。 */ }
    this.update({ base: tagSnapshot(this.state.latest, this.entry), latest: null, reviewed: false, reviewRequired: false, error: null,
      notice: matches ? '最新标签与当前草稿一致，无需再次保存。' : '已采用对照版本，当前标签草稿已保留，请确认后保存。' })
  }

  save = async () => {
    if (!this.allowed() || !this.access().canEdit || !this.state.base || this.state.saving || this.state.loading || this.state.reviewing || this.state.reviewRequired) return
    let tags: string[]
    try { tags = normalizeSettingTags([...this.state.draft, this.state.search]) }
    catch (error) { this.update({ draftError: errorMessage(error) }); return }
    if (sameTags(tags, this.state.base.setting_tags)) return
    const expectedRevision = this.state.base.categories_revision
    this.cancel('read')
    this.cancel('suggestion')
    const ticket = this.begin('write')
    this.update({ draft: tags, search: '', saving: true, suggesting: false, draftError: null, error: null, notice: null })
    try {
      // 二次检查实时写权限，外层表单与剧情字段从不进入这个请求。
      if (!this.access().canEdit || !this.valid(ticket, 'write')) { this.invalidate(); return }
      const result = await this.api.updateTags(this.pid, this.entry, { setting_tags: [...tags], expected_categories_revision: expectedRevision }, ticket.request.signal)
      if (!this.valid(ticket, 'write')) return
      if (!this.access().canEdit) { this.invalidate(); return }
      const saved = tagSnapshot(result, this.entry)
      this.update({ base: saved, original: tagSnapshot(saved, this.entry), draft: [...saved.setting_tags], saving: false, latest: null, reviewed: false, reviewRequired: false, notice: '整理标签已保存。' })
      this.changed({ ...this.entry })
    } catch (error) {
      if (!this.valid(ticket, 'write')) return
      const uncertain = !(error instanceof ApiError) || error.status === 0 || error.status >= 500
      this.update({ saving: false, error: errorMessage(error), reviewRequired: !!settingCategoryConflictScope(error) || uncertain, reviewed: false, latest: null,
        notice: uncertain ? '保存结果尚不能确认，标签草稿已保留，请先读取最新标签并对照。' : null })
    }
  }
}

/** 缓存仅属于一个页面的 actor/project；不注册全局 store，不缓存条目正文。 */
export class SettingTagsSession {
  private controllers = new Map<string, SettingTagsController>()
  constructor(readonly projectId: number, private readonly api: TagsApi = settingCategoriesApi,
    private readonly access: () => SettingTagsAccess = () => OPEN_ACCESS, private readonly changed: (entry: SettingEntryRef) => void = () => {}) {}
  get(entry: SettingEntryRef) {
    const key = `${entry.kind}:${entry.id}`
    let controller = this.controllers.get(key)
    if (!controller) { controller = new SettingTagsController(this.projectId, { ...entry }, this.api, this.access, this.changed); this.controllers.set(key, controller) }
    return controller
  }
  forget = (entry: SettingEntryRef) => {
    const key = `${entry.kind}:${entry.id}`
    const controller = this.controllers.get(key)
    if (controller) { controller.close(); controller.clear(); this.controllers.delete(key) }
  }
  accessChanged = () => { this.controllers.forEach((controller) => controller.accessChanged()) }
  refresh = () => { this.controllers.forEach((controller) => controller.refreshIfOpen()) }
  deactivate = () => { this.controllers.forEach((controller) => { controller.close(); controller.clear() }) }
}
