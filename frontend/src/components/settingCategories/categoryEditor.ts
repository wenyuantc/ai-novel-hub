import { ApiError, errorMessage } from '../../api/client'
import { settingCategoriesApi, settingCategoryConflictScope, settingCategoryRuleIndex } from '../../api/settingCategories'
import { settingFieldsApi } from '../../api/settingFields'
import type { SettingCategory, SettingCategoryEntryPage, SettingCategoryEntryQuery, SettingCategoryVersions, SettingFieldDefinitionList } from '../../api/types'
import { categoryDirty, categoryDraft, categoryDraftInput, categoryPatch, type CategoryDraft } from './categoryDraft'
import { withCategoryMetadata, type CategoryBundle } from './categoryQuery'

interface CategoryConfiguration { original: SettingCategory | null; base: SettingCategoryVersions; definitions: SettingFieldDefinitionList }
export interface CategorySession {
  key: string; categoryId: number | null; open: boolean; original: SettingCategory | null; draft: CategoryDraft
  base: SettingCategoryVersions | null; definitions: SettingFieldDefinitionList | null
  loading: boolean; saving: boolean; reviewing: boolean; reviewRequired: boolean; review: CategoryConfiguration | null
  error: string | null; issueIndex: number | null
  preview: CategoryBundle<SettingCategoryEntryPage> | null; previewLoading: boolean; previewError: string | null
}
export interface CategoryEditorState { activeKey: string | null; sessions: Record<string, CategorySession>; notice: string | null }
export interface CategoryDeleteToken { key: string; generation: number; original: SettingCategory; base: SettingCategoryVersions; identity: string }
export function categoryEditorDirty(state: CategoryEditorState): boolean { return Object.values(state.sessions).some((session) => categoryDirty(session.draft, session.original)) }

