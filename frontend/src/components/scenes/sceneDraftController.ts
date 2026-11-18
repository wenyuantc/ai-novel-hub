import { ApiError, errorMessage } from '../../api/client'
import type { RichDocument, RichDocumentOut, RichDocumentSave } from '../../api/richDocument'
import type { Scene, ScenePage, SceneUpdate } from '../../api/types'
import { writeResultUnknown } from '../../utils/writeResult'
import { richDocumentSaveError } from './richDocumentCapacity'
import { appendSceneEvidence, documentSave, emptyEvidence, rebaseDraftDocument, sameDocument } from './sceneDraftEvidence'
import { decodeSceneDraftRecord, trimSceneTitle, type RichEditEvidence, type SceneDraft, type SceneDraftBase,
  type SceneDraftRecord, type SceneSaveAttempt } from './sceneDraftRecord'
import { validSectionDocument } from './richSectionValidity'

export { decodeSceneDraftRecord, trimSceneTitle }
export type { RichEditEvidence, SceneDraft, SceneDraftRecord, SceneSaveAttempt, SceneEditEvidence } from './sceneDraftRecord'

export interface SceneConflictStamp { sceneId: number; revision: number; planningRevision: number; editSeq: number }
export type SceneBeforeWrite = (record: SceneDraftRecord) => Promise<void | false>
type RestoreResult = 'restored' | 'conflict' | 'unknown' | 'invalid'
type TargetGuard = () => boolean
type SceneRead = { scene: Scene; document: RichDocumentOut | null }
export interface SceneDraftSnapshot {
  scene: Scene | null
  draft: SceneDraft
  page: ScenePage | null
  loading: boolean
  saving: boolean
  composing: boolean
  error: string | null
  conflict: Scene | null
  externalChange: boolean
  document: RichDocumentOut | null
  documentDraft: RichDocument | null
  conflictDocument: RichDocumentOut | null
  editSeq: number
  bodyEditSeq: number
  draftVersion: number
  unknownSave: boolean
  unknownRetryable: boolean
  lastOwnSave: { sceneId: number; from: number; to: number } | null
}
export interface SceneDraftApi {
  list: (signal: AbortSignal, isCurrent?: TargetGuard) => Promise<ScenePage>
  get: (sid: number, signal?: AbortSignal, isCurrent?: TargetGuard) => Promise<Scene>
  update: (sid: number, body: SceneUpdate, isCurrent?: TargetGuard) => Promise<Scene>
  getDocument?: (sid: number, signal?: AbortSignal, isCurrent?: TargetGuard) => Promise<RichDocumentOut>
  saveDocument?: (sid: number, body: RichDocumentSave, isCurrent?: TargetGuard) => Promise<RichDocumentOut>
}
const empty = (): SceneDraft => ({ title: '', content: '', summary: '' })
const draftOf = ({ title, content, summary }: SceneDraft): SceneDraft => ({ title, content, summary })
const sameDraft = (a: SceneDraft, b: SceneDraft) =>
  a.title === b.title && a.content === b.content && a.summary === b.summary
const sameIdentity = (a: SceneDraftBase, b: SceneDraftBase) =>
  a.id === b.id && a.chapter_id === b.chapter_id && a.created_at === b.created_at
const sameOptionalDocument = (a: RichDocumentOut | null, b: RichDocumentOut | null) =>
  a && b ? sameDocument(a.document, b.document) : a === b
export const sceneDocumentDirty = ({ scene, draft, document, documentDraft }: SceneDraftSnapshot): boolean =>
  !!scene && (draft.content !== scene.content || !!document && !!documentDraft && !sameDocument(document.document, documentDraft))
export const sceneMetadataDirty = ({ scene, draft }: SceneDraftSnapshot): boolean =>
  !!scene && (draft.title !== scene.title || draft.summary !== scene.summary)
export const sceneDraftDirty = (snapshot: SceneDraftSnapshot): boolean =>
  !!snapshot.scene && (snapshot.composing || sceneMetadataDirty(snapshot) || sceneDocumentDirty(snapshot))

/** 无可重新定位的元数据时，作者可明确用本地全文替换对端全文。 */
function noDocumentPositions(document: RichDocument): boolean {
  const { formatting } = document
  return !document.sections.length && !document.markers.length && !document.retired_section_keys.length
    && !formatting.boundaries.length && !formatting.inline.length && !formatting.blocks.length
}

/** 一章一授权身份的草稿控制器；保存失败不切场景，冲突只展示，核对前不替换 revision。 */
export class SceneDraftController {
  private active = false
  private generation = 0
  private request: AbortController | null = null
  private pendingRefresh = false
  private listeners = new Set<() => void>()
  private evidence = emptyEvidence()
  private retiredMarkerKeys = new Set<string>()
  private saveSerial = 0
  private activeSave: number | null = null
  private attemptSerial = 0
  private attempt: SceneSaveAttempt | null = null
  private recoveredAttempt = false
  private reviewSerial = 0
  private inputIsBlocked: (() => boolean) | null = null
  private compositionWaiters = new Set<() => void>()
  private boundAuthorityVersion: number
  private snapshot: SceneDraftSnapshot = { scene: null, draft: empty(), page: null, loading: true, saving: false,
    composing: false, error: null, conflict: null, externalChange: false, document: null, documentDraft: null, conflictDocument: null,
    editSeq: 0, bodyEditSeq: 0, draftVersion: 0, unknownSave: false, unknownRetryable: false, lastOwnSave: null }
  constructor(private readonly chapterId: number, private readonly api: SceneDraftApi, private readonly canRead: () => boolean,
    private readonly canEdit: () => boolean, private readonly authorityVersion: () => number = () => 0) {
    this.boundAuthorityVersion = authorityVersion()
  }
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  setInputBarrier(isBlocked: (() => boolean) | null) { this.inputIsBlocked = isBlocked }
  activate() { this.active = true }
  deactivate() {
    this.active = false
    this.generation += 1
    this.request?.abort()
    this.request = null
    this.pendingRefresh = false
    this.releaseCompositionWaiters()
    this.snapshot = { ...this.snapshot, composing: false }
  }

