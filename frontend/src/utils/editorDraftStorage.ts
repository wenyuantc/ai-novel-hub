import {
  decodeDraftEnvelope,
  decodeDraftScope,
  decodeEditorDraftEntry,
  draftRecordRef,
  sameEditorDraftScope,
  sameEditorDraftTarget,
} from '../components/editorDrafts/editorDraftRecord'
import type {
  DraftRecordRef,
  EditorDraftEntry,
  EditorDraftRecord,
  EditorDraftScope,
} from '../components/editorDrafts/editorDraftRecord'

export type {
  DraftRecordRef,
  EditorDraftEntry,
  EditorDraftRecord,
  EditorDraftScope,
  EditorDraftTarget,
  SceneEditorDraftRecord,
  TitleDraftRecord,
} from '../components/editorDrafts/editorDraftRecord'

const DATABASE = 'ainovel.editor-drafts'
const STORE = 'drafts'
const SCOPE_INDEX = 'by_scope'
const CHAPTER_INDEX = 'by_chapter'
const SCOPE_KEYS = ['scope.actor_id', 'scope.actor_created_at', 'scope.project_id', 'scope.project_created_at']

type IsCurrent = () => boolean

export interface EditorDraftChange {
  kind: 'put' | 'remove'
  scope: EditorDraftScope
  chapter_id: number
  draft_id: string
  writer_id: string
  record_seq: number
}

export interface EditorDraftStorage {
  list: (scope: EditorDraftScope, isCurrent: IsCurrent, chapterId?: number) => Promise<EditorDraftEntry[]>
  /** existing=true 的写入以分支仍存在为条件，防止迟到操作重新建立已舍弃草稿。 */
  put: (record: EditorDraftRecord, isCurrent: IsCurrent, existing?: boolean) => Promise<boolean>
  remove: (scope: EditorDraftScope, ref: DraftRecordRef, isCurrent: IsCurrent) => Promise<boolean>
  readRaw: (scope: EditorDraftScope, ref: DraftRecordRef, isCurrent: IsCurrent) => Promise<unknown | null>
  copyForRecovery: (
    scope: EditorDraftScope, ref: DraftRecordRef, writerId: string, isCurrent: IsCurrent,
  ) => Promise<EditorDraftRecord | null>
  subscribe: (listener: (change: EditorDraftChange) => void) => () => void
  subscribeError: (listener: (error: Error) => void) => () => void
}

interface StorageOptions {
  factory?: IDBFactory | null
  now?: () => number
  id?: () => string
  broadcast?: boolean
}

export function newEditorDraftId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  if (!globalThis.crypto?.getRandomValues) throw new Error('当前浏览器无法生成本地草稿身份')
  // 普通 HTTP 下 randomUUID 可能不可用，使用相同的安全随机源生成 UUID v4。
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function requireCurrent(isCurrent: IsCurrent) {
  if (!isCurrent()) throw new DOMException('草稿访问已失效', 'AbortError')
}

function transactionFor(db: IDBDatabase, mode: IDBTransactionMode): IDBTransaction {
  if (mode !== 'readwrite') return db.transaction(STORE, mode)
  try {
    return db.transaction(STORE, mode, { durability: 'strict' })
  } catch (error) {
    if (!(error instanceof TypeError) && !(error instanceof DOMException && error.name === 'NotSupportedError')) throw error
    return db.transaction(STORE, mode)
  }
}

type WatchRequest = <Value>(request: IDBRequest<Value>, success: (value: Value) => void) => void

/** 在请求回调内发出下一次请求，等整笔事务完成后才兑现结果。 */
function transact<Result>(
  db: IDBDatabase,
  mode: IDBTransactionMode,
  isCurrent: IsCurrent,
  run: (store: IDBObjectStore, watch: WatchRequest, finish: (value: Result) => void) => void,
): Promise<Result> {
  return new Promise((resolve, reject) => {
    requireCurrent(isCurrent)
    const tx = transactionFor(db, mode)
    let outcome: { value: Result } | undefined
    let failure: unknown
    const abort = (reason: unknown) => {
      failure = reason
      try {
        tx.abort()
      } catch {
        reject(reason)
      }
    }
    tx.onabort = () => reject(failure ?? tx.error ?? new DOMException('本地草稿事务已中止', 'AbortError'))
    tx.oncomplete = () => {
      try {
        requireCurrent(isCurrent)
        if (failure) throw failure
        if (!outcome) throw new Error('本地草稿事务未返回结果')
        resolve(outcome.value)
      } catch (error) {
        reject(error)
      }
    }
    const watch: WatchRequest = (request, success) => {
      request.onsuccess = () => {
        try {
          requireCurrent(isCurrent)
          success(request.result)
        } catch (error) {
          abort(error)
        }
      }
      request.onerror = () => { failure = request.error }
    }
    try {
      run(tx.objectStore(STORE), watch, (value) => { outcome = { value } })
    } catch (error) {
      abort(error)
    }
  })
}