/** 分类分别保存本地草稿；原快照、双版本和读取最新对照始终分开。 */
export class CategoryEditor {
  private state: CategoryEditorState = { activeKey: null, sessions: {}, notice: null }
  private listeners = new Set<() => void>()
  private active = false
  private generation = 0
  private request: AbortController | null = null
  private notifyingSavedKey: string | null = null
  constructor(private readonly pid: number, private readonly api = settingCategoriesApi,
    private readonly definitions = (signal: AbortSignal) => settingFieldsApi.listDefinitions(pid, { include_archived: true }, signal),
    private readonly readable = () => true, private readonly writable = () => true, private readonly identity = () => '',
    private changed: (id: number | null) => void = () => {}) {}
  setChanged = (changed: (id: number | null) => void) => { this.changed = changed }
  private notifyChanged(id: number | null) {
    // 同步自写广播保留本次成功响应；其他草稿和之后的外部广播仍须核对。
    this.notifyingSavedKey = id === null ? null : `category-${id}`
    try { this.changed(id) } finally { this.notifyingSavedKey = null }
  }
  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.active = true }
  deactivate() { this.active = false; this.cancel() }
  private update(patch: Partial<CategoryEditorState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  private current() { return this.state.activeKey ? this.state.sessions[this.state.activeKey] : null }
  private session(patch: Partial<CategorySession>) {
    const current = this.current()
    if (current) this.update({ sessions: { ...this.state.sessions, [current.key]: { ...current, ...patch } } })
  }
  private cancel() { this.generation += 1; this.request?.abort(); this.request = null }
  private allowed() {
    if (!this.readable()) { this.cancel(); this.update({ activeKey: null, sessions: {}, notice: null }); return false }
    return this.active
  }
  private begin() {
    this.cancel()
    const request = new AbortController(); this.request = request
    return { request, generation: this.generation, identity: this.identity(), key: this.state.activeKey }
  }
  private valid(context: ReturnType<CategoryEditor['begin']>, write = false) {
    const valid = this.active && context.generation === this.generation && !context.request.signal.aborted
      && context.key === this.state.activeKey && context.identity === this.identity() && this.allowed()
    if (valid && (!write || this.writable())) return true
    if (context.generation === this.generation) this.invalidate()
    return false
  }
  private async read(id: number | null, signal: AbortSignal): Promise<CategoryConfiguration> {
    const [category, definitions] = await Promise.all([id === null ? this.api.list(this.pid, { page: 1, page_size: 1 }, signal) : this.api.get(this.pid, id, signal), this.definitions(signal)])
    withCategoryMetadata(category, definitions)
    return { original: 'item' in category ? category.item : null, definitions, base: { categories_revision: category.categories_revision, fields_revision: category.fields_revision } }
  }
  invalidate = (configurationChanged = true) => {
    this.cancel()
    if (!this.readable()) { this.update({ activeKey: null, sessions: {}, notice: null }); return }
    const sessions = Object.fromEntries(Object.entries(this.state.sessions).map(([key, session]) => [key, { ...session,
      loading: false, saving: false, reviewing: false, previewLoading: false, preview: null, previewError: null,
      ...(configurationChanged ? { reviewRequired: !!session.base && key !== this.notifyingSavedKey, review: null } : {}) }]))
    this.update({ sessions })
  }
  expirePreview = () => {
    if (this.current()?.previewLoading) this.cancel()
    this.session({ preview: null, previewLoading: false, previewError: null })
  }
  open = async (categoryId: number | null) => {
    if (!this.allowed() || (categoryId === null && !this.writable()) || this.current()?.saving) return
    this.invalidate(false)
    const key = categoryId === null ? 'new' : `category-${categoryId}`
    let session = this.state.sessions[key]
    if (!session) session = { key, categoryId, open: true, original: null, draft: categoryDraft(null), base: null, definitions: null,
      loading: false, saving: false, reviewing: false, reviewRequired: false, review: null, error: null, issueIndex: null, preview: null, previewLoading: false, previewError: null }
    this.update({ activeKey: key, sessions: { ...this.state.sessions, [key]: { ...session, open: true } }, notice: null })
    if (session.base) return
    const context = this.begin(); this.session({ loading: true, error: null })
    try {
      const configuration = await this.read(categoryId, context.request.signal)
      if (this.valid(context)) this.session({ ...configuration, draft: categoryDraft(configuration.original), loading: false })
    } catch (error) { if (this.valid(context)) this.session({ loading: false, error: errorMessage(error) }) }
  }
  close = () => { if (!this.current()?.saving) { this.cancel(); this.session({ open: false, loading: false, reviewing: false, previewLoading: false }) } }
  discard = () => {
    const session = this.current()
    if (!this.allowed() || !session || session.saving) return
    this.cancel(); const sessions = { ...this.state.sessions }; delete sessions[session.key]
    this.update({ sessions, activeKey: null, notice: '已放弃此分类的本地草稿。' })
  }
  edit = (patch: Partial<CategoryDraft>) => {
    const session = this.current()
    if (!this.allowed() || !this.writable() || !session?.base || session.saving || session.loading) return
    this.cancel()
    this.session({ draft: { ...session.draft, ...patch }, error: null, issueIndex: null, preview: null, previewLoading: false, previewError: null, reviewing: false, review: null })
  }
  review = async () => {
    const session = this.current()
    if (!this.allowed() || !session || session.saving || session.loading) return
    const context = this.begin(); this.session({ reviewing: true, error: null, review: null, previewLoading: false })
    try {
      const review = await this.read(session.categoryId, context.request.signal)
      if (this.valid(context)) this.session({ review, reviewing: false })
    } catch (error) { if (this.valid(context)) this.session({ reviewing: false, error: errorMessage(error) }) }
  }
  useReview = (keepDraft: boolean) => {
    const session = this.current()
    if (!this.allowed() || !session?.review || session.saving || session.reviewing || (keepDraft && !this.writable())) return
    this.cancel()
    this.session({ ...session.review, draft: keepDraft ? session.draft : categoryDraft(session.review.original), review: null,
      reviewRequired: false, error: null, issueIndex: null, preview: null, previewLoading: false, previewError: null })
  }
  private failure(error: unknown) {
    const uncertain = !(error instanceof ApiError) || error.status === 0 || error.status >= 500
    const conflict = settingCategoryConflictScope(error) !== null
    this.session({ saving: false, error: uncertain ? `${errorMessage(error)}。保存结果尚不能确认，请读取最新对照。` : errorMessage(error), issueIndex: settingCategoryRuleIndex(error),
      ...((uncertain || conflict) ? { reviewRequired: true, review: null } : {}) })
  }
  private writableSession() {
    const session = this.current()
    return this.allowed() && this.writable() && session?.open && session.base && session.definitions && !session.loading && !session.saving && !session.reviewing && !session.reviewRequired ? session : null
  }
  save = async () => {
    const session = this.writableSession()
    if (!session || !session.base || !session.definitions) return
    const input = categoryDraftInput(session.draft, session.definitions.items, session.original)
    if (!input.ok) { this.session({ error: input.issues[0].message, issueIndex: input.issues[0].ruleIndex ?? null }); return }
    const context = this.begin(); this.session({ saving: true, error: null, issueIndex: null, previewLoading: false })
    const versions = { expected_categories_revision: session.base.categories_revision, expected_fields_revision: session.base.fields_revision }
    try {
      const result = session.original ? await this.api.update(this.pid, session.original.id, { ...categoryPatch(session.original, input.input), ...versions }, context.request.signal)
        : await this.api.create(this.pid, { ...input.input, ...versions }, context.request.signal)
      if (!this.valid(context, true)) return
      const key = `category-${result.item.id}`
      const sessions = { ...this.state.sessions }; delete sessions[session.key]
      sessions[key] = { ...session, key, categoryId: result.item.id, open: false, original: result.item, draft: categoryDraft(result.item),
        base: { categories_revision: result.categories_revision, fields_revision: result.fields_revision }, saving: false, error: null, preview: null }
      this.update({ activeKey: key, sessions, notice: '分类已保存，成员将按条件自动计算。' }); this.notifyChanged(result.item.id)
    } catch (error) { if (this.valid(context)) this.failure(error) }
  }
  deleteToken = (): CategoryDeleteToken | null => {
    const session = this.writableSession()
    return session?.original && session.base ? { key: session.key, generation: this.generation, original: session.original, base: session.base, identity: this.identity() } : null
  }
  remove = async (token: CategoryDeleteToken) => {
    const session = this.writableSession()
    if (!session || session.key !== token.key || token.generation !== this.generation || token.identity !== this.identity() || token.original !== session.original || token.base !== session.base) return
    const context = this.begin(); this.session({ saving: true, error: null, previewLoading: false })
    try {
      await this.api.remove(this.pid, token.original.id, token.base.categories_revision, context.request.signal)
      if (!this.valid(context, true)) return
      const sessions = { ...this.state.sessions }; delete sessions[session.key]
      this.update({ activeKey: null, sessions, notice: '分类已删除，设定条目、标签及字段值仍保留。' }); this.notifyChanged(null)
    } catch (error) { if (this.valid(context)) this.failure(error) }
  }
  preview = async (filters: SettingCategoryEntryQuery) => {
    const session = this.current()
    if (!this.allowed() || !session?.base || !session.definitions || session.loading || session.saving || session.reviewing) return
    const input = categoryDraftInput(session.draft, session.definitions.items, session.original)
    if (!input.ok) { this.session({ error: input.issues[0].message, issueIndex: input.issues[0].ruleIndex ?? null }); return }
    const context = this.begin(); this.session({ preview: null, previewLoading: true, previewError: null })
    try {
      const value = await this.api.preview(this.pid, { draft: input.input, filters, ...(session.categoryId !== null ? { editing_category_id: session.categoryId } : {}) }, context.request.signal)
      if (!this.valid(context)) return
      if (value.categories_revision !== session.base.categories_revision || value.fields_revision !== session.base.fields_revision) {
        this.session({ previewLoading: false, previewError: '分类或字段配置已有变化，请先读取最新对照。原草稿和保存版本已保留。', reviewRequired: true, review: null }); return
      }
      this.session({ preview: withCategoryMetadata(value, session.definitions), previewLoading: false })
    } catch (error) { if (this.valid(context)) this.session({ previewLoading: false, previewError: errorMessage(error), issueIndex: settingCategoryRuleIndex(error) }) }
  }
}