  private clearEvidence() {
    this.evidence = emptyEvidence()
    this.retiredMarkerKeys.clear()
  }

  captureDraft(): SceneDraftRecord | null {
    const { scene, document, draft, documentDraft, editSeq, bodyEditSeq } = this.snapshot
    if (!scene || !this.active || !this.allowedRead() || !this.allowedEdit()) return null
    const { id, chapter_id, created_at, title, summary, content, content_revision, planning_revision } = scene
    return {
      base: { id, chapter_id, created_at, title, summary, content, content_revision, planning_revision },
      document, draft, documentDraft, evidence: this.evidence, retiredMarkerKeys: [...this.retiredMarkerKeys],
      attempt: this.attempt, editSeq, bodyEditSeq,
    }
  }

  /** 授权轮次变化时丢弃旧响应；仍有写权时保留草稿，已发送的保存先等待并要求核对。 */
  authorizationChanged() {
    this.boundAuthorityVersion = this.authorityVersion()
    if (!this.active) return
    const wasLoading = this.snapshot.loading
    const unknownSave = !!this.attempt && (this.snapshot.saving || this.snapshot.unknownSave)
    this.generation += 1
    this.request?.abort(); this.request = null; this.pendingRefresh = false
    this.releaseCompositionWaiters()
    if (!this.allowedRead()) {
      this.clearEvidence()
      this.attempt = null
      this.publish({ scene: null, page: null, draft: empty(), loading: false, saving: false, composing: false,
        error: null, conflict: null, externalChange: false, document: null, documentDraft: null, conflictDocument: null,
        unknownSave: false, unknownRetryable: false })
      return
    }
    if (!this.allowedEdit()) {
      this.clearEvidence()
      this.attempt = null
      this.publish({ draft: this.snapshot.scene ? draftOf(this.snapshot.scene) : empty(),
        documentDraft: this.snapshot.document?.document ?? null, conflict: null, conflictDocument: null,
        loading: false, saving: this.activeSave !== null, composing: false, externalChange: unknownSave,
        unknownSave: false, unknownRetryable: false,
        error: unknownSave ? '权限已变化，先前保存的结果尚待核对。' : null })
    } else {
      this.publish({ loading: false, saving: this.activeSave !== null, ...(unknownSave ? {
        externalChange: true, unknownSave: true, unknownRetryable: false,
        error: '权限已变化，先前保存的结果尚待核对。',
      } : {}) })
    }
    if (wasLoading && this.activeSave === null) void this.load()
  }
  private publish(patch: Partial<SceneDraftSnapshot>) { this.snapshot = { ...this.snapshot, ...patch }; this.listeners.forEach((fn) => fn()) }
  private publishDraft(patch: Partial<SceneDraftSnapshot>) {
    this.publish({ ...patch, draftVersion: this.snapshot.draftVersion + 1 })
  }
  private allowedRead() { return this.boundAuthorityVersion === this.authorityVersion() && this.canRead() }
  private allowedEdit() { return this.boundAuthorityVersion === this.authorityVersion() && this.canEdit() }
  private valid(generation: number) { return this.active && generation === this.generation && this.allowedRead() }
  private releaseCompositionWaiters() {
    for (const resolve of this.compositionWaiters) resolve()
    this.compositionWaiters.clear()
  }
  setComposing(composing: boolean) {
    if (!this.active || this.snapshot.composing === composing) return
    this.publish({ composing })
    if (!composing) this.releaseCompositionWaiters()
    if (!composing && this.pendingRefresh && !this.snapshot.saving) { this.pendingRefresh = false; void this.load() }
  }
  change(patch: Partial<SceneDraft>) {
    if (!this.active || !this.allowedEdit() || !this.snapshot.scene || this.inputIsBlocked?.()) return
    // 已加载完整文档后，字符串入口不能覆盖结构草稿。
    if (this.snapshot.document && patch.content !== undefined && patch.content !== this.snapshot.draft.content) return
    const draft = { ...this.snapshot.draft, ...patch }
    if (sameDraft(draft, this.snapshot.draft)) return
    this.publishDraft({ draft, editSeq: this.snapshot.editSeq + 1, bodyEditSeq: this.snapshot.bodyEditSeq
      + (draft.content === this.snapshot.draft.content ? 0 : 1), error: null })
  }
  changeDocument(document: RichDocument, evidence: RichEditEvidence = { mappedMarkerKeys: [], explicitMarkerIntent: false }) {
    if (!this.active || !this.allowedEdit() || !this.snapshot.document || !this.snapshot.documentDraft) return
    if (!this.snapshot.composing && this.inputIsBlocked?.()) return
    const source = this.snapshot.documentDraft
    if (sameDocument(source, document)) return
    if (!validSectionDocument(document)) { this.publish({ error: '完整文档的正文、区块或标记结构无效，草稿保持不变。' }); return }
    const oldSections = new Map(source.sections.map((item) => [item.key, item]))
    const newSections = new Map(document.sections.map((item) => [item.key, item]))
    const addedSections = [...newSections.keys()].filter((key) => !oldSections.has(key))
    const removedSections = [...oldSections.keys()].filter((key) => !newSections.has(key))
    if ((addedSections.length || removedSections.length) && !evidence.explicitSectionIntent) {
      this.publish({ error: '区块增删缺少明确事务，完整文档保持不变。' }); return
    }
    const base = this.snapshot.document.document
    const persistedSections = new Set([...base.sections.map((item) => item.key), ...base.retired_section_keys])
    const expectedRetired = new Set(source.retired_section_keys)
    for (const key of removedSections) if (persistedSections.has(key)) expectedRetired.add(key)
    for (const key of addedSections) {
      if (expectedRetired.has(key) && !evidence.restoredSectionKeys?.includes(key)) {
        this.publish({ error: '恢复已删除区块缺少历史事务，完整文档保持不变。' }); return
      }
      expectedRetired.delete(key)
    }
    if (expectedRetired.size !== document.retired_section_keys.length
      || document.retired_section_keys.some((key) => !expectedRetired.has(key))) {
      this.publish({ error: '区块退场身份缺少明确事务，完整文档保持不变。' }); return
    }
    const mappedSections = new Set(evidence.mappedSectionKeys ?? [])
    for (const [key, old] of oldSections) {
      const next = newSections.get(key)
      if (!next) continue
      if (source.content !== document.content && !mappedSections.has(key)) {
        this.publish({ error: '区块锚点缺少可靠事务映射，完整文档保持不变。' }); return
      }
      if ((old.content !== next.content || JSON.stringify(old.formatting) !== JSON.stringify(next.formatting)) && !mappedSections.has(key)) {
        this.publish({ error: '区块文字或格式缺少可靠事务映射，完整文档保持不变。' }); return
      }
      if ((old.anchor !== next.anchor || old.order !== next.order || old.title !== next.title || old.include_in_ai !== next.include_in_ai)
        && !mappedSections.has(key) && !evidence.explicitSectionIntent) {
        this.publish({ error: '区块资料缺少明确事务，完整文档保持不变。' }); return
      }
    }
    const mapped = new Set(evidence.mappedMarkerKeys)
    const before = new Map(source.markers.map((item) => [item.key, item]))
    const after = new Map(document.markers.map((item) => [item.key, item]))
    const membershipChanged = before.size !== after.size || [...before.keys()].some((key) => !after.has(key))
    if (membershipChanged && !evidence.explicitMarkerIntent) {
      this.publish({ error: '标记增删缺少明确事务，完整文档保持不变。' }); return
    }
    for (const key of after.keys()) {
      if (!before.has(key) && this.retiredMarkerKeys.has(key) && !evidence.restoredMarkerKeys?.includes(key)) {
        this.publish({ error: '恢复已删除标记缺少历史事务，完整文档保持不变。' }); return
      }
    }
    for (const [key, old] of before) {
      const next = after.get(key)
      if (!next) continue
      if (old.section_key !== next.section_key) { this.publish({ error: '标记目标不能通过普通编辑更换，完整文档保持不变。' }); return }
      const oldText = old.section_key === null ? source.content : oldSections.get(old.section_key)?.content
      const newText = next.section_key === null ? document.content : newSections.get(next.section_key)?.content
      if ((oldText !== newText && old.status === 'active' && !mapped.has(key))
        || (JSON.stringify(old) !== JSON.stringify(next) && !mapped.has(key) && !evidence.explicitMarkerIntent)) {
        this.publish({ error: '标记缺少可靠事务映射，完整文档保持不变。' }); return
      }
    }
    this.evidence = appendSceneEvidence(this.evidence, evidence, document,
      (evidence.restoredMarkerKeys ?? []).filter((key) => !before.has(key) && after.has(key)),
      (evidence.restoredSectionKeys ?? []).filter((key) => !oldSections.has(key) && newSections.has(key)))
    this.publishDraft({ documentDraft: document, draft: { ...this.snapshot.draft, content: document.content },
      editSeq: this.snapshot.editSeq + 1, bodyEditSeq: this.snapshot.bodyEditSeq + 1, error: null })
  }
  private sameBase(read: SceneRead): boolean {
    const { scene, document } = this.snapshot
    return !!scene && sameIdentity(read.scene, scene) && read.scene.content_revision === scene.content_revision
      && sameDraft(read.scene, scene) && sameOptionalDocument(document, read.document)
  }

