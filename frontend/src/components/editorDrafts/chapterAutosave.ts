import { chapterApi } from '../../api/chapters'
import { ApiError, errorMessage } from '../../api/client'
import type { Chapter } from '../../api/types'
import { isProjectScopeCurrent, type ProjectScope } from '../../stores/project'
import { editorDraftStorage, newEditorDraftId } from '../../utils/editorDraftStorage'
import { writeResultUnknown } from '../../utils/writeResult'
import { sceneDocumentDirty, sceneDraftDirty, sceneMetadataDirty } from '../scenes/sceneDraftController'
import type { SceneConflictStamp, SceneDraftController } from '../scenes/sceneDraftController'
import { ChapterAutosaveTiming } from './chapterAutosaveTiming'
import { ChapterDraftJournal } from './chapterDraftJournal'
import { chapterTitleDirty, ChapterTitleDraft } from './chapterTitleDraft'
import { isTitleDraft, sameEditorDraftScope } from './editorDraftRecord'
import type { EditorDraftRecord, EditorDraftScope } from './editorDraftRecord'
import { invalidateMismatchedEditorActor, verifyDraftScope } from './verifyDraftScope'

export type RunSavedAction = <T>(action: () => Promise<T>) => Promise<T | undefined>
type Mode = 'all' | 'document'
type Resolve = 'keep-local' | 'use-remote'

const titleBase = (chapter: Chapter) => ({ title: chapter.title, revision: chapter.content_revision })
const onlineNow = () => typeof navigator === 'undefined' || navigator.onLine !== false

/** 一个编辑实例共享一条保存链，并在替换正文、启动任务时持有动作屏障。 */
export class ChapterAutosave {
  readonly title: ChapterTitleDraft
  readonly journal: ChapterDraftJournal
  private alive = false
  private epoch = 0
  private ready = false
  private online = onlineNow()
  private blocked = true
  private checking: Promise<boolean> | null = null
  private inFlight: Promise<boolean> | null = null
  private action = false
  private titleComposing = false
  private planningRefresh = false
  private replacing = false
  private problem: string | null = null
  private listeners = new Set<() => void>()
  private stamp = 0
  private lastNetworkStatus: Chapter['status']
  private chapterRevision: number
  private onChapter: (chapter: Chapter) => void = () => undefined
  private onSaved: () => void = () => undefined
  private readonly timing = new ChapterAutosaveTiming(() => { void this.save('document') })

  constructor(
    private readonly initial: Chapter,
    readonly scene: SceneDraftController,
    private readonly access: ProjectScope | null,
    scope: EditorDraftScope | null,
  ) {
    this.title = new ChapterTitleDraft(titleBase(initial))
    this.journal = new ChapterDraftJournal(scope, this.canEdit)
    this.lastNetworkStatus = initial.status
    this.chapterRevision = initial.content_revision
  }