function sameRef(left: DraftRecordRef, right: DraftRecordRef): boolean {
  return left.draft_id === right.draft_id && left.writer_id === right.writer_id && left.record_seq === right.record_seq
}

function isMatchingRecord(raw: unknown, scope: EditorDraftScope, ref: DraftRecordRef): boolean {
  const envelope = decodeDraftEnvelope(raw)
  return Boolean(envelope && sameEditorDraftScope(envelope.scope, scope) && sameRef(envelope.ref, ref))
}

function scopeKey(scope: EditorDraftScope): IDBValidKey[] {
  return [scope.actor_id, scope.actor_created_at, scope.project_id, scope.project_created_at]
}

function readChange(value: unknown): EditorDraftChange | null {
  if (typeof value !== 'object' || value === null || !('scope' in value)) return null
  const scope = decodeDraftScope(value.scope)
  if (!scope || !('kind' in value) || value.kind !== 'put' && value.kind !== 'remove'
      || !('draft_id' in value) || typeof value.draft_id !== 'string' || !value.draft_id || value.draft_id.length > 128
      || !('writer_id' in value) || typeof value.writer_id !== 'string' || !value.writer_id || value.writer_id.length > 128
      || !('chapter_id' in value) || typeof value.chapter_id !== 'number' || !Number.isSafeInteger(value.chapter_id) || value.chapter_id < 1
      || !('record_seq' in value) || typeof value.record_seq !== 'number' || !Number.isSafeInteger(value.record_seq)
      || value.record_seq < 1) return null
  return {
    kind: value.kind, scope, draft_id: value.draft_id, writer_id: value.writer_id,
    chapter_id: value.chapter_id, record_seq: value.record_seq,
  }
}

function notify<Value>(listeners: Set<(value: Value) => void>, value: Value) {
  for (const listener of listeners) {
    try {
      listener(value)
    } catch {
      // 草稿已经提交，通知错误不改变事务结果。
    }
  }
}

function sortEntries(left: EditorDraftEntry, right: EditorDraftEntry): number {
  const leftTime = left.status === 'ready' ? left.record.saved_at : left.saved_at ?? 0
  const rightTime = right.status === 'ready' ? right.record.saved_at : right.saved_at ?? 0
  const leftId = left.status === 'ready' ? left.record.draft_id : left.ref.draft_id
  const rightId = right.status === 'ready' ? right.record.draft_id : right.ref.draft_id
  return rightTime - leftTime || leftId.localeCompare(rightId)
}