  private async readScene(sid: number, isCurrent: TargetGuard, signal?: AbortSignal): Promise<SceneRead | null> {
    const scene = await this.api.get(sid, signal, isCurrent)
    if (!isCurrent()) return null
    if (scene.id !== sid || scene.chapter_id !== this.chapterId || !scene.active) {
      throw new Error('返回的场景与当前章节不一致，请重试。')
    }
    const document = this.api.getDocument ? await this.api.getDocument(sid, signal, isCurrent) : null
    if (!isCurrent()) return null
    if (document && (document.content_revision !== scene.content_revision
      || document.planning_revision !== scene.planning_revision || document.document.content !== scene.content)) {
      throw new Error('完整文档与场景版本不一致，请重新加载后核对。')
    }
    return { scene, document }
  }

  private pageWith(scene: Scene): ScenePage | null {
    const { page } = this.snapshot
    if (!page) return null
    return {
      ...page,
      content_revision: scene.content_revision,
      planning_revision: scene.planning_revision,
      items: page.items.map((item) => item.id === scene.id ? scene : { ...item,
        chapter_title: scene.chapter_title, content_revision: scene.content_revision, planning_revision: scene.planning_revision }),
    }
  }

  /** 外部刷新先核对基线，不修改仍在编辑或待核对的草稿。 */
  async load(preferredId?: number, preserveDraft = true): Promise<boolean> {
    if (!this.active || !this.allowedRead() || !preserveDraft && this.snapshot.unknownSave) return false
    if (this.snapshot.saving || this.activeSave !== null || this.snapshot.composing) {
      if (preserveDraft && preferredId === undefined) this.pendingRefresh = true
      return false
    }
    const previous = this.snapshot.scene
    if (preferredId !== undefined && previous && previous.id !== preferredId && sceneDraftDirty(this.snapshot)) return false
    const editSeq = this.snapshot.editSeq
    this.request?.abort()
    const request = new AbortController()
    this.request = request
    const generation = ++this.generation
    const isCurrent = () => this.valid(generation) && !request.signal.aborted
    this.publish({ loading: true, error: null })
    try {
      const page = await this.api.list(request.signal, isCurrent)
      if (!isCurrent()) return false
      if (preferredId !== undefined && !page.items.some((item) => item.id === preferredId)) {
        throw new Error('指定场景已退场或不属于当前章节，请核对来源；不会自动定位到同序号的其他场景。')
      }
      const preserve = preserveDraft && (sceneDraftDirty(this.snapshot) || this.snapshot.conflict !== null || this.attempt !== null)
      if (preserve && previous && !page.items.some((item) => item.id === previous.id)) {
        this.publish({ page, loading: false, externalChange: true,
          error: '当前场景已退场或不可用。草稿不会自动写入其他场景。' })
        return false
      }
      const sid = [preferredId, previous?.id, page.items[0]?.id].find((id) => id !== undefined && page.items.some((item) => item.id === id))
      if (sid === undefined) throw new Error('该章节缺少初始场景，请联系管理员检查迁移或建章入口。')
      const read = await this.readScene(sid, isCurrent, request.signal)
      if (!read) return false
      const { scene, document } = read
      if (page.content_revision !== scene.content_revision || page.planning_revision !== scene.planning_revision) {
        throw new Error('加载期间场景版本已变化，请重新加载后核对。')
      }
      // 切换和显式替换读请求发出后若仍有作者输入，原编辑区必须保留。
      if ((sid !== previous?.id || !preserveDraft) && (this.snapshot.editSeq !== editSeq || this.snapshot.composing)) {
        this.publish({ loading: false })
        return false
      }
      const sameTarget = !!previous && sameIdentity(previous, scene)
      if (sameTarget && this.snapshot.scene && scene.content_revision < this.snapshot.scene.content_revision) {
        throw new Error('读到较早的场景版本，请重新读取。')
      }
      if (preserveDraft && previous?.id === sid && (sceneDraftDirty(this.snapshot) || this.snapshot.conflict || this.attempt)) {
        if (!sameTarget) {
          this.publish({ page, loading: false, externalChange: true, error: '原场景已不可用，请保留需要的草稿。' })
          return false
        }
        const sameBase = this.sameBase(read)
        if (sameBase && !this.attempt && !this.snapshot.conflict) {
          const patch = { page, loading: false, scene, document, externalChange: false }
          if (previous.planning_revision !== scene.planning_revision) this.publishDraft(patch)
          else this.publish(patch)
        } else {
          this.publish({ page, loading: false, externalChange: true, conflict: scene, conflictDocument: document })
        }
      } else {
        if (!sameTarget || !this.sameBase(read)) this.clearEvidence()
        this.attempt = null
        this.publish({ page, scene, document, documentDraft: document?.document ?? null, draft: draftOf(scene),
          loading: false, conflict: null, conflictDocument: null, externalChange: false, unknownSave: false,
          unknownRetryable: false, ...(!sameTarget ? { lastOwnSave: null } : {}) })
      }
      return true
    } catch (error) {
      if (isCurrent()) this.publish({ loading: false, error: errorMessage(error) })
      return false
    } finally {
      if (this.request === request) this.request = null
    }
  }