  getSnapshot = () => this.stamp
  configureCallbacks(onChapter: (chapter: Chapter) => void, onSaved: () => void) {
    this.onChapter = onChapter
    this.onSaved = onSaved
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  private canRead = () => this.alive && isProjectScopeCurrent(this.access, 'canRead')
  private canEdit = () => this.alive && isProjectScopeCurrent(this.access, 'canEdit')

  private current(edit = false) {
    const epoch = this.epoch
    return () => epoch === this.epoch && (edit ? this.canEdit() : this.canRead())
  }

  get composing() { return this.titleComposing || this.scene.getSnapshot().composing }
  get saving() { return this.inFlight !== null }
  get actionLocked() { return this.action }
  get disconnected() { return !this.online }
  get error() { return this.problem }
  get verifying() { return this.checking !== null }
  get readyToSave() { return !!this.journal.scope && this.ready && this.canEdit() }
  get restored() { return this.journal.getSnapshot().hold }
  get manualDirty() { return sceneMetadataDirty(this.scene.getSnapshot()) }
  get dirty() { return sceneDraftDirty(this.scene.getSnapshot()) || chapterTitleDirty(this.title.getSnapshot()) || this.composing }
  get unknown() { return this.scene.getSnapshot().unknownSave || this.title.getSnapshot().unknown }
  get reviewNeeded() {
    const state = this.scene.getSnapshot()
    const title = this.title.getSnapshot()
    return this.unknown || state.externalChange || !!state.conflict || !!title.conflict || title.targetLost
  }
  get retryableUnknown() {
    return this.scene.getSnapshot().unknownRetryable || this.title.getSnapshot().retryable
  }

  activate() {
    this.alive = true
    this.epoch += 1
    this.online = onlineNow()
    this.scene.setInputBarrier(() => this.action || this.blocked)
    let previousTitle = this.title.getSnapshot()
    let previousScene = this.scene.getSnapshot()
    const unwatchTitle = this.title.subscribe(() => {
      const next = this.title.getSnapshot()
      const edited = next.editSeq !== previousTitle.editSeq && !this.replacing
      previousTitle = next
      if (edited && !next.conflict && !next.unknown && !next.targetLost) {
        this.journal.releaseHold('chapter_title')
        this.onEdit()
      }
      this.syncTitle()
      this.changed()
    })
    const unwatchScene = this.scene.subscribe(() => {
      const next = this.scene.getSnapshot()
      const previous = previousScene
      previousScene = next
      if (next.lastOwnSave && next.lastOwnSave !== previous.lastOwnSave) {
        this.title.advanceOwnSceneRevision(next.lastOwnSave.from, next.lastOwnSave.to)
      }
      if (next.bodyEditSeq !== previous.bodyEditSeq && !this.replacing && !next.conflict && !next.externalChange && !next.unknownSave) {
        this.journal.releaseHold('scene_document')
        this.onEdit()
      }
      if (next.scene && !next.loading && !next.conflict && !next.externalChange && !this.title.getSnapshot().unknown) {
        const title = this.title.getSnapshot()
        // 场景自己的回执可更新 B。普通加载只能在无本地标题修改时推进标题。
        if (next.scene.chapter_title !== title.base.title || !chapterTitleDirty(title)) {
          this.title.observe({ title: next.scene.chapter_title, revision: next.scene.content_revision })
        }
      }
      if (next.draftVersion !== previous.draftVersion) this.syncScene()
      this.changed()
    })
    const unwatchJournal = this.journal.subscribe(() => this.changed())
    const unwatchStorage = editorDraftStorage.subscribe((change) => {
      const scope = this.journal.scope
      if (!scope || change.kind !== 'remove' || change.chapter_id !== this.initial.id
        || !sameEditorDraftScope(scope, change.scope) || !this.dirty || this.action) return
      // 其他页面删除了已确认稿时读取服务端，仍以条件写为最终保护。
      const own = this.journal.record('chapter_title')?.writer_id === change.writer_id
        || this.journal.record('scene_document')?.writer_id === change.writer_id
      if (!own) void this.review()
    })
    const flushLocal = () => { void this.journal.protectCurrent() }
    const disconnected = () => { this.online = false; this.ready = false; this.changed() }
    const reconnect = () => { this.online = onlineNow(); this.changed(); if (this.online) void this.review() }
    const visibility = () => {
      if (document.visibilityState === 'hidden') flushLocal()
      else reconnect()
    }
    window.addEventListener('offline', disconnected)
    window.addEventListener('online', reconnect)
    window.addEventListener('focus', reconnect)
    window.addEventListener('pagehide', flushLocal)
    document.addEventListener('visibilitychange', visibility)
    this.syncTitle()
    this.syncScene()
    if (this.pending('document')) this.timing.edit()
    if (this.online && this.canRead()) void this.review()
    this.changed()
    return () => {
      this.alive = false
      this.epoch += 1
      this.ready = false
      this.checking = null
      this.inFlight = null
      this.timing.clear()
      this.timing.pause(true)
      this.scene.setInputBarrier(null)
      unwatchTitle()
      unwatchScene()
      unwatchJournal()
      unwatchStorage()
      this.journal.release()
      window.removeEventListener('offline', disconnected)
      window.removeEventListener('online', reconnect)
      window.removeEventListener('focus', reconnect)
      window.removeEventListener('pagehide', flushLocal)
      document.removeEventListener('visibilitychange', visibility)
    }
  }

  private syncTitle() {
    const state = this.title.getSnapshot()
    this.journal.syncTitle(this.title.capture(), this.initial.id, state.anchor, state.editSeq,
      chapterTitleDirty(state) || !!state.attempt || !!state.conflict || state.targetLost)
  }

  private syncScene() {
    const record = this.scene.captureDraft()
    if (!record) return
    const state = this.scene.getSnapshot()
    this.journal.syncScene(record, sceneDocumentDirty(state) || sceneMetadataDirty(state) || !!record.attempt
      || !!state.conflict || state.externalChange)
  }

  private changed() {
    const pending = this.pending('document')
    if (!pending) this.timing.clear()
    this.timing.pause(!pending || !this.allowed(false) || this.inFlight !== null || !!this.problem)
    this.stamp += 1
    for (const listener of this.listeners) listener()
  }

  private onEdit() {
    this.problem = null
    this.timing.edit()
  }

  setBlocked(value: boolean) {
    if (value !== this.blocked) { this.blocked = value; this.changed() }
  }

  setTitleComposing(value: boolean) {
    if (value !== this.titleComposing) { this.titleComposing = value; this.changed() }
  }

  changeTitle = (value: string) => {
    const scene = this.scene.getSnapshot().scene
    if (scene && this.canEdit() && !this.action && !this.blocked) this.title.edit(value, scene.id)
  }

  hasLocalChanges = () => this.dirty || this.unknown

  private pending(mode: Mode) {
    const scene = this.scene.getSnapshot()
    const title = this.title.getSnapshot()
    const all = mode === 'all'
    return ((all || !this.journal.held('chapter_title')) && chapterTitleDirty(title))
      || ((all || !this.journal.held('scene_document')) && (sceneDocumentDirty(scene) || (all && sceneMetadataDirty(scene))))
  }

  private allowed(allowAction: boolean, allowReview = false) {
    const scene = this.scene.getSnapshot()
    return this.readyToSave && this.online && onlineNow() && !this.checking && !this.blocked
      && (allowAction || !this.action) && !this.composing && !scene.loading && !!scene.scene
      && this.lastNetworkStatus !== 'generating' && (allowReview || !this.reviewNeeded)
  }

  private async saveTitle(again = false): Promise<boolean | null> {
    if (this.composing) return null
    const current = this.current(true)
    const frozen = this.title.freeze(newEditorDraftId(), again)
    const scope = this.journal.scope
    if (!frozen || !scope) return false
    await this.journal.awaitWrite('chapter_title')
    if (!current() || !this.online || !onlineNow() || this.composing) {
      if (current()) this.title.failed(frozen.attempt, again)
      return null
    }
    try {
      const actor = { id: scope.actor_id, createdAt: scope.actor_created_at }
      const result = await chapterApi.saveTitle(this.initial.project_id, this.initial.id, frozen.body, actor, current)
      if (!current()) return false
      if (!this.validChapter(result) || !this.title.accept(frozen.attempt, titleBase(result))) {
        throw new Error('无法确认标题保存结果，请核对后重试。')
      }
      this.planningRefresh = true
      this.scene.acceptTitleRevision(frozen.attempt.revision, result.content_revision, result.title)
      this.consumeChapter(result)
      return true
    } catch (error) {
      invalidateMismatchedEditorActor(error, current)
      if (!current()) return false
      const status = error instanceof ApiError ? error.status : null
      const conflict = status === 409
      this.title.failed(frozen.attempt, again || writeResultUnknown(error) || status === 401 || status === 403 || status === 404)
      if (error instanceof ApiError && error.body?.code === 'editor_target_changed') this.title.loseTarget()
      this.problem = status === 404 ? '标题保存入口不可用，请刷新应用后核对。' : errorMessage(error)
      if (conflict && !this.title.getSnapshot().targetLost) void this.reloadAfterFailure(current)
      return false
    }
  }

  private async reloadAfterFailure(current: () => boolean) {
    // 等这一轮结束，避免核对读取与分阶段写入交错。
    await this.inFlight
    if (current()) await this.review()
  }

  private beforeSceneWrite = async (): Promise<void | false> => {
    await this.journal.awaitWrite('scene_document')
    if (!this.online || !onlineNow() || !this.canEdit() || this.titleComposing) return false
  }

  private save(mode: Mode, allowAction = false): Promise<boolean> {
    if (this.inFlight) return this.inFlight
    if (!this.allowed(allowAction)) return Promise.resolve(false)
    if (mode === 'all') {
      const heldTitle = this.journal.held('chapter_title')
      const heldScene = this.journal.held('scene_document')
      this.journal.releaseHold('chapter_title')
      this.journal.releaseHold('scene_document')
      if (heldTitle) this.syncTitle()
      if (heldScene) this.syncScene()
    }
    if (!this.pending(mode)) return Promise.resolve(true)
    return this.runWrite(async () => {
      let changed = false
      const scene = this.scene.getSnapshot()
      if (!this.journal.held('scene_document') && (sceneDocumentDirty(scene) || (mode === 'all' && sceneMetadataDirty(scene)))) {
        const saved = await this.scene.save(mode, this.beforeSceneWrite)
        if (!saved || !this.canEdit()) return this.composing ? null : false
        changed = true
      }
      if (!this.journal.held('chapter_title') && chapterTitleDirty(this.title.getSnapshot())) {
        const titleSaved = await this.saveTitle()
        if (titleSaved !== true) {
          if (changed) await this.refreshSavedChapter()
          return titleSaved === null && changed ? true : titleSaved
        }
        changed = true
      } else if (changed) await this.refreshSavedChapter()
      return changed
    })
  }

  private runWrite(write: () => Promise<boolean | null>): Promise<boolean> {
    const current = this.current(true)
    this.problem = null
    const work = Promise.resolve().then(write).then((saved) => {
      if (!current()) return false
      if (saved) this.onSaved()
      else if (saved === false && !this.reviewNeeded && this.online && !this.problem) this.problem = this.scene.getSnapshot().error ?? '保存未完成，请重试。'
      return saved === true
    }).catch((error: unknown) => {
      if (current()) this.problem = errorMessage(error)
      return false
    }).finally(() => {
      if (this.inFlight !== work) return
      this.inFlight = null
      if (current()) {
        if (!this.problem && this.pending('document')) this.timing.edit()
        this.changed()
      }
    })
    this.inFlight = work
    this.changed()
    return work
  }

  private validChapter(chapter: Chapter) {
    return chapter?.id === this.initial.id && chapter.project_id === this.initial.project_id
      && typeof chapter.title === 'string' && Number.isSafeInteger(chapter.content_revision) && chapter.content_revision >= 0
  }

  private consumeChapter = (chapter: Chapter) => {
    if (!this.canRead() || !this.validChapter(chapter) || chapter.content_revision < this.chapterRevision) return
    this.chapterRevision = chapter.content_revision
    this.lastNetworkStatus = chapter.status
    this.title.observe(titleBase(chapter))
    this.onChapter(chapter)
    this.changed()
  }

  private async refreshSavedChapter() {
    const current = this.current()
    try {
      const chapter = await this.readChapter(current)
      if (chapter && current()) this.consumeChapter(chapter)
    } catch {
      // 章读取是派生状态刷新，已确认的场景写入不回退。
    }
  }

  private async readChapter(current: () => boolean) {
    if (!current()) return null
    const value = await chapterApi.get(this.initial.project_id, this.initial.id, current)
    return current() && this.validChapter(value) ? value : null
  }

  review = (): Promise<boolean> => {
    if (this.checking) return this.checking
    if (!this.online || !onlineNow() || !this.canRead() || this.action) return Promise.resolve(false)
    const current = this.current()
    const activeWrite = this.inFlight
    const work = Promise.resolve().then(async () => {
      await activeWrite
      if (!current() || !(await verifyDraftScope(this.access, current))) return false
      const chapter = await this.readChapter(current)
      if (!chapter) return false
      const resolvingUnknown = this.unknown
      const titleBefore = this.title.getSnapshot()
      const uncertainTitle = titleBefore.unknown ? titleBefore.attempt : null
      if (uncertainTitle && chapter.content_revision === uncertainTitle.revision + 1 && chapter.title === uncertainTitle.title) {
        // 先暂以标题回执推进 revision，再核对完整场景，避免把仅标题变化当作正文冲突。
        // 如果另端在同一版本事务中同时更新了正文，下面的场景核对仍会保留冲突。
        this.scene.acceptTitleRevision(uncertainTitle.revision, chapter.content_revision, chapter.title)
        this.planningRefresh = true
      }
      const old = this.scene.getSnapshot().scene
      if (old) {
        const local = this.scene.getSnapshot()
        if (this.composing || sceneDraftDirty(local) || local.unknownSave || local.conflict || local.externalChange) {
          if (!(await this.scene.reviewLatest()) || !current()) return false
        } else if (!(await this.scene.load(old.id)) || !current()) return false
      } else if (!this.scene.getSnapshot().loading) {
        // 加载失败或正在初始化时由场景控制器决定起点，不能抢另一个目标。
        return false
      }
      const checkedScene = this.scene.getSnapshot()
      const seenRevision = checkedScene.conflict?.content_revision ?? checkedScene.scene?.content_revision
      if (seenRevision !== undefined && seenRevision !== chapter.content_revision) return false
      const anchor = this.title.getSnapshot().anchor
      const page = this.scene.getSnapshot().page
      if (anchor !== null && page?.content_revision === chapter.content_revision && !page.items.some((scene) => scene.id === anchor)) {
        this.title.loseTarget()
      }
      this.consumeChapter(chapter)
      this.ready = true
      if (resolvingUnknown && !this.reviewNeeded) {
        this.problem = null
        if (this.pending('document')) this.timing.edit()
      }
      return true
    }).then((verified) => {
      if (current() && this.online && !verified && this.dirty && !this.reviewNeeded) this.problem = '暂时无法核对服务器上的保存状态，请重试。'
      return verified
    }).catch((error: unknown) => {
      if (current()) this.problem = errorMessage(error)
      return false
    }).finally(() => {
      if (this.checking === work) { this.checking = null; if (current()) this.changed() }
    })
    this.ready = false
    this.checking = work
    this.changed()
    return work
  }

  /** 从受保护服务端动作返回的章信息，保持本地草稿再重读场景。 */
  receiveChapter(chapter: Chapter) {
    this.consumeChapter(chapter)
  }

  async refreshedAuthority() {
    if (this.action) {
      await this.scene.load()
      await this.refreshSavedChapter()
    } else await this.review()
  }

  saveNow = async () => {
    if (this.action) return false
    await this.inFlight
    if (this.reviewNeeded || !this.canEdit()) return false
    if (!this.readyToSave && !(await this.review())) return false
    const saved = await this.save('all')
    return saved && !this.dirty && !this.reviewNeeded
  }

  retry = async () => {
    if (this.action || this.saving || this.composing || !this.canEdit()) return false
    if (!this.retryableUnknown) {
      if (!(await this.review()) || this.reviewNeeded) return false
      return this.saveNow()
    }
    // “重试原保存”只发送核对过的 S。新输入仍由下一轮保存处理。
    if (!this.allowed(false, true)) return false
    return this.runWrite(async () => {
      if (this.scene.getSnapshot().unknownRetryable) {
        if (!(await this.scene.retryUnknown(this.beforeSceneWrite))) return false
        await this.refreshSavedChapter()
        return true
      }
      return this.saveTitle(true)
    })
  }

  private async exclusive<T>(action: () => Promise<T>): Promise<T | undefined> {
    if (!this.canEdit() || this.action || this.blocked || this.composing) return undefined
    const current = this.current(true)
    this.action = true
    this.changed()
    try {
      await this.inFlight
      await this.checking
      if (current() && !this.blocked && !this.composing) return await action()
    } finally {
      this.action = false
      if (current()) this.changed()
    }
  }

  runSavedAction: RunSavedAction = (action) => this.exclusive(async () => {
    const current = this.current(true)
    if (!(await verifyDraftScope(this.access, current)) || !current()) return undefined
    if (this.reviewNeeded || this.restored || !(await this.save('all', true)) || this.dirty || this.reviewNeeded) return undefined
    if (this.planningRefresh) {
      const scene = this.scene.getSnapshot().scene
      if (!scene || !(await this.scene.load(scene.id)) || this.scene.getSnapshot().scene?.content_revision !== scene.content_revision) return undefined
      this.planningRefresh = false
    }
    return current() && this.allowed(true) ? action() : undefined
  })

  restore = (record: EditorDraftRecord) => this.exclusive(async () => {
    const current = this.current(true)
    if (this.unknown || this.composing || !this.online || !onlineNow() || !this.matchesScene(record)) return false
    if (!(await verifyDraftScope(this.access, current))) return false
    if (this.dirty && !(await this.journal.protectCurrent())) return false
    const remote = await this.readChapter(current)
    if (!remote || !this.matchesScene(record)) return false
    try {
      const copy = await this.journal.copy(record)
      if (!copy || !current() || !this.matchesScene(copy)) return false
      this.replacing = true
      this.journal.attach(copy)
      this.problem = null
      if (isTitleDraft(copy)) {
        this.title.restore(copy.payload, copy.target.anchor_scene_id, copy.edit_seq, titleBase(remote))
        return true
      }
      const restored = await this.scene.restoreDraft(copy.payload)
      if (restored === 'invalid') this.journal.detach('scene_document')
      return restored !== 'invalid' && current()
    } catch (error) {
      if (current()) this.problem = errorMessage(error)
      return false
    } finally {
      this.replacing = false
    }
  })

  private matchesScene(record: EditorDraftRecord) {
    const scene = this.scene.getSnapshot().scene
    if (!scene || scene.chapter_id !== record.target.chapter_id || scene.id !== record.target.anchor_scene_id) return false
    return record.target.kind === 'chapter_title' || scene.created_at === record.target.scene_created_at
  }

  titleStamp() { return this.title.stamp(this.journal.record('chapter_title')?.draft_id) }

  async exportCurrent(kind: 'chapter_title' | 'scene_document') {
    const current = this.current()
    const record = this.journal.record(kind)
    if (!record || !(await verifyDraftScope(this.access, current)) || !current()) return null
    return this.journal.record(kind) === record ? record : null
  }

  resolveTitle = (choice: Resolve, stamp: string) => this.exclusive(async () => {
    const current = this.current(true)
    if (this.unknown || this.titleStamp() !== stamp || !(await verifyDraftScope(this.access, current))) return false
    const remote = await this.readChapter(current)
    if (!remote) return false
    this.consumeChapter(remote)
    if (this.titleStamp() !== stamp || this.unknown) return false
    if (choice === 'use-remote') {
      if (!(await this.journal.protectCurrent()) || this.titleStamp() !== stamp) return false
      const draftId = this.journal.record('chapter_title')?.draft_id
      // 保留原本地分支，让控制器清空编辑区时不会清掉原稿。
      this.journal.detach('chapter_title')
      return this.title.resolve(choice, stamp, draftId)
    }
    const resolved = this.title.resolve(choice, stamp, this.journal.record('chapter_title')?.draft_id)
    if (resolved) { this.journal.releaseHold('chapter_title'); this.onEdit() }
    return resolved
  })

  resolveScene = (choice: Resolve, stamp: SceneConflictStamp) => this.exclusive(async () => {
    if (this.unknown || !(await verifyDraftScope(this.access, this.current(true))) || !(await this.scene.reviewLatest())) return false
    if (this.unknown) return false
    if (choice === 'use-remote') {
      if (!(await this.journal.protectCurrent())) return false
      // 先检查快照再拆分，过时的按钮不能舍弃当前分支。
      const state = this.scene.getSnapshot()
      if (state.conflict?.id !== stamp.sceneId || state.conflict.content_revision !== stamp.revision
        || state.conflict.planning_revision !== stamp.planningRevision || state.editSeq !== stamp.editSeq) return false
      this.journal.detach('scene_document')
      return this.scene.adoptLatest(stamp)
    }
    const resolved = this.scene.acknowledgeLatest(stamp)
    if (resolved) { this.journal.releaseHold('scene_document'); this.onEdit() }
    return resolved
  })
}
