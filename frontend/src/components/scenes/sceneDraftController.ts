import { ApiError, errorMessage } from '../../api/client'
import type { RichDocument, RichDocumentOut, RichDocumentSave } from '../../api/richDocument'
import type { Scene, ScenePage, SceneUpdate } from '../../api/types'
import { validateSectionTitle } from './richSectionNode'
import { sameSectionDocument } from './richSectionProjection'
import { validSectionDocument } from './richSectionValidity'
import { richDocumentCapacityProblem, richDocumentSaveError } from './richDocumentCapacity'

export interface SceneDraft { title: string; content: string; summary: string }
export interface RichEditEvidence {
  mappedMarkerKeys: string[]; explicitMarkerIntent: boolean; restoredMarkerKeys?: string[]
  mappedSectionKeys?: string[]; explicitSectionIntent?: boolean; restoredSectionKeys?: string[]
}
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
}
export interface SceneDraftApi {
  list: (signal: AbortSignal) => Promise<ScenePage>
  get: (sid: number, signal?: AbortSignal) => Promise<Scene>
  update: (sid: number, body: SceneUpdate) => Promise<Scene>
  getDocument?: (sid: number, signal?: AbortSignal) => Promise<RichDocumentOut>
  saveDocument?: (sid: number, body: RichDocumentSave) => Promise<RichDocumentOut>
}
const empty = (): SceneDraft => ({ title: '', content: '', summary: '' })
const draftOf = (scene: Scene): SceneDraft => ({ title: scene.title, content: scene.content, summary: scene.summary })
export const sceneDraftDirty = (snapshot: SceneDraftSnapshot) => snapshot.scene !== null &&
  (snapshot.composing || snapshot.draft.title !== snapshot.scene.title || snapshot.draft.content !== snapshot.scene.content || snapshot.draft.summary !== snapshot.scene.summary
    || !!snapshot.document && !!snapshot.documentDraft && !sameDocument(snapshot.document.document, snapshot.documentDraft))

const sameDocument = (a: RichDocument, b: RichDocument) => a === b || sameSectionDocument(a, b)
const metadata = (value: RichDocument) => ({ ...value, content: '' })

function documentSave(source: RichDocumentOut, draft: RichDocument, mappedMarkerKeys: Set<string>, explicitMarkerIntent: boolean,
  retiredMarkerKeys: Set<string>, historyRestoredKeys: Set<string>, mappedSectionKeys: Set<string>, explicitSectionIntent: boolean,
  historyRestoredSectionKeys: Set<string>): RichDocumentSave {
  const capacityProblem = richDocumentCapacityProblem(draft)
  if (capacityProblem) throw new Error(capacityProblem)
  if (!validSectionDocument(draft)) throw new Error('文档位置或格式无效，草稿未提交。')
  for (const item of draft.sections) {
    const problem = validateSectionTitle(item.title)
    if (problem) throw new Error(`区块名称无效：${problem}`)
  }
  const originalSections = new Set(source.document.sections.map((item) => item.key))
  const nextSections = new Set(draft.sections.map((item) => item.key))
  const retiredSections = new Set(source.document.retired_section_keys)
  const restoredSections = [...nextSections].filter((key) => !originalSections.has(key) && retiredSections.has(key))
  if (restoredSections.some((key) => !historyRestoredSectionKeys.has(key))) throw new Error('缺少区块的历史重做事务，草稿未提交。')
  const addedSections = [...nextSections].filter((key) => !originalSections.has(key) && !retiredSections.has(key))
  const deletedSections = [...originalSections].filter((key) => !nextSections.has(key))
  if ((addedSections.length || restoredSections.length || deletedSections.length) && !explicitSectionIntent) {
    throw new Error('缺少明确的区块增删事务，草稿未提交。')
  }
  const original = new Set(source.document.markers.map((item) => item.key))
  const next = new Set(draft.markers.map((item) => item.key))
  const restored = [...next].filter((key) => !original.has(key) && retiredMarkerKeys.has(key))
  if (restored.some((key) => !historyRestoredKeys.has(key))) throw new Error('缺少标记的历史重做事务，草稿未提交。')
  const added = [...next].filter((key) => !original.has(key) && !retiredMarkerKeys.has(key))
  const deleted = [...original].filter((key) => !next.has(key))
  if ((added.length || restored.length || deleted.length) && !explicitMarkerIntent) throw new Error('缺少明确的标记增删事务，草稿未提交。')
  return { expected_content_revision: source.content_revision, document: draft,
    add_section_keys: addedSections, delete_section_keys: deletedSections, restore_section_keys: restoredSections,
    add_marker_keys: added, delete_marker_keys: deleted, restore_marker_keys: restored,
    mapped_section_keys: [...originalSections].filter((key) => nextSections.has(key) && mappedSectionKeys.has(key)),
    mapped_marker_keys: [...original].filter((key) => next.has(key) && mappedMarkerKeys.has(key)) }
}