  /** 发送中接收作者新输入，仅冻结本次应提交的字段。 */
  async save(mode: 'all' | 'document' = 'all', beforeWrite?: SceneBeforeWrite): Promise<Scene | null> {
    const initial = this.snapshot
    if (initial.composing) {
      this.publish({ error: '输入法组合尚未结束，请确认文字后再保存。' })
      return null
    }
    if (!initial.scene || !this.canStartSave() || initial.conflict || initial.externalChange || this.attempt) return null
    const bodyChanged = sceneDocumentDirty(initial)
    const metadataChanged = mode === 'all' && sceneMetadataDirty(initial)
    if (!bodyChanged && !metadataChanged) return initial.scene

    return this.runSave(async (isCurrent) => {
      if (bodyChanged && initial.document && initial.documentDraft) {
        if (!this.api.saveDocument) throw new Error('完整文档保存入口不可用，草稿没有提交。')
        const payload = documentSave(initial.document, initial.documentDraft, this.evidence, this.retiredMarkerKeys)
        const attempt: SceneSaveAttempt = { id: ++this.attemptSerial, editSeq: initial.editSeq,
          baseRevision: initial.document.content_revision, kind: 'document', payload,
          draft: { ...draftOf(initial.scene!), content: payload.document.content }, evidence: this.evidence }
        if (!await this.sendAttempt(attempt, isCurrent, beforeWrite)) return null
        if (!await this.refreshConfirmedScene(isCurrent)) return null
      }
      if (!isCurrent()) return null
      const base = this.snapshot.scene!
      const withPlainBody = bodyChanged && !initial.document
      const intended = draftOf(base)
      if (metadataChanged) {
        intended.title = initial.draft.title
        intended.summary = initial.draft.summary
      }
      if (withPlainBody) intended.content = initial.draft.content
      const savedTitle = trimSceneTitle(intended.title)
      if (sameDraft({ ...intended, title: savedTitle }, base)) {
        // 规范化后等于原值时没有服务器写入，只确认仍等于本次输入的标题。
        if (metadataChanged && this.snapshot.draft.title === initial.draft.title
          && this.snapshot.draft.title !== savedTitle) {
          this.publishDraft({ draft: { ...this.snapshot.draft, title: savedTitle } })
        }
        return base
      }
      const attempt: SceneSaveAttempt = { id: ++this.attemptSerial, editSeq: initial.editSeq,
        baseRevision: base.content_revision, kind: withPlainBody ? 'plain' : 'metadata',
        draft: intended, payload: null, evidence: emptyEvidence() }
      const saved = await this.sendAttempt(attempt, isCurrent, beforeWrite)
      return saved ? this.snapshot.scene : null
    })
  }