/** 只在当前账号和作品权限通过核验后，由调用方打开或修改该范围的记录。 */
export function createEditorDraftStorage(options: StorageOptions = {}): EditorDraftStorage {
  const listeners = new Set<(change: EditorDraftChange) => void>()
  const errors = new Set<(error: Error) => void>()
  let connection: Promise<IDBDatabase> | null = null
  let channel: BroadcastChannel | null = null

  function newChannel(): BroadcastChannel | null {
    if (options.broadcast === false || typeof window === 'undefined' || typeof BroadcastChannel === 'undefined') return null
    try {
      return new BroadcastChannel(DATABASE)
    } catch {
      return null
    }
  }

  function publish(change: EditorDraftChange) {
    notify(listeners, change)
    const sender = channel ?? newChannel()
    try {
      sender?.postMessage(change)
    } catch {
      // 广播只是列表失效提示，不参与读取与写入保护。
    } finally {
      if (sender !== channel) sender?.close()
    }
  }

  function database(): Promise<IDBDatabase> {
    if (connection) return connection
    const opening = new Promise<IDBDatabase>((resolve, reject) => {
      const factory = options.factory === undefined ? globalThis.indexedDB : options.factory
      if (!factory) throw new Error('当前浏览器未提供本地草稿存储')
      const request = factory.open(DATABASE, 1)
      let failed = false
      request.onblocked = () => {
        failed = true
        reject(new Error('本地草稿存储正在被其他标签页占用，请关闭旧页面后重试'))
      }
      request.onerror = () => reject(request.error ?? new Error('无法打开本地草稿存储'))
      request.onupgradeneeded = () => {
        if (failed) {
          request.transaction?.abort()
          return
        }
        const store = request.result.createObjectStore(STORE, { keyPath: 'draft_id' })
        store.createIndex(SCOPE_INDEX, SCOPE_KEYS)
        store.createIndex(CHAPTER_INDEX, [...SCOPE_KEYS, 'target.chapter_id'])
      }
      request.onsuccess = () => {
        const db = request.result
        if (failed) {
          db.close()
          return
        }
        const invalidate = (message: string) => {
          if (connection !== opening) return
          connection = null
          db.close()
          notify(errors, new Error(message))
        }
        db.onversionchange = () => invalidate('本地草稿存储版本已变化，请刷新页面后重试')
        db.onclose = () => invalidate('本地草稿存储连接已关闭，请重试')
        resolve(db)
      }
    })
    connection = opening
    void opening.catch(() => { if (connection === opening) connection = null })
    return opening
  }

  async function access<Result>(
    mode: IDBTransactionMode,
    isCurrent: IsCurrent,
    run: (store: IDBObjectStore, watch: WatchRequest, finish: (value: Result) => void) => void,
  ): Promise<Result> {
    requireCurrent(isCurrent)
    const db = await database()
    const result = await transact(db, mode, isCurrent, run)
    requireCurrent(isCurrent)
    return result
  }

  return {
    async list(scope, isCurrent, chapterId) {
      const values = await access<unknown[]>('readonly', isCurrent, (store, watch, finish) => {
        const values = scopeKey(scope)
        const index = chapterId === undefined ? SCOPE_INDEX : CHAPTER_INDEX
        if (chapterId !== undefined) values.push(chapterId)
        watch(store.index(index).getAll(values), finish)
      })
      requireCurrent(isCurrent)
      const entries: EditorDraftEntry[] = []
      for (const value of values) {
        const envelope = decodeDraftEnvelope(value)
        if (!envelope || !sameEditorDraftScope(envelope.scope, scope)) continue
        const entry = decodeEditorDraftEntry(value)
        if (entry) entries.push(entry)
      }
      return entries.sort(sortEntries)
    },

    async put(record, isCurrent, existing = false) {
      const saved = await access<boolean>('readwrite', isCurrent, (store, watch, finish) => {
        watch(store.get(record.draft_id), (raw: unknown) => {
          if (!existing) {
            if (raw !== undefined) return finish(false)
            return watch(store.add(record), () => finish(true))
          }
          const previous = decodeDraftEnvelope(raw)
          if (!previous || previous.schema_version !== 1 || !previous.target
              || previous.ref.writer_id !== record.writer_id || previous.ref.record_seq >= record.record_seq
              || !sameEditorDraftScope(previous.scope, record.scope) || !sameEditorDraftTarget(previous.target, record.target)) {
            return finish(false)
          }
          watch(store.put(record), () => finish(true))
        })
      })
      requireCurrent(isCurrent)
      if (saved) publish({ kind: 'put', scope: record.scope, chapter_id: record.target.chapter_id,
        ...draftRecordRef(record) })
      return saved
    },

    async readRaw(scope, ref, isCurrent) {
      return access<unknown | null>('readonly', isCurrent, (store, watch, finish) => {
        watch(store.get(ref.draft_id), (raw: unknown) => finish(isMatchingRecord(raw, scope, ref) ? raw : null))
      })
    },

    async remove(scope, ref, isCurrent) {
      const removed = await access<{ chapterId: number | undefined } | null>('readwrite', isCurrent, (store, watch, finish) => {
        watch(store.get(ref.draft_id), (raw: unknown) => {
          const envelope = decodeDraftEnvelope(raw)
          if (!envelope || !sameEditorDraftScope(envelope.scope, scope) || !sameRef(envelope.ref, ref)) return finish(null)
          watch(store.delete(ref.draft_id), () => finish({ chapterId: envelope.target?.chapter_id }))
        })
      })
      requireCurrent(isCurrent)
      if (removed?.chapterId !== undefined) publish({ kind: 'remove', scope, chapter_id: removed.chapterId,
        ...draftRecordRef(ref) })
      return removed !== null
    },

    async copyForRecovery(scope, ref, writerId, isCurrent) {
      const copy = await access<EditorDraftRecord | null>('readwrite', isCurrent, (store, watch, finish) => {
        watch(store.get(ref.draft_id), (raw: unknown) => {
          if (!isMatchingRecord(raw, scope, ref)) return finish(null)
          const entry = decodeEditorDraftEntry(raw)
          if (!entry || entry.status !== 'ready') return finish(null)
          const restored: EditorDraftRecord = {
            ...entry.record,
            draft_id: (options.id ?? newEditorDraftId)(),
            writer_id: writerId,
            record_seq: 1,
            saved_at: (options.now ?? Date.now)(),
            recovery_hold: true,
            recovered_from: draftRecordRef(ref),
          }
          watch(store.add(restored), () => finish(restored))
        })
      })
      requireCurrent(isCurrent)
      if (copy) publish({ kind: 'put', scope, chapter_id: copy.target.chapter_id,
        ...draftRecordRef(copy) })
      requireCurrent(isCurrent)
      return copy
    },

    subscribe(listener) {
      listeners.add(listener)
      if (!channel) {
        channel = newChannel()
        if (channel) channel.onmessage = (event: MessageEvent<unknown>) => {
          const change = readChange(event.data)
          if (change) notify(listeners, change)
        }
      }
      return () => {
        listeners.delete(listener)
        if (listeners.size === 0) {
          channel?.close()
          channel = null
        }
      }
    },

    subscribeError(listener) {
      errors.add(listener)
      return () => { errors.delete(listener) }
    },
  }
}

