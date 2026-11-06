import { ApiError, errorMessage } from '../../api/client'
import { isSettingFieldsRevisionConflict, settingFieldsApi } from '../../api/settingFields'
import type { SettingFieldDefinition, SettingFieldDefinitionList, SettingFieldDefinitionMutation, SettingFieldOption } from '../../api/types'
import { definitionDraft, definitionDraftError, definitionInput, optionDraftError, type DefinitionDraft, type OptionDraft } from './settingFieldDraft'

interface EditorScope {
  open: boolean
  expectedRevision: number
  reviewRequired: boolean
  reviewed: SettingFieldDefinitionMutation | null
  reviewedRevision: number | null
}
export type FieldConfigurationEditor = EditorScope & (
  | { kind: 'definition'; original: SettingFieldDefinition | null; draft: DefinitionDraft }
  | { kind: 'option'; definition: SettingFieldDefinition; original: SettingFieldOption | null; draft: OptionDraft }
)
export interface SettingFieldsState {
  data: SettingFieldDefinitionList | null
  loading: boolean
  saving: boolean
  reviewing: boolean
  editor: FieldConfigurationEditor | null
  error: string | null
  notice: string | null
}

function optionDraft(item: SettingFieldOption | null): OptionDraft { return { label: item?.label ?? '', sort: item?.sort ?? 0, archived: item?.archived ?? false } }
function copy(item: SettingFieldDefinition): SettingFieldDefinition { return { ...item, applicable_types: [...item.applicable_types], reference_types: [...item.reference_types], options: item.options.map((value) => ({ ...value })) } }
export function configurationDirty(editor: FieldConfigurationEditor | null): boolean {
  if (!editor) return false
  return JSON.stringify(editor.draft) !== JSON.stringify(editor.kind === 'definition' ? definitionDraft(editor.original) : optionDraft(editor.original))
}

/** 本页拥有请求代次、原快照和草稿；刷新与冲突均不替换草稿的保存版本。 */
export class SettingFieldsController {
  private state: SettingFieldsState = { data: null, loading: false, saving: false, reviewing: false, editor: null, error: null, notice: null }
  private listeners = new Set<() => void>()
  private active = false
  private generation = 0
  private request: AbortController | null = null