  private canStartSave() {
    return this.active && this.allowedRead() && this.allowedEdit() && !this.snapshot.loading
      && !this.snapshot.saving && !this.snapshot.composing && this.activeSave === null
  }

  private async runSave(work: (isCurrent: TargetGuard) => Promise<Scene | null>): Promise<Scene | null> {
    const generation = this.generation
    const scene = this.snapshot.scene!
    const saveSerial = ++this.saveSerial
    this.activeSave = saveSerial
    const isCurrent = () => this.valid(generation) && this.allowedEdit() && this.activeSave === saveSerial
      && !!this.snapshot.scene && sameIdentity(scene, this.snapshot.scene)
    this.publish({ saving: true, error: null })
    try {
      return await work(isCurrent)
    } catch (error) {
      if (isCurrent()) this.publish({ error: richDocumentSaveError(errorMessage(error)) })
      return null
    } finally {
      if (this.activeSave === saveSerial) {
        const stillCurrent = isCurrent()
        this.activeSave = null
        if (!stillCurrent && this.attempt && this.active) {
          // 已离开旧授权轮次；不根据旧 HTTP 回执处理任何后续编辑。
          this.snapshot = { ...this.snapshot, unknownSave: true, unknownRetryable: false, externalChange: true }
        }
        if (this.active && this.allowedRead()) {
          this.publish({ saving: false })
          if (this.pendingRefresh) {
            this.pendingRefresh = false
            void this.load()
          }
        } else {
          this.snapshot = { ...this.snapshot, saving: false }
        }
      }
    }
  }

  private async sendAttempt(attempt: SceneSaveAttempt, isCurrent: TargetGuard, beforeWrite?: SceneBeforeWrite,
    retry = false): Promise<boolean> {
    const origin = this.snapshot.scene!
    const baseDocument = this.snapshot.document
    if (!retry) {
      this.attempt = attempt
      this.recoveredAttempt = false
      if (attempt.payload) this.evidence = emptyEvidence()
      this.publishDraft({ unknownRetryable: false })
    }
    const currentAttempt = () => isCurrent() && this.attempt === attempt
    if (beforeWrite) {
      const record = this.captureDraft()
      let allowed: void | false = undefined
      try {
        allowed = record ? await beforeWrite(record) : false
      } catch {
        // 本地存储错误由草稿仓库显示。服务器仍可接收安全的条件写入。
      }
      if (!currentAttempt()) return false
      if (allowed === false) {
        if (!retry) this.rejectAttempt()
        return false
      }
    }
    if (!currentAttempt()) return false
    // 等待落盘期间可能开始组合输入。保持本次的冻结内容，等输入稳定再发送。
    if (this.snapshot.composing) {
      await new Promise<void>((resolve) => { this.compositionWaiters.add(resolve) })
      if (!currentAttempt()) return false
    }

    try {
      let response: SceneRead
      if (attempt.payload) {
        if (!this.api.saveDocument) throw new Error('完整文档保存入口不可用。')
        const document = await this.api.saveDocument(origin.id, attempt.payload, currentAttempt)
        const scene = { ...origin, content: document.document.content, content_revision: document.content_revision,
          planning_revision: document.planning_revision }
        response = { scene, document }
      } else {
        const update: SceneUpdate = { expected_content_revision: attempt.baseRevision }
        if (attempt.draft.title !== origin.title) update.title = attempt.draft.title
        if (attempt.draft.summary !== origin.summary) update.summary = attempt.draft.summary
        if (attempt.kind === 'plain') update.content = attempt.draft.content
        const scene = await this.api.update(origin.id, update, currentAttempt)
        const document = baseDocument && { ...baseDocument,
          content_revision: scene.content_revision, planning_revision: scene.planning_revision }
        response = { scene, document }
      }
      if (!currentAttempt()) return false
      if (!this.matchesAttempt(response, attempt)) {
        this.markUnknown('保存响应与本次场景草稿不一致，请读取最新版本核对。')
        return false
      }
      // 延迟派生元数据同步，组合提交的文本仍进入下一份草稿。
      if (attempt.payload && this.snapshot.composing) {
        await new Promise<void>((resolve) => { this.compositionWaiters.add(resolve) })
        if (!currentAttempt()) return false
      }
      this.acceptAttempt(response, attempt, true)
      return true
    } catch (error) {
      if (!currentAttempt()) return false
      if (retry || writeResultUnknown(error) || error instanceof ApiError && [401, 403, 404].includes(error.status)) {
        this.markUnknown('保存结果尚未确认，请读取最新场景并核对。')
      } else {
        this.rejectAttempt()
        this.publish({ error: richDocumentSaveError(errorMessage(error)) })
      }
      if (error instanceof ApiError && error.status === 409) await this.readLatest(isCurrent, true)
      return false
    }
  }