interface PendingWrite {
  record: EditorDraftRecord
  promise: Promise<boolean>
  resolve: (written: boolean) => void
  reject: (error: unknown) => void
}

function pendingWrite(record: EditorDraftRecord): PendingWrite {
  let resolve: PendingWrite['resolve'] = () => {}
  let reject: PendingWrite['reject'] = () => {}
  const promise = new Promise<boolean>((accept, fail) => {
    resolve = accept
    reject = fail
  })
  return { record, promise, resolve, reject }
}

/** 一个分支最多一笔存储事务与一个待写快照，记录序号由编辑器生成。 */
export class EditorDraftWriteQueue {
  private closed = false
  private branch: Pick<DraftRecordRef, 'draft_id' | 'writer_id'> | null = null
  private writing: PendingWrite | null = null
  private waiting: PendingWrite | null = null
  private latest: { seq: number; promise: Promise<boolean> } | null = null
  private committedSeq = 0

  constructor(
    private readonly storage: Pick<EditorDraftStorage, 'put'>,
    private readonly isCurrent: IsCurrent,
    private existing = false,
  ) {}

  private canWrite = () => !this.closed && this.isCurrent()

  enqueue(record: EditorDraftRecord): Promise<boolean> {
    if (!this.canWrite()) return Promise.resolve(false)
    if (!this.branch) this.branch = { draft_id: record.draft_id, writer_id: record.writer_id }
    if (this.branch.draft_id !== record.draft_id || this.branch.writer_id !== record.writer_id) {
      return Promise.reject(new Error('不能将其他草稿写入当前分支队列'))
    }
    if (record.record_seq < (this.latest?.seq ?? this.committedSeq)) return Promise.resolve(false)
    if (record.record_seq === this.committedSeq) return Promise.resolve(true)
    let batch = this.waiting
    if (!batch && this.writing?.record.record_seq === record.record_seq) return this.writing.promise
    if (batch) {
      if (record.record_seq > batch.record.record_seq) batch.record = record
    } else {
      batch = pendingWrite(record)
      this.waiting = batch
    }
    this.latest = { seq: record.record_seq, promise: batch.promise }
    this.start()
    return batch.promise
  }

  flush(): Promise<boolean> {
    if (!this.canWrite()) return Promise.resolve(false)
    return this.latest?.promise ?? Promise.resolve(true)
  }

  close() {
    this.closed = true
    this.waiting?.resolve(false)
    this.waiting = null
  }

  private start() {
    if (!this.canWrite()) {
      this.close()
      return
    }
    if (this.writing || !this.waiting) return
    const batch = this.waiting
    this.waiting = null
    this.writing = batch
    void this.write(batch)
  }

  private async write(batch: PendingWrite) {
    try {
      const written = await this.storage.put(batch.record, this.canWrite, this.existing)
      if (written && this.canWrite()) {
        this.existing = true
        this.committedSeq = batch.record.record_seq
        batch.resolve(true)
      } else {
        batch.resolve(false)
      }
    } catch (error) {
      if (this.canWrite()) batch.reject(error)
      else batch.resolve(false)
    } finally {
      this.writing = null
      if (this.canWrite()) this.start()
      else this.close()
    }
  }
}

export const editorDraftStorage = createEditorDraftStorage()
