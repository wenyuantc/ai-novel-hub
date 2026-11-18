import type { SceneDraftRecord } from '../scenes/sceneDraftRecord'
import { editorDraftStorage, EditorDraftWriteQueue, newEditorDraftId } from '../../utils/editorDraftStorage'
import { draftRecordRef, sameEditorDraftTarget } from './editorDraftRecord'
import type { EditorDraftRecord, EditorDraftScope, EditorDraftTarget, TitleDraftPayload } from './editorDraftRecord'

type Kind = 'chapter_title' | 'scene_document'
type Fields = Pick<EditorDraftRecord, 'edit_seq'> & (
  Pick<Extract<EditorDraftRecord, { payload: TitleDraftPayload }>, 'target' | 'payload'> |
  { target: Extract<EditorDraftTarget, { kind: 'scene_document' }>; payload: SceneDraftRecord }
)
type LocalStatus = 'saving' | 'saved' | 'error' | undefined
interface JournalSnapshot { local: LocalStatus; ids: readonly string[]; hold: boolean }
const EMPTY: JournalSnapshot = { local: undefined, ids: [], hold: false }

interface Slot {
  record: EditorDraftRecord
  queue: EditorDraftWriteQueue
  lastWrite: Promise<boolean>
  local: LocalStatus
  retired: boolean
}

/** 只负责保留由编辑控制器发布的记录，不参与编辑器基线的选择。 */
export class ChapterDraftJournal {
  private writer: string | null = null
  private slots = new Map<Kind, Slot>()
  private listeners = new Set<() => void>()
  private snapshot = EMPTY

  constructor(readonly scope: EditorDraftScope | null, private readonly current: () => boolean) {}

  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  private get writerId() { return this.writer ??= newEditorDraftId() }

  record(kind: Kind) { return this.slots.get(kind)?.record }

  held(kind: Kind) { return this.record(kind)?.recovery_hold ?? false }

  syncTitle(payload: TitleDraftPayload, chapterId: number, anchor: number | null, editSeq: number, dirty: boolean) {
    const activeAnchor = anchor ?? this.record('chapter_title')?.target.anchor_scene_id
    if (!activeAnchor) return
    this.sync({ target: { kind: 'chapter_title', chapter_id: chapterId, anchor_scene_id: activeAnchor }, payload, edit_seq: editSeq }, dirty)
  }

  syncScene(payload: SceneDraftRecord, dirty: boolean) {
    const scene = payload.base
    this.sync({ target: { kind: 'scene_document', chapter_id: scene.chapter_id, anchor_scene_id: scene.id,
      scene_id: scene.id, scene_created_at: scene.created_at }, payload, edit_seq: payload.editSeq }, dirty)
  }

  private sync(fields: Fields, dirty: boolean) {
    if (!this.scope || !this.current()) return
    const kind = fields.target.kind
    let slot = this.slots.get(kind)
    if (slot && (!sameEditorDraftTarget(slot.record.target, fields.target) || (slot.retired && fields.edit_seq > slot.record.edit_seq))) {
      this.detach(kind)
      slot = undefined
    }
    if (!slot && !dirty) return
    if (slot) {
      slot.record = { ...slot.record, ...fields, record_seq: slot.record.record_seq + 1, saved_at: Date.now() }
    } else {
      const record = {
        ...fields, schema_version: 1, scope: this.scope, draft_id: newEditorDraftId(), writer_id: this.writerId,
        record_seq: 1, recovery_hold: false, recovered_from: null, saved_at: Date.now(),
      } satisfies EditorDraftRecord
      slot = this.add(record, false)
    }
    this.write(slot, !dirty && !slot.record.recovery_hold)
  }

  private add(record: EditorDraftRecord, stored: boolean) {
    const slot: Slot = { record, queue: new EditorDraftWriteQueue(editorDraftStorage, this.current, stored),
      lastWrite: Promise.resolve(stored), local: stored ? 'saved' : 'saving', retired: false }
    this.slots.set(record.target.kind, slot)
    return slot
  }

  private write(slot: Slot, clean: boolean) {
    const record = slot.record
    const kind = record.target.kind
    const unchanged = () => this.current() && this.slots.get(kind) === slot && slot.record === record
    slot.local = 'saving'
    this.publish()
    slot.lastWrite = slot.queue.enqueue(record).then((saved) => {
      if (!saved && this.current() && this.slots.get(kind) === slot) {
        const latest = slot.record
        slot.retired = true
        if (latest.edit_seq > record.edit_seq) {
          // 另一页面已舍弃原版本，只有这之后的真实新输入可以创建新分支。
          this.detach(kind)
          const next = this.add({ ...latest, draft_id: newEditorDraftId(), record_seq: 1 }, false)
          this.write(next, false)
        }
      }
      if (unchanged()) {
        slot.local = saved ? 'saved' : 'error'
        if (saved && clean) {
          this.detach(kind)
          // 保存者只清理自己已核对的这一次快照。
          void editorDraftStorage.remove(record.scope, draftRecordRef(record), this.current).catch(() => undefined)
        } else this.publish()
      }
      return saved
    }, () => {
      if (unchanged()) { slot.local = 'error'; this.publish() }
      return false
    })
  }

  releaseHold(kind: Kind) {
    const slot = this.slots.get(kind)
    if (!slot || !slot.record.recovery_hold || !this.current()) return
    slot.record = { ...slot.record, recovery_hold: false, record_seq: slot.record.record_seq + 1, saved_at: Date.now() }
    this.write(slot, false)
  }

  async copy(record: EditorDraftRecord): Promise<EditorDraftRecord | null> {
    if (!this.scope || !this.current()) return null
    return editorDraftStorage.copyForRecovery(this.scope, draftRecordRef(record), this.writerId, this.current)
  }

  attach(record: EditorDraftRecord) {
    this.detach(record.target.kind)
    this.add(record, true)
    this.publish()
  }

  detach(kind: Kind) {
    const existing = this.slots.get(kind)
    if (!existing) return
    existing.queue.close()
    this.slots.delete(kind)
    this.publish()
  }

  release() {
    this.detach('chapter_title')
    this.detach('scene_document')
  }

  /* 等待进入保存步骤时已排队的快照。之后的新输入继续排队。 */
  awaitWrite(kind: Kind) { return this.slots.get(kind)?.lastWrite ?? Promise.resolve(false) }

  async protectCurrent(): Promise<boolean> {
    const before = [...this.slots.values()].map((slot) => ({ slot, record: slot.record, pending: slot.lastWrite }))
    if (!(await Promise.all(before.map((item) => item.pending))).every(Boolean) || !this.current()) return false
    return [...this.slots.values()].every((slot) => before.some((item) => item.slot === slot && item.record === slot.record) && slot.local === 'saved')
  }

  private publish() {
    const items = [...this.slots.values()]
    const local = items.some((item) => item.local === 'error') ? 'error'
      : items.some((item) => item.local === 'saving') ? 'saving' : items.length ? 'saved' : undefined
    const ids = items.map(({ record }) => record.draft_id)
    // 也校验是否包含状态不同但 ID 相同的替换记录。
    const hold = items.some(({ record }) => record.recovery_hold)
    if (this.snapshot.local === local && this.snapshot.hold === hold
      && ids.length === this.snapshot.ids.length && ids.every((id, index) => this.snapshot.ids[index] === id)) return
    this.snapshot = { local, ids, hold }
    for (const listener of this.listeners) listener()
  }
}