  private markUnknown(message: string) {
    this.publishDraft({ error: message, unknownSave: true, unknownRetryable: false, externalChange: true })
  }

  private rejectAttempt() {
    if (this.attempt?.payload && this.snapshot.documentDraft) {
      this.evidence = appendSceneEvidence(this.attempt.evidence, this.evidence, this.snapshot.documentDraft,
        this.evidence.restoredMarkerKeys, this.evidence.restoredSectionKeys)
    }
    this.attempt = null
    this.publishDraft({ unknownSave: false, unknownRetryable: false })
  }

  private matchesAttempt(read: SceneRead, attempt: SceneSaveAttempt): boolean {
    const { scene, document } = this.snapshot
    if (!scene || !sameIdentity(scene, read.scene) || !read.scene.active
      || read.scene.content_revision !== attempt.baseRevision + 1
      || !sameDraft(read.scene, { ...attempt.draft, title: attempt.payload ? attempt.draft.title : trimSceneTitle(attempt.draft.title) })) return false
    return attempt.payload ? !!read.document && sameDocument(read.document.document, attempt.payload.document)
      : sameOptionalDocument(document, read.document)
  }

  /** 仅确认已提交字段与基线版本，继续保留之后的输入和证据。 */
  private acceptAttempt(read: SceneRead, attempt: SceneSaveAttempt, ownReceipt: boolean) {
    const { scene, document } = read
    const draft = { ...this.snapshot.draft }
    let documentDraft = this.snapshot.documentDraft
    if (attempt.payload && document && documentDraft) {
      for (const key of attempt.payload.delete_marker_keys) this.retiredMarkerKeys.add(key)
      for (const key of attempt.payload.restore_marker_keys ?? []) this.retiredMarkerKeys.delete(key)
      documentDraft = sameDocument(documentDraft, attempt.payload.document) ? document.document
        : rebaseDraftDocument(documentDraft, document.document)
      draft.content = documentDraft.content
    } else {
      if (draft.title === attempt.draft.title) draft.title = scene.title
      if (draft.summary === attempt.draft.summary) draft.summary = scene.summary
      if (attempt.kind === 'plain' && draft.content === attempt.draft.content) draft.content = scene.content
    }
    if (document && documentDraft && sameDocument(document.document, documentDraft)) this.evidence = emptyEvidence()
    this.attempt = null
    this.publishDraft({ scene, document, documentDraft, draft, page: this.pageWith(scene), externalChange: false,
      conflict: null, conflictDocument: null, unknownSave: false, unknownRetryable: false, error: null,
      ...(ownReceipt ? { lastOwnSave: { sceneId: scene.id, from: attempt.baseRevision, to: scene.content_revision } } : {}) })
  }

  /** 富文档 PUT 确认后已推进基线。场景附加资料读取失败也不能再次发送原正文。 */
  private async refreshConfirmedScene(isCurrent: TargetGuard): Promise<boolean> {
    const { scene, document } = this.snapshot
    if (!scene || !document) return false
    try {
      const latest = await this.api.get(scene.id, undefined, isCurrent)
      if (!isCurrent()) return false
      if (!sameIdentity(latest, scene) || !latest.active || latest.content_revision !== scene.content_revision || !sameDraft(latest, scene)) {
        this.publish({ externalChange: true, error: '正文已保存，但场景随后又变化，请重新核对。' })
        return false
      }
      this.publishDraft({ scene: latest, document: { ...document, planning_revision: latest.planning_revision }, page: this.pageWith(latest) })
      return true
    } catch (error) {
      if (!isCurrent()) return false
      this.publish({ error: richDocumentSaveError(errorMessage(error)), externalChange: true })
      return false
    }
  }

