import { cardApi, defaultResolutions, MAX_CARD_FILE_BYTES, parseCardText } from '../../api/cards'
import { ApiError, errorMessage } from '../../api/client'
import type { CardDefinitionMapping, CardImportResult, CardMappings, CardPreview, CardResolution, CardResolutions, SettingEntry, SettingFieldDefinition } from '../../api/types'
import type { ProjectAccessCheck } from '../../stores/project'
import { writeResultUnknown } from '../../utils/writeResult'
import { cardMetadata, cleanCardMappings, isInternalReference, withoutInternalMapping, type CardEntryGroup, type CardMetadata } from './cardImportMapping'

export interface CardImportState {
  data: unknown
  fileName: string
  metadata: CardMetadata
  preview: CardPreview | null
  reviewPreview: CardPreview | null
  choices: CardResolutions
  mappings: CardMappings
  selectedEntries: Record<string, SettingEntry>
  selectedDefinitions: Record<string, SettingFieldDefinition>
  loading: boolean
  importing: boolean
  needsPreview: boolean
  reviewRequired: boolean
  error: string | null
  result: CardImportResult | null
  accessUncertain: boolean
  checkingAccess: boolean
  resultUnknown: boolean
}
const empty = (): CardImportState => ({ data: null, fileName: '', metadata: cardMetadata(null), preview: null, reviewPreview: null,
  choices: { characters: {}, organizations: {}, careers: {} }, mappings: { references: {}, definitions: {} }, selectedEntries: {}, selectedDefinitions: {},
  loading: false, importing: false, needsPreview: false, reviewRequired: false, error: null, result: null,
  accessUncertain: false, checkingAccess: false, resultUnknown: false })
function copyChoices(value: CardResolutions): CardResolutions { return { characters: { ...value.characters }, organizations: { ...value.organizations }, ...(value.careers ? { careers: { ...value.careers } } : {}) } }