  constructor(private readonly pid: number, private readonly api = settingFieldsApi,
    private readonly readable: () => boolean = () => true, private readonly writable: () => boolean = () => true,
    private readonly changed: (aiChanged?: boolean) => void = () => {}) {}

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.active = true }
  deactivate() { this.active = false; this.invalidate() }
  private update(patch: Partial<SettingFieldsState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  private cancel() { this.generation += 1; this.request?.abort(); this.request = null }
  private allowed() {
    if (!this.readable()) { this.cancel(); this.update({ data: null, editor: null, loading: false, saving: false, reviewing: false, error: null, notice: null }); return false }
    return this.active
  }
  private valid(generation: number, request: AbortController) { return this.active && this.generation === generation && !request.signal.aborted && this.allowed() }
  private validWrite(generation: number, request: AbortController) {
    if (!this.valid(generation, request)) return false
    if (this.writable()) return true
    this.invalidate()
    return false
  }
  private begin() { this.cancel(); const request = new AbortController(); this.request = request; return { request, generation: this.generation } }
  private scope(): EditorScope { return { open: true, expectedRevision: this.state.data?.fields_revision ?? 0, reviewRequired: false, reviewed: null, reviewedRevision: null } }

  invalidate = () => {
    this.cancel()
    const editor = this.state.editor
    this.update({ loading: false, saving: false, reviewing: false, editor: editor ? { ...editor, reviewRequired: true, reviewed: null, reviewedRevision: null } : null,
      notice: editor ? '作品设定已有变化，草稿和原配置已保留，请核对最新配置。' : this.state.notice })
  }

  refresh = async () => {
    if (!this.allowed() || this.state.saving || this.state.reviewing) return
    const { request, generation } = this.begin()
    this.update({ loading: true, error: null })
    try {
      const data = await this.api.listDefinitions(this.pid, { include_archived: true }, request.signal)
      if (!this.valid(generation, request)) return
      const editor = this.state.editor
      this.update({ data, loading: false, editor: editor && editor.expectedRevision !== data.fields_revision ? { ...editor, reviewRequired: true } : editor })
    } catch (err) { if (this.valid(generation, request)) this.update({ loading: false, error: errorMessage(err) }) }
  }

  private open(editor: FieldConfigurationEditor) {
    if (!this.allowed() || !this.writable() || this.state.saving || this.state.loading || this.state.reviewing || !this.state.data) return
    if (configurationDirty(this.state.editor)) {
      this.update({ editor: this.state.editor ? { ...this.state.editor, open: true } : null, notice: '请先完成或放弃当前草稿，再编辑其他配置。' })
      return
    }
    this.update({ editor, error: null, notice: null })
  }
  openDefinition = (item: SettingFieldDefinition | null) => {
    const current = item ? this.state.data?.items.find((entry) => entry.id === item.id) : null
    if (item && !current) return
    this.open({ ...this.scope(), kind: 'definition', original: current ? copy(current) : null, draft: definitionDraft(current ?? null) })
  }
  openOption = (definition: SettingFieldDefinition, item: SettingFieldOption | null) => {
    const current = this.state.data?.items.find((entry) => entry.id === definition.id)
    const selected = current?.options.find((entry) => entry.id === item?.id) ?? null
    if (!current || (item && !selected)) return
    this.open({ ...this.scope(), kind: 'option', definition: copy(current), original: selected ? { ...selected } : null, draft: optionDraft(selected) })
  }
  resumeEditor = () => { if (this.allowed() && this.state.editor && !this.state.saving) this.update({ editor: { ...this.state.editor, open: true } }) }
  closeEditor = () => { if (this.state.editor && !this.state.saving && !this.state.reviewing) this.update({ editor: { ...this.state.editor, open: false } }) }
  discardEditor = () => { if (!this.state.saving && !this.state.reviewing) this.update({ editor: null, error: null, notice: null }) }
  editDefinition = (patch: Partial<DefinitionDraft>) => {
    const editor = this.state.editor
    if (this.allowed() && this.writable() && editor?.kind === 'definition' && !this.state.saving) this.update({ editor: { ...editor, draft: { ...editor.draft, ...patch } }, error: null })
  }
  editOption = (patch: Partial<OptionDraft>) => {
    const editor = this.state.editor
    if (this.allowed() && this.writable() && editor?.kind === 'option' && !this.state.saving) this.update({ editor: { ...editor, draft: { ...editor.draft, ...patch } }, error: null })
  }

  review = async () => {
    const editor = this.state.editor
    if (!this.allowed() || !editor || this.state.saving || this.state.loading || this.state.reviewing) return
    const { request, generation } = this.begin()
    this.update({ reviewing: true, error: null })
    try {
      const id = editor.kind === 'definition' ? editor.original?.id : editor.definition.id
      const latest = await this.api.listDefinitions(this.pid, { include_archived: true }, request.signal)
      if (!this.valid(generation, request) || !this.state.editor) return
      const item = latest.items.find((item) => item.id === id)
      this.update({ data: latest, reviewing: false, editor: { ...this.state.editor, reviewed: item ? { item, fields_revision: latest.fields_revision } : null, reviewedRevision: latest.fields_revision } })
    } catch (err) { if (this.valid(generation, request)) this.update({ reviewing: false, error: errorMessage(err) }) }
  }

  /** 仅经用户显式放弃草稿后采用已核对版本，绝不自动修改 expectedRevision。 */
  useReviewed = () => {
    const editor = this.state.editor
    if (!this.allowed() || !this.writable() || !editor || editor.reviewedRevision === null || this.state.saving || this.state.reviewing) return
    const scope = { ...this.scope(), expectedRevision: editor.reviewedRevision }
    if (editor.kind === 'definition') {
      const item = editor.reviewed?.item ?? null
      if (editor.original && !item) { this.update({ editor: null, error: null, notice: '该字段已不存在，请从最新字段配置重新选择。' }); return }
      this.update({ editor: { ...scope, kind: 'definition', original: item ? copy(item) : null, draft: definitionDraft(item) }, error: null, notice: null })
    } else {
      const definition = editor.reviewed?.item
      const item = definition?.options.find((entry) => entry.id === editor.original?.id) ?? null
      if (!definition || (editor.original && !item)) { this.update({ editor: null, notice: '该选项已不存在，请从最新字段配置重新选择。', error: null }); return }
      this.update({ editor: { ...scope, kind: 'option', definition: copy(definition), original: item, draft: optionDraft(item) }, error: null, notice: null })
    }
  }

  private failure(err: unknown) {
    const uncertain = !(err instanceof ApiError) || err.status === 0 || err.status >= 500
    const editor = this.state.editor
    this.update({ saving: false, error: errorMessage(err), editor: editor && (isSettingFieldsRevisionConflict(err) || uncertain) ? { ...editor, reviewRequired: true, reviewed: null, reviewedRevision: null } : editor,
      notice: uncertain ? '保存结果尚不能确认，草稿已保留，请先核对最新配置。' : null })
  }
  private canSave() { return this.allowed() && this.writable() && !this.state.saving && !this.state.loading && !this.state.reviewing && !!this.state.editor?.open && !this.state.editor.reviewRequired }

  save = async () => {
    if (!this.canSave()) return
    const editor = this.state.editor
    if (!editor) return
    const invalid = editor.kind === 'definition' ? definitionDraftError(editor.draft) : optionDraftError(editor.draft)
    if (invalid) { this.update({ error: invalid }); return }
    const { request, generation } = this.begin()
    this.update({ saving: true, error: null, notice: null })
    try {
      let items = this.state.data?.items ?? []
      let revision: number
      let aiChanged = false
      if (editor.kind === 'definition') {
        const body = definitionInput(editor.draft, editor.expectedRevision, editor.original?.ai_hidden ?? false)
        aiChanged = body.ai_hidden !== undefined
        const result = editor.original ? await this.api.updateDefinition(this.pid, editor.original.id, body, request.signal) : await this.api.createDefinition(this.pid, body, request.signal)
        if (!this.validWrite(generation, request)) return
        items = [...items.filter((item) => item.id !== result.item.id), result.item]
        revision = result.fields_revision
      } else {
        const body = { expected_fields_revision: editor.expectedRevision, label: editor.draft.label.trim(), sort: editor.draft.sort ?? 0, archived: editor.draft.archived }
        const result = editor.original ? await this.api.updateOption(this.pid, editor.definition.id, editor.original.id, body, request.signal) : await this.api.createOption(this.pid, editor.definition.id, body, request.signal)
        if (!this.validWrite(generation, request)) return
        items = items.map((item) => item.id === editor.definition.id ? { ...item, options: [...item.options.filter((entry) => entry.id !== result.item.id), result.item] } : item)
        revision = result.fields_revision
      }
      this.update({ data: { items, total: items.length, fields_revision: revision }, saving: false, editor: null, notice: editor.kind === 'definition' ? '字段定义已保存。' : '下拉选项已保存。' })
      this.changed(aiChanged)
    } catch (err) { if (this.valid(generation, request)) this.failure(err) }
  }

  remove = async () => {
    if (!this.canSave()) return
    const editor = this.state.editor
    if (!editor?.original) return
    const { request, generation } = this.begin()
    this.update({ saving: true, error: null, notice: null })
    try {
      const result = editor.kind === 'definition'
        ? await this.api.removeDefinition(this.pid, editor.original.id, editor.expectedRevision, request.signal)
        : await this.api.removeOption(this.pid, editor.definition.id, editor.original.id, editor.expectedRevision, request.signal)
      if (!this.validWrite(generation, request)) return
      const items = (this.state.data?.items ?? []).filter((item) => editor.kind !== 'definition' || item.id !== editor.original?.id)
        .map((item) => editor.kind === 'option' && item.id === editor.definition.id ? { ...item, options: item.options.filter((option) => option.id !== editor.original?.id) } : item)
      this.update({ data: { items, total: items.length, fields_revision: result.fields_revision }, saving: false, editor: null, notice: '已删除未被使用的配置。' })
      this.changed()
    } catch (err) { if (this.valid(generation, request)) this.failure(err) }
  }
}