  async select(sid: number): Promise<boolean> {
    if (this.snapshot.saving || this.activeSave !== null || this.snapshot.unknownSave) return false
    if (this.snapshot.scene?.id === sid) return true
    if (sceneDraftDirty(this.snapshot) && !(await this.save())) return false
    if (sceneDraftDirty(this.snapshot)) return false
    return this.load(sid, false)
  }

  async reviewLatest(): Promise<boolean> {
    if (this.activeSave !== null || this.snapshot.loading) return false
    const generation = this.generation
    const initial = this.snapshot.scene
    const review = ++this.reviewSerial
    const isCurrent = () => this.valid(generation) && this.activeSave === null && !!initial
      && this.snapshot.scene === initial && this.reviewSerial === review
    return this.readLatest(isCurrent)
  }

  private async readLatest(isCurrent: TargetGuard, forceConflict = false): Promise<boolean> {
    const initial = this.snapshot.scene
    if (!initial || !isCurrent()) return false
    try {
      const read = await this.readScene(initial.id, isCurrent)
      if (!read) return false
      if (!sameIdentity(read.scene, initial) || read.scene.content_revision < initial.content_revision) {
        throw new Error('原场景身份或版本无法核对，请保留本地输入。')
      }
      if (this.attempt) {
        if (this.matchesAttempt(read, this.attempt) && this.snapshot.composing) {
          await new Promise<void>((resolve) => { this.compositionWaiters.add(resolve) })
          if (!isCurrent()) return false
        }
        this.settleAgainst(read)
      } else if (this.sameBase(read) && !forceConflict && !this.snapshot.conflict) {
        const patch = { ...read, page: this.pageWith(read.scene), externalChange: false, error: null }
        if (read.scene.planning_revision !== initial.planning_revision) this.publishDraft(patch)
        else this.publish(patch)
      } else {
        this.publish({ conflict: read.scene, conflictDocument: read.document, externalChange: true })
      }
      return true
    } catch (error) {
      if (isCurrent()) this.publish({ error: errorMessage(error), externalChange: true, unknownRetryable: false })
      return false
    }
  }

  private settleAgainst(read: SceneRead) {
    const attempt = this.attempt!
    if (this.matchesAttempt(read, attempt)) {
      this.acceptAttempt(read, attempt, !this.recoveredAttempt)
      return
    }
    const fenced = read.scene.content_revision > attempt.baseRevision
    const unchanged = this.sameBase(read)
    this.publishDraft({ conflict: read.scene, conflictDocument: read.document, externalChange: true,
      unknownSave: !fenced, unknownRetryable: unchanged && !fenced,
      error: fenced ? '服务器版本已变化，请比较草稿。'
        : unchanged ? '仍需确认上次保存结果。可以按原版本重试上次保存。' : '无法确认上次保存，请重新核对。' })
  }

  /** 远端仍为 B 时只重发持久化的原快照，不消费作者后续输入。 */
  async retryUnknown(beforeWrite?: SceneBeforeWrite): Promise<Scene | null> {
    if (!this.canStartSave() || !this.snapshot.unknownRetryable || !this.attempt || !this.snapshot.scene) return null
    const attempt = this.attempt
    return this.runSave(async (isCurrent) => {
      if (!await this.sendAttempt(attempt, isCurrent, beforeWrite, true)) return null
      if (attempt.payload && !await this.refreshConfirmedScene(isCurrent)) return null
      return this.snapshot.scene
    })
  }

  /** 安装草稿前须核对同一活动场景的完整服务端快照。调用方负责先保留当前稿。 */
  async restoreDraft(value: unknown): Promise<RestoreResult> {
    const record = decodeSceneDraftRecord(value)
    const initial = this.snapshot.scene
    if (!record || !initial || !sameIdentity(record.base, initial) || !this.canStartSave() || this.snapshot.unknownSave) return 'invalid'
    const attempt = this.verifyStoredAttempt(record)
    if (!!record.document !== !!this.snapshot.document || attempt === false) return 'invalid'
    const generation = this.generation
    const oldDraftVersion = this.snapshot.draftVersion
    const isCurrent = () => this.valid(generation) && this.allowedEdit() && this.activeSave === null && !this.snapshot.composing
      && this.snapshot.draftVersion === oldDraftVersion && !!this.snapshot.scene && sameIdentity(initial, this.snapshot.scene)
    try {
      const read = await this.readScene(initial.id, isCurrent)
      if (!read || !isCurrent() || !sameIdentity(record.base, read.scene)
        || read.scene.content_revision < initial.content_revision || read.scene.content_revision < record.base.content_revision) return 'invalid'
      const base = { ...read.scene, ...record.base }
      const mismatch = read.scene.content_revision !== record.base.content_revision
        || !sameDraft(read.scene, base) || !sameOptionalDocument(read.document, record.document)
      this.evidence = record.evidence
      this.retiredMarkerKeys = new Set(record.retiredMarkerKeys)
      this.attempt = attempt
      this.recoveredAttempt = !!attempt
      this.attemptSerial = Math.max(this.attemptSerial, record.attempt?.id ?? 0)
      // 恢复是状态替换而非新输入。令以往比较确认失效，避免使用原分支的序号解锁。
      const editSeq = Math.max(this.snapshot.editSeq, record.editSeq) + 1
      this.publishDraft({ scene: mismatch ? base : read.scene, document: mismatch ? record.document : read.document,
        draft: record.draft, documentDraft: record.documentDraft, editSeq, bodyEditSeq: this.snapshot.bodyEditSeq,
        conflict: mismatch ? read.scene : null, conflictDocument: mismatch ? read.document : null,
        externalChange: mismatch || !!record.attempt, unknownSave: !!record.attempt, unknownRetryable: false, error: null })
      if (this.attempt) this.settleAgainst(read)
      return this.snapshot.unknownSave ? 'unknown' : this.snapshot.conflict ? 'conflict' : 'restored'
    } catch (error) {
      if (isCurrent()) this.publish({ error: errorMessage(error) })
      return 'invalid'
    }
  }