/** 一章一授权身份的草稿控制器；保存失败不切场景，冲突只展示，核对前不替换 revision。 */
export class SceneDraftController {
  private active = false
  private generation = 0
  private request: AbortController | null = null
  private pendingRefresh = false
  private listeners = new Set<() => void>()
  private mappedMarkerKeys = new Set<string>()
  private mappedSectionKeys = new Set<string>()
  private retiredMarkerKeys = new Set<string>()
  private historyRestoredKeys = new Set<string>()
  private historyRestoredSectionKeys = new Set<string>()
  private explicitMarkerIntent = false
  private explicitSectionIntent = false
  private snapshot: SceneDraftSnapshot = { scene: null, draft: empty(), page: null, loading: true, saving: false, composing: false, error: null, conflict: null, externalChange: false, document: null, documentDraft: null, conflictDocument: null }
  constructor(private readonly chapterId: number, private readonly api: SceneDraftApi, private readonly canRead: () => boolean, private readonly canEdit: () => boolean) {}
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.active = true }
  deactivate() { this.active = false; this.generation += 1; this.request?.abort(); this.request = null; this.pendingRefresh = false; this.snapshot = { ...this.snapshot, composing: false } }
  private publish(patch: Partial<SceneDraftSnapshot>) { this.snapshot = { ...this.snapshot, ...patch }; this.listeners.forEach((fn) => fn()) }
  private valid(generation: number) { return this.active && generation === this.generation && this.canRead() }
  setComposing(composing: boolean) {
    if (!this.active || this.snapshot.composing === composing) return
    this.publish({ composing })
    if (!composing && this.pendingRefresh && !this.snapshot.saving) { this.pendingRefresh = false; void this.load() }
  }
  change(patch: Partial<SceneDraft>) {
    if (!this.active || !this.canEdit() || this.snapshot.saving) return
    // 已加载完整文档后，字符串入口不能覆盖结构草稿。
    if (this.snapshot.document && patch.content !== undefined && patch.content !== this.snapshot.draft.content) return
    this.publish({ draft: { ...this.snapshot.draft, ...patch } })
  }
  changeDocument(document: RichDocument, evidence: RichEditEvidence = { mappedMarkerKeys: [], explicitMarkerIntent: false }) {
    if (!this.active || !this.canEdit() || this.snapshot.saving || !this.snapshot.document || !this.snapshot.documentDraft) return
    const source = this.snapshot.documentDraft
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
    for (const key of before.keys()) if (!after.has(key)) this.historyRestoredKeys.delete(key)
    for (const key of evidence.restoredMarkerKeys ?? []) if (!before.has(key) && after.has(key)) this.historyRestoredKeys.add(key)
    for (const key of mapped) this.mappedMarkerKeys.add(key)
    for (const key of removedSections) this.historyRestoredSectionKeys.delete(key)
    for (const key of evidence.restoredSectionKeys ?? []) if (!oldSections.has(key) && newSections.has(key)) this.historyRestoredSectionKeys.add(key)
    for (const key of mappedSections) this.mappedSectionKeys.add(key)
    if (evidence.explicitMarkerIntent) this.explicitMarkerIntent = true
    if (evidence.explicitSectionIntent) this.explicitSectionIntent = true
    this.publish({ documentDraft: document, draft: { ...this.snapshot.draft, content: document.content }, error: null })
  }
  async load(preferredId?: number, preserveDraft = true): Promise<boolean> {
    if (!this.active || !this.canRead()) return false
    if (this.snapshot.saving || this.snapshot.composing) { this.pendingRefresh = true; return false }
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
      const document = this.api.getDocument ? await this.api.getDocument(sid, request.signal) : null
      if (!this.valid(generation)) return false
      if (document && (document.content_revision !== scene.content_revision || document.planning_revision !== scene.planning_revision || document.document.content !== scene.content)) throw new Error('完整文档与场景版本不一致，请重新加载后核对。')
      if (preserveDraft && previousId === sid && (sceneDraftDirty(this.snapshot) || this.snapshot.conflict !== null)) {
        const sameContentRevision = scene.content_revision === this.snapshot.scene?.content_revision
        this.publish({ page, loading: false, ...(sameContentRevision ? { scene, document } : {}), ...(this.snapshot.conflict ? { conflict: scene, conflictDocument: document } : {}), externalChange: !sameContentRevision })
      } else {
        this.mappedMarkerKeys.clear(); this.retiredMarkerKeys.clear(); this.historyRestoredKeys.clear(); this.explicitMarkerIntent = false
        this.mappedSectionKeys.clear(); this.historyRestoredSectionKeys.clear(); this.explicitSectionIntent = false
        this.publish({ page, scene, document, documentDraft: document?.document ?? null, draft: draftOf(scene), loading: false, conflict: null, conflictDocument: null, externalChange: false })
      }
      return true
    } catch (error) {
      if (this.valid(generation)) this.publish({ loading: false, error: errorMessage(error) })
      return false
    } finally { if (this.request === request) this.request = null }
  }
  async save(): Promise<Scene | null> {
    const { scene, draft, saving, conflict, externalChange, composing } = this.snapshot
    if (composing) { this.publish({ error: '输入法组合尚未结束，请确认文字后再保存。' }); return null }
    if (!scene || saving || conflict || externalChange || !this.active || !this.canEdit()) return null
    if (!sceneDraftDirty(this.snapshot)) return scene
    const generation = this.generation
    this.publish({ saving: true, error: null })
    try {
      if (this.snapshot.document && this.snapshot.documentDraft && this.api.saveDocument) {
        return await this.saveRich(generation, scene, draft, this.snapshot.document, this.snapshot.documentDraft)
      }
      const saved = await this.api.update(scene.id, { ...draft, expected_content_revision: scene.content_revision })
      if (!this.valid(generation) || !this.canEdit() || this.snapshot.scene?.id !== scene.id) return null
      this.publish({ scene: saved, draft: draftOf(saved), externalChange: false, page: this.snapshot.page ? {
        ...this.snapshot.page, content_revision: saved.content_revision, planning_revision: saved.planning_revision,
        items: this.snapshot.page.items.map((item) => item.id === saved.id ? saved : { ...item, content_revision: saved.content_revision, planning_revision: saved.planning_revision }),
      } : null })
      return saved
    } catch (error) {
      if (!this.valid(generation) || !this.canEdit()) return null
      this.publish({ error: richDocumentSaveError(errorMessage(error)) })
      if (error instanceof ApiError && error.status === 409) await this.reviewLatest()
      return null
    } finally {
      if (this.valid(generation)) {
        this.publish({ saving: false })
        if (this.pendingRefresh) { this.pendingRefresh = false; void this.load() }
      }
    }
  }
  private async saveRich(generation: number, original: Scene, draft: SceneDraft, base: RichDocumentOut, document: RichDocument): Promise<Scene | null> {
    if (!this.api.saveDocument) return null
    let scene = original; let current = base
    if (!sameDocument(base.document, document)) {
      const payload = documentSave(base, document, this.mappedMarkerKeys, this.explicitMarkerIntent,
        this.retiredMarkerKeys, this.historyRestoredKeys, this.mappedSectionKeys, this.explicitSectionIntent,
        this.historyRestoredSectionKeys)
      current = await this.api.saveDocument(scene.id, payload)
      if (!this.valid(generation) || !this.canEdit() || this.snapshot.scene?.id !== scene.id) return null
      if (current.content_revision !== base.content_revision + 1 || !sameDocument(current.document, document)) {
        this.publish({ externalChange: true, error: '完整文档保存响应与本地草稿不一致，请核对，草稿仍保留。' })
        return null
      }
      for (const key of payload.delete_marker_keys) this.retiredMarkerKeys.add(key)
      for (const key of payload.restore_marker_keys ?? []) this.retiredMarkerKeys.delete(key)
      this.historyRestoredKeys.clear()
      this.historyRestoredSectionKeys.clear()
      // 文档已提交；先更新已保存基线，再读场景，避免后续网络错误造成旧版本重发。
      scene = { ...scene, content: current.document.content, content_revision: current.content_revision, planning_revision: current.planning_revision }
      this.publish({ scene, document: current, documentDraft: current.document, draft: { ...draft, content: current.document.content } })
      this.mappedMarkerKeys.clear(); this.explicitMarkerIntent = false
      this.mappedSectionKeys.clear(); this.explicitSectionIntent = false
      const latest = await this.api.get(scene.id)
      if (!this.valid(generation) || !this.canEdit()) return null
      if (latest.content_revision !== current.content_revision || latest.content !== current.document.content) {
        this.publish({ externalChange: true, error: '文档已保存，但场景随后又变化，请重新核对。' })
        return null
      }
      scene = latest
    }
    if (draft.title !== scene.title || draft.summary !== scene.summary) {
      const saved = await this.api.update(scene.id, { title: draft.title, summary: draft.summary, expected_content_revision: scene.content_revision })
      if (!this.valid(generation) || !this.canEdit()) return null
      if (saved.id !== scene.id || saved.chapter_id !== this.chapterId || saved.content !== current.document.content
        || saved.content_revision !== scene.content_revision + 1) {
        this.publish({ externalChange: true, error: '场景资料响应与已保存正文不一致，请核对，草稿仍保留。' })
        return null
      }
      scene = saved
      current = { ...current, content_revision: saved.content_revision, planning_revision: saved.planning_revision }
    }
    this.publish({ scene, document: current, documentDraft: current.document, draft: draftOf(scene), externalChange: false, conflict: null, conflictDocument: null, page: this.snapshot.page ? {
      ...this.snapshot.page, content_revision: scene.content_revision, planning_revision: scene.planning_revision,
      items: this.snapshot.page.items.map((item) => item.id === scene.id ? scene : { ...item, content_revision: scene.content_revision, planning_revision: scene.planning_revision }),
    } : null })
    return scene
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
      const document = this.api.getDocument ? await this.api.getDocument(sid) : null
      if (document && (document.content_revision !== latest.content_revision || document.planning_revision !== latest.planning_revision || document.document.content !== latest.content)) throw new Error('最新文档读取期间已变化，请重新核对。')
      if (this.valid(generation) && latest.chapter_id === this.chapterId && this.snapshot.scene?.id === sid) this.publish({ conflict: latest, conflictDocument: document })
    } catch (error) { if (this.valid(generation)) this.publish({ error: errorMessage(error), externalChange: true }) }
  }
  /** 作者看过对比后显式换用最新版本；本地输入不变，下一次保存仍由作者触发。 */
  acknowledgeLatest() {
    if (!this.snapshot.conflict || !this.active || !this.canEdit()) return
    const { document, documentDraft, conflictDocument } = this.snapshot
    if (document && documentDraft && conflictDocument) {
      const locallyChanged = !sameDocument(document.document, documentDraft)
      if (locallyChanged && !sameDocument(metadata(document.document), metadata(conflictDocument.document))) {
        this.publish({ error: '服务端完整文档结构已变化，请先复制本地草稿并重新加载，不能用旧结构覆盖。' })
        return
      }
      const nextDocument = locallyChanged ? documentDraft : conflictDocument.document
      this.publish({ scene: this.snapshot.conflict, document: conflictDocument, documentDraft: nextDocument,
        draft: { ...this.snapshot.draft, content: nextDocument.content }, conflict: null, conflictDocument: null, externalChange: false, error: null })
      return
    }
    this.publish({ scene: this.snapshot.conflict, conflict: null, externalChange: false, error: null })
  }
}