/** 文件、请求与确认由同一所有者管理；冲突后的新预览须作者显式比较采纳。 */
export class CardImportController {
  private state = empty()
  private listeners = new Set<() => void>()
  private active = false
  private generation = 0
  private request: AbortController | null = null
  private accessCheck = 0
  constructor(
    private readonly pid: number,
    private readonly api = cardApi,
    private readonly allowed: () => boolean = () => true,
    private readonly imported: () => void = () => {},
    private readonly canWrite: () => boolean = () => true,
    private readonly verifyAccess: (afterFailure: boolean) => Promise<ProjectAccessCheck> = async () => 'unknown',
  ) {}
  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private update(patch: Partial<CardImportState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  private cancel() { this.generation += 1; this.request?.abort(); this.request = null }
  activate() { this.active = true }
  deactivate() { this.active = false; this.cancel(); this.accessCheck += 1 }
  private current() { if (!this.allowed()) { this.cancel(); this.update(empty()); return false }; return this.active }
  private valid(generation: number, request: AbortController) { return this.active && generation === this.generation && !request.signal.aborted && this.current() }
  private begin() { this.cancel(); const request = new AbortController(); this.request = request; return { request, generation: this.generation } }
  private editable() { return this.current() && !this.state.loading && !this.state.importing && !this.state.result && !this.state.accessUncertain }
  private edited(patch: Partial<CardImportState>) { this.update({ ...patch, needsPreview: (this.state.metadata.version ?? 0) >= 4, reviewPreview: null, error: null }) }
  private denyIfNeeded(error: unknown) {
    if (!(error instanceof ApiError) || (error.status !== 403 && error.status !== 404)) return false
    this.cancel()
    this.update({ loading: false, importing: false, accessUncertain: true, checkingAccess: false,
      needsPreview: this.state.data !== null, reviewRequired: this.state.preview !== null,
      reviewPreview: null, error: '作品权限需要确认，文件和当前映射已保留。' })
    void this.recheckAccess(true)
    return true
  }

  recheckAccess = async (afterFailure = false) => {
    if (!this.active || !this.state.accessUncertain || this.state.checkingAccess) return
    const ticket = ++this.accessCheck
    this.update({ checkingAccess: true })
    const result = await this.verifyAccess(afterFailure).catch(() => 'unknown' as const)
    if (!this.active || ticket !== this.accessCheck) return
    if (!this.allowed()) { this.cancel(); this.update(empty()); return }
    if (result === 'denied') { this.cancel(); this.update(empty()); return }
    if (result === 'confirmed') {
      this.update({ accessUncertain: false, checkingAccess: false, error: this.state.resultUnknown
        ? '作品权限已确认；先前导入结果尚不明确，请重新预览并核对。' : '作品权限已确认，请重新预览并核对。' })
    } else {
      this.update({ checkingAccess: false, error: '暂时无法确认作品权限，文件和映射已保留，请重试权限确认。' })
    }
  }

  /** 同一会话仍可编辑时保留输入并要求重新预览；失权或会话变化时清除。 */
  accessChanged = () => {
    this.accessCheck += 1
    if (!this.allowed() || !this.canWrite()) { this.cancel(); this.update(empty()); return }
    const resultUnknown = this.state.resultUnknown || this.state.importing
    this.cancel()
    this.update({ loading: false, importing: false, checkingAccess: false, accessUncertain: true,
      needsPreview: this.state.data !== null, reviewRequired: this.state.preview !== null, resultUnknown,
      reviewPreview: null, error: resultUnknown ? '作品授权已变化，先前导入结果尚不明确；文件和映射已保留。'
        : '作品授权已变化，文件和映射已保留，请重新确认权限。' })
  }

  selectFile = async (file: Pick<File, 'size' | 'name' | 'text'>) => {
    if (!this.current() || this.state.importing || this.state.accessUncertain) return
    const { request, generation } = this.begin()
    this.update({ ...empty(), fileName: file.name, loading: true })
    try {
      if (file.size > MAX_CARD_FILE_BYTES) throw new Error('卡片文件过大，上限为 2MB')
      const text = await file.text()
      if (!this.valid(generation, request)) return
      const parsed = parseCardText(text)
      if (parsed.error) throw new Error(parsed.error)
      this.update({ data: parsed.data, metadata: cardMetadata(parsed.data) })
      const preview = await this.api.preview(this.pid, parsed.data, {}, request.signal)
      if (!this.valid(generation, request)) return
      this.update({ data: parsed.data, metadata: cardMetadata(parsed.data), preview, choices: defaultResolutions(preview), loading: false })
    } catch (err) { if (this.valid(generation, request) && !this.denyIfNeeded(err)) this.update({ loading: false, error: errorMessage(err) }) }
  }

  setResolution = (group: CardEntryGroup, name: string, resolution: CardResolution) => {
    if (!this.editable()) return
    const choices = { ...this.state.choices, [group]: { ...this.state.choices[group], [name]: resolution } }
    const mappings = resolution === 'rename' ? withoutInternalMapping(this.state.mappings, this.state.metadata, group, name) : this.state.mappings
    const selectedEntries = Object.fromEntries(Object.entries(this.state.selectedEntries).filter(([ref]) => !!mappings.references[ref]))
    this.edited({ choices, mappings, selectedEntries })
  }
  setDefinition = (ref: string, mapping: CardDefinitionMapping | null, target?: SettingFieldDefinition) => {
    if (!this.editable()) return
    const definitions = { ...this.state.mappings.definitions }
    if (mapping) definitions[ref] = { ...mapping, option_mappings: { ...mapping.option_mappings } }
    else delete definitions[ref]
    this.edited({ mappings: { ...this.state.mappings, definitions }, selectedDefinitions: target ? { ...this.state.selectedDefinitions, [ref]: target } : this.state.selectedDefinitions })
  }
  setReference = (ref: string, entry: SettingEntry | null) => {
    if (!this.editable()) return
    if (entry && this.state.preview && isInternalReference(ref, this.state.metadata, this.state.choices, this.state.preview)) return
    const references = { ...this.state.mappings.references }
    const selectedEntries = { ...this.state.selectedEntries }
    if (entry) { references[ref] = { ...entry.reference }; selectedEntries[ref] = entry }
    else { delete references[ref]; delete selectedEntries[ref] }
    this.edited({ mappings: { ...this.state.mappings, references }, selectedEntries })
  }

  invalidate = () => {
    if (this.state.result) return
    if (!this.state.preview) {
      if (this.state.loading) { this.cancel(); this.update({ loading: false, error: '作品设定已变化，请重新选择文件以读取最新预览。' }) }
      return
    }
    const resultUnknown = this.state.resultUnknown || this.state.importing
    this.cancel()
    this.update({ loading: false, importing: false, reviewPreview: null, needsPreview: true, reviewRequired: true, resultUnknown,
      error: resultUnknown ? '先前导入结果尚不明确；文件和映射已保留，请重新预览并核对。'
        : '作品设定已变化，当前输入仍保留，请重新预览并比较。' })
  }

  refreshPreview = async () => {
    if (!this.editable() || this.state.data === null) return
    const originalPreview = this.state.preview
    const { request, generation } = this.begin()
    const choices = copyChoices(this.state.choices)
    const mappings = cleanCardMappings(this.state.mappings)
    this.update({ loading: true, error: null })
    try {
      const preview = await this.api.preview(this.pid, this.state.data, { resolutions: choices, mappings }, request.signal)
      if (!this.valid(generation, request)) return
      if (!originalPreview) {
        this.update({ preview, choices: defaultResolutions(preview), loading: false, needsPreview: false, reviewRequired: false })
        return
      }
      const version = this.state.metadata.version ?? 0
      const versionsChanged = (version >= 5 && (preview.fields_revision !== originalPreview.fields_revision || preview.categories_revision !== originalPreview.categories_revision))
        || (version >= 6 && preview.ai_visibility_revision !== originalPreview.ai_visibility_revision)
      this.update(this.state.reviewRequired || versionsChanged ? { reviewPreview: preview, loading: false, reviewRequired: true, needsPreview: true }
        : { preview, loading: false, needsPreview: false, reviewPreview: null })
    } catch (err) { if (this.valid(generation, request) && !this.denyIfNeeded(err)) this.update({ loading: false, needsPreview: true, error: errorMessage(err) }) }
  }

  acceptReview = () => {
    if (!this.editable() || !this.state.reviewPreview) return
    this.update({ preview: this.state.reviewPreview, reviewPreview: null, reviewRequired: false, needsPreview: false, resultUnknown: false, error: null })
  }

  confirm = async () => {
    if (!this.editable() || !this.canWrite() || !this.state.preview?.can_import || this.state.needsPreview || this.state.reviewRequired) return
    const { request, generation } = this.begin()
    const version = this.state.metadata.version ?? 0
    const options = version >= 4 ? { mappings: cleanCardMappings(this.state.mappings), expected_fields_revision: this.state.preview.fields_revision,
      ...(version >= 5 ? { expected_categories_revision: this.state.preview.categories_revision } : {}),
      ...(version >= 6 ? { expected_ai_visibility_revision: this.state.preview.ai_visibility_revision } : {}),
    } : {}
    this.update({ importing: true, error: null })
    try {
      const result = await this.api.import(this.pid, this.state.data, copyChoices(this.state.choices), options, request.signal)
      if (!this.valid(generation, request)) return
      if (!this.canWrite()) {
        this.update({ importing: false, needsPreview: true, reviewRequired: true, error: '作品编辑权限已变化，请重新核对。' })
        return
      }
      this.update({ result, importing: false })
      this.imported()
    } catch (err) {
      if (!this.valid(generation, request)) return
      if (this.denyIfNeeded(err)) return
      const needsReview = writeResultUnknown(err) || err instanceof ApiError && err.status === 409
      this.update({ importing: false, error: writeResultUnknown(err) ? '导入结果尚不明确，请重新预览并核对。' : errorMessage(err),
        resultUnknown: writeResultUnknown(err), reviewRequired: needsReview, reviewPreview: null, needsPreview: true })
    }
  }
}