  private verifyStoredAttempt(record: SceneDraftRecord): SceneSaveAttempt | null | false {
    const attempt = record.attempt
    if (!attempt?.payload) return attempt
    if (!record.document) return false
    try {
      const checked = documentSave(record.document, attempt.payload.document, attempt.evidence, new Set(record.retiredMarkerKeys))
      // 重新计算所有意图。落盘数据若损坏，不可把原载荷直接交给 API。
      const { document: ignored, ...guard } = checked
      const { document: ignoredSaved, ...stored } = attempt.payload
      void ignored; void ignoredSaved
      return JSON.stringify(guard) === JSON.stringify(stored) && { ...attempt, payload: checked }
    } catch {
      return false
    }
  }

  private latestForChoice(expected?: SceneConflictStamp): SceneRead | null {
    const { scene, conflict, conflictDocument, editSeq, unknownSave, loading, saving, composing } = this.snapshot
    if (!this.active || !this.allowedEdit() || !this.allowedRead() || this.activeSave !== null
      || !scene || !conflict || !sameIdentity(scene, conflict) || unknownSave || loading || saving || composing) return null
    if (expected && (expected.sceneId !== conflict.id || expected.revision !== conflict.content_revision
      || expected.planningRevision !== conflict.planning_revision || expected.editSeq !== editSeq)) return null
    return { scene: conflict, document: conflictDocument }
  }

  canAcknowledgeLatest(): boolean {
    const latest = this.latestForChoice()
    if (!latest) return false
    if (!sceneDocumentDirty(this.snapshot)) return true
    const { document, documentDraft } = this.snapshot
    if (!document || !documentDraft || !latest.document) return !document && !latest.document
    return sameDocument(document.document, latest.document.document)
      || noDocumentPositions(document.document) && noDocumentPositions(latest.document.document)
  }

  /** 已核对的远端版本成为新条件，作者的有效输入留给下一次显式提交。 */
  acknowledgeLatest(expected?: SceneConflictStamp): boolean {
    const latest = this.latestForChoice(expected)
    if (!latest) return false
    if (!this.canAcknowledgeLatest()) {
      this.publish({ error: '服务端完整文档结构已变化。请比较或下载草稿后，选择服务端版本继续编辑。' })
      return false
    }
    const current = this.snapshot
    const dirtyBody = sceneDocumentDirty(current)
    const base = current.scene!
    const draft = { title: current.draft.title === base.title ? latest.scene.title : current.draft.title,
      summary: current.draft.summary === base.summary ? latest.scene.summary : current.draft.summary,
      content: dirtyBody ? current.draft.content : latest.scene.content }
    let documentDraft = current.documentDraft
    if (!dirtyBody) {
      this.evidence = emptyEvidence()
      documentDraft = latest.document?.document ?? null
    } else if (this.attempt?.payload && documentDraft) {
      this.evidence = appendSceneEvidence(this.attempt.evidence, this.evidence, documentDraft,
        this.evidence.restoredMarkerKeys, this.evidence.restoredSectionKeys)
    }
    for (const marker of latest.document?.document.markers ?? []) this.retiredMarkerKeys.delete(marker.key)
    this.attempt = null
    this.publishDraft({ ...latest, documentDraft, draft, page: this.pageWith(latest.scene),
      conflict: null, conflictDocument: null, externalChange: false, unknownSave: false, unknownRetryable: false, error: null })
    return true
  }

  /** 调用方先保留原稿；当前明确选择的服务端版本才可替换编辑区。 */
  adoptLatest(expected?: SceneConflictStamp): boolean {
    const latest = this.latestForChoice(expected)
    if (!latest) return false
    this.attempt = null
    this.clearEvidence()
    this.publishDraft({ ...latest, draft: draftOf(latest.scene), documentDraft: latest.document?.document ?? null,
      page: this.pageWith(latest.scene), conflict: null, conflictDocument: null, externalChange: false,
      unknownSave: false, unknownRetryable: false, error: null })
    return true
  }

  /** 本实例已确认的标题写入只推进此场景基线，不动正文和后续编辑证据。 */
  acceptTitleRevision(expectedRevision: number, nextRevision: number, title: string): boolean {
    const { scene, document } = this.snapshot
    if (!scene || !this.active || !this.allowedRead() || !this.allowedEdit() || this.activeSave !== null
      || this.attempt || this.snapshot.conflict || this.snapshot.externalChange
      || scene.content_revision !== expectedRevision || nextRevision !== expectedRevision + 1) return false
    const current = { ...scene, content_revision: nextRevision, chapter_title: title }
    this.publishDraft({ scene: current, document: document && { ...document, content_revision: nextRevision }, page: this.pageWith(current) })
    return true
  }
}
