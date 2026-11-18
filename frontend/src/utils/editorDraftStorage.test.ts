import { IDBFactory, IDBObjectStore, IDBVersionChangeEvent } from 'fake-indexeddb'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RichDocument } from '../api/richDocument'
import type { EditorDraftScope, SceneEditorDraftRecord, TitleDraftRecord } from '../components/editorDrafts/editorDraftRecord'
import type { SceneEditEvidence } from '../components/scenes/sceneDraftRecord'
import { createEditorDraftStorage, EditorDraftWriteQueue, newEditorDraftId } from './editorDraftStorage'

const scope: EditorDraftScope = {
  actor_id: 1,
  actor_created_at: '2026-10-01T10:00:00Z',
  project_id: 2,
  project_created_at: '2026-10-03T12:00:00Z',
}
const current = () => true
const recording = (record_seq: number, value: string, overrides: Partial<TitleDraftRecord> = {}): TitleDraftRecord => ({
  schema_version: 1,
  draft_id: 'draft-a',
  writer_id: 'tab-a',
  scope,
  target: { kind: 'chapter_title', chapter_id: 11, anchor_scene_id: 21 },
  record_seq,
  edit_seq: record_seq,
  saved_at: 1_000 * record_seq,
  recovery_hold: false,
  recovered_from: null,
  payload: { base: { title: '原始标题', revision: 1 }, value },
  ...overrides,
})

const database = 'ainovel.editor-drafts'
async function openDatabase(factory: IDBFactory, version?: number): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = factory.open(database, version)
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

async function putRaw(factory: IDBFactory, raw: unknown) {
  const db = await openDatabase(factory)
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction('drafts', 'readwrite')
      tx.oncomplete = () => resolve()
      tx.onabort = () => reject(tx.error)
      tx.objectStore('drafts').put(raw)
    })
  } finally {
    db.close()
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('editor draft storage', () => {
  it('reopens independent tab branches and isolates account, project instance and chapter', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const first = recording(1, '第一页草稿 甲')
    const second = recording(1, '副本草稿 乙', { draft_id: 'draft-b', writer_id: 'tab-b' })
    expect(await storage.put(first, current)).toBe(true)
    expect(await storage.put(second, current)).toBe(true)
    const reopened = createEditorDraftStorage({ factory, broadcast: false })
    expect(await reopened.list(scope, current, 11)).toEqual([
      { status: 'ready', record: first },
      { status: 'ready', record: second },
    ])
    expect(await reopened.list({ ...scope, actor_created_at: '2026-10-11T12:00:00Z' }, current)).toEqual([])
    expect(await reopened.list({ ...scope, project_created_at: '2026-10-11T12:00:00Z' }, current)).toEqual([])
    expect(await reopened.list(scope, current, 12)).toEqual([])
    expect(await reopened.readRaw({ ...scope, actor_id: 42 }, first, current)).toBeNull()
    expect(await reopened.remove({ ...scope, actor_id: 42 }, first, current)).toBe(false)
  })

  it('keeps a newer record and cannot recreate an exactly discarded known branch', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const elsewhere = createEditorDraftStorage({ factory, broadcast: false })
    const old = recording(1, '旧稿')
    const latest = recording(3, '包含继续输入的新稿')
    expect(await storage.put(old, current)).toBe(true)
    expect(await storage.put(latest, current, true)).toBe(true)
    expect(await storage.put(recording(2, '迟到的旧内容'), current, true)).toBe(false)
    expect(await elsewhere.remove(scope, old, current)).toBe(false)
    expect(await elsewhere.readRaw(scope, latest, current)).toEqual(latest)
    expect(await elsewhere.remove(scope, latest, current)).toBe(true)
    expect(await storage.put(recording(4, '已关闭分支的迟到写入'), current, true)).toBe(false)
    expect(await storage.list(scope, current)).toEqual([])
  })

  it('copies only the selected exact version and leaves its source intact', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({
      factory,
      broadcast: false,
      now: () => 5_000,
      id: () => 'recovered',
    })
    const original = recording(1, '收到保存回复前关闭')
    const latest = recording(2, '收到保存回复前关闭，又写了句', {
      payload: {
        ...original.payload,
        value: '收到保存回复前关闭，又写了句',
        attempt: { id: 'save-a', title: original.payload.value, expectedTitle: '原始标题', revision: 1, editSeq: 1 },
      },
    })
    await storage.put(original, current)
    await storage.put(latest, current, true)
    expect(await storage.copyForRecovery(scope, original, 'new-tab', current)).toBeNull()
    const copy = await storage.copyForRecovery(scope, latest, 'new-tab', current)
    expect(copy).toEqual({
      ...latest,
      draft_id: 'recovered',
      writer_id: 'new-tab',
      record_seq: 1,
      saved_at: 5_000,
      recovery_hold: true,
      recovered_from: { draft_id: latest.draft_id, writer_id: latest.writer_id, record_seq: latest.record_seq },
    })
    expect(await storage.readRaw(scope, latest, current)).toEqual(latest)
  })

  it('restores rich scene text, layout, notes and mapping evidence without normalizing them', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const sectionKey = '10000000-0000-4000-8000-000000000001'
    const markerKey = '10000000-0000-4000-8000-000000000002'
    const staleMarkerKey = '10000000-0000-4000-8000-000000000003'
    const retiredSectionKey = '10000000-0000-4000-8000-000000000004'
    const baseDocument: RichDocument = {
      content: '甲\r\n🙂乙',
      formatting: { version: 1, boundaries: [], inline: [{ kind: 'bold', start: 3, end: 4 }], blocks: [] },
      sections: [{
        key: sectionKey, anchor: 0, order: 0, title: '背景资料', content: '作者区块',
        formatting: { version: 1, boundaries: [], inline: [{ kind: 'italic', start: 0, end: 2 }], blocks: [] },
        include_in_ai: false,
      }],
      retired_section_keys: [retiredSectionKey],
      markers: [{
        key: markerKey, section_key: sectionKey, status: 'active', start: 0, end: 2,
        color: 'yellow', excerpt: '', excerpt_truncated: false, last_start: null,
      }],
    }
    const draftDocument = { ...baseDocument, content: baseDocument.content + '丙' }
    const sentDocument = { ...baseDocument, content: baseDocument.content + '丁' }
    const evidence: SceneEditEvidence = {
      mappedMarkerKeys: [markerKey], explicitMarkerIntent: false, restoredMarkerKeys: [],
      mappedSectionKeys: [sectionKey], explicitSectionIntent: false, restoredSectionKeys: [],
    }
    const created = '2026-10-06T02:00:00Z'
    const base = { id: 21, chapter_id: 11, created_at: created, content_revision: 9, planning_revision: 3,
      title: '场景', summary: '原摘要', content: baseDocument.content }
    const scene: SceneEditorDraftRecord = {
      ...recording(4, '场景'),
      target: { kind: 'scene_document', chapter_id: 11, scene_id: 21, anchor_scene_id: 21, scene_created_at: created },
      payload: {
        base,
        document: { content_revision: 9, planning_revision: 3, document: baseDocument },
        draft: { title: '场景', summary: '手动改过的摘要', content: draftDocument.content },
        documentDraft: draftDocument,
        evidence,
        retiredMarkerKeys: [staleMarkerKey],
        editSeq: 4,
        bodyEditSeq: 4,
        attempt: {
          id: 2, editSeq: 2, baseRevision: 9, kind: 'document', evidence,
          draft: { title: '场景', summary: '原摘要', content: sentDocument.content },
          payload: {
            expected_content_revision: 9, document: sentDocument,
            add_marker_keys: [], delete_marker_keys: [], restore_marker_keys: [], mapped_marker_keys: [markerKey],
            add_section_keys: [], delete_section_keys: [], restore_section_keys: [], mapped_section_keys: [sectionKey],
          },
        },
      },
    }
    expect(await storage.put(scene, current)).toBe(true)
    const reopened = createEditorDraftStorage({ factory, broadcast: false, id: () => 'restored-scene' })
    expect(await reopened.list(scope, current, 11)).toEqual([{ status: 'ready', record: scene }])
    const copy = await reopened.copyForRecovery(scope, scene, 'tab-new', current)
    expect(copy).toMatchObject({ payload: scene.payload, edit_seq: scene.edit_seq, recovery_hold: true })
    expect(copy?.draft_id).toBe('restored-scene')
  })

  it('keeps incompatible and corrupted records available for an explicit raw export or exact removal', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    await storage.list(scope, current)
    const title = recording(1, '原始文本')
    const future = { ...title, schema_version: 2, payload: { differentlyShaped: title.payload.value } }
    const corrupt = { ...title, draft_id: 'draft-b', payload: { base: { title: '坏草稿', revision: -1 } } }
    await putRaw(factory, future)
    await putRaw(factory, corrupt)
    const { draft_id, writer_id, record_seq, target, saved_at } = future
    expect(await storage.list(scope, current, 11)).toEqual([
      { status: 'unreadable', ref: { draft_id, writer_id, record_seq }, target, saved_at },
      { status: 'unreadable', ref: { draft_id: corrupt.draft_id, writer_id, record_seq }, target, saved_at },
    ])
    expect(await storage.copyForRecovery(scope, corrupt, 'tab-new', current)).toBeNull()
    expect(await storage.put(recording(2, '新版写请求'), current, true)).toBe(false)
    expect(await storage.readRaw(scope, future, current)).toEqual(future)
    expect(await storage.remove(scope, { ...future, record_seq: 2 }, current)).toBe(false)
    expect(await storage.remove(scope, future, current)).toBe(true)
    expect(await storage.readRaw(scope, corrupt, current)).toEqual(corrupt)
  })

  it('waits for transaction completion and preserves the previous draft when the write aborts', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const old = recording(1, '上一份已经完成的草稿')
    await storage.put(old, current)
    const put = IDBObjectStore.prototype.put
    const abort = vi.spyOn(IDBObjectStore.prototype, 'put').mockImplementation(function (this: IDBObjectStore, ...args) {
      const request = put.apply(this, args)
      request.addEventListener('success', () => this.transaction.abort())
      return request
    })
    await expect(storage.put(recording(2, '写入请求成功但事务中止'), current, true)).rejects.toMatchObject({ name: 'AbortError' })
    abort.mockRestore()
    expect(await storage.readRaw(scope, old, current)).toEqual(old)
    expect(await storage.put(recording(2, '重试后完成'), current, true)).toBe(true)
  })

  it('withholds the result after the authorized identity changes during a read', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const original = recording(1, '旧账号的正文')
    await storage.put(original, current)
    let authorized = true
    const get = IDBObjectStore.prototype.get
    const read = vi.spyOn(IDBObjectStore.prototype, 'get').mockImplementation(function (this: IDBObjectStore, key) {
      const request = get.call(this, key)
      request.addEventListener('success', () => { authorized = false })
      return request
    })
    await expect(storage.readRaw(scope, original, () => authorized)).rejects.toMatchObject({ name: 'AbortError' })
    read.mockRestore()
    expect(await storage.readRaw(scope, original, current)).toEqual(original)
  })

  it('reports unavailable IndexedDB to callers without opening another backend', async () => {
    const storage = createEditorDraftStorage({ factory: null, broadcast: false })
    await expect(storage.put(recording(1, '仍在内存中的作者输入'), current)).rejects.toThrow('本地草稿存储')
  })

  it('preserves previously completed writes on quota failure and resumes after an explicit retry', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const queue = new EditorDraftWriteQueue(storage, current)
    const old = recording(1, '上一份草稿')
    expect(await queue.enqueue(old)).toBe(true)
    vi.spyOn(IDBObjectStore.prototype, 'put').mockImplementationOnce(() => {
      throw new DOMException('空间用尽', 'QuotaExceededError')
    })
    const newest = recording(2, '当前仍留在编辑器里的输入')
    await expect(queue.enqueue(newest)).rejects.toMatchObject({ name: 'QuotaExceededError' })
    expect(await storage.readRaw(scope, old, current)).toEqual(old)
    expect(await queue.enqueue(newest)).toBe(true)
    expect(await storage.readRaw(scope, newest, current)).toEqual(newest)
  })

  it('lets callers retry opening a blocked database', async () => {
    const factory = new IDBFactory()
    const open = factory.open.bind(factory)
    vi.spyOn(factory, 'open').mockImplementationOnce((name, version) => {
      const request = open(name, version)
      queueMicrotask(() => request.onblocked?.(new IDBVersionChangeEvent('blocked')))
      return request
    })
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    await expect(storage.list(scope, current)).rejects.toThrow('占用')
    expect(await storage.put(recording(1, '另一标签页退出后保存'), current)).toBe(true)
  })

  it('releases its connection on version change and preserves records in a newer database', async () => {
    const factory = new IDBFactory()
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const onError = vi.fn()
    const unsubscribe = storage.subscribeError(onError)
    const old = recording(1, '升级前保存的草稿')
    expect(await storage.put(old, current)).toBe(true)
    const upgraded = await openDatabase(factory, 2)
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining('版本已变化') }))
    await expect(storage.list(scope, current)).rejects.toMatchObject({ name: 'VersionError' })
    const retained = await new Promise<unknown>((resolve, reject) => {
      const tx = upgraded.transaction('drafts')
      const request = tx.objectStore('drafts').get(old.draft_id)
      tx.oncomplete = () => resolve(request.result)
      tx.onabort = () => reject(tx.error)
    })
    expect(retained).toEqual(old)
    upgraded.close()
    unsubscribe()
  })

  it('notifies unexpected closure and reconnects without losing a completed record', async () => {
    const factory = new IDBFactory()
    const open = factory.open.bind(factory)
    let databaseHandle: IDBDatabase | null = null
    vi.spyOn(factory, 'open').mockImplementationOnce((name, version) => {
      const request = open(name, version)
      request.addEventListener('success', () => { databaseHandle = request.result })
      return request
    })
    const storage = createEditorDraftStorage({ factory, broadcast: false })
    const onError = vi.fn()
    const unsubscribe = storage.subscribeError(onError)
    const draft = recording(1, '关闭连接前的草稿')
    expect(await storage.put(draft, current)).toBe(true)
    // 浏览器异常关闭通知与普通 close() 不同，手动触发适配器的事件入口。
    function signalClose(db: IDBDatabase | null) {
      if (!db) throw new Error('测试未捕获数据库连接')
      db.onclose?.(new Event('close'))
    }
    signalClose(databaseHandle)
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringContaining('连接已关闭') }))
    expect(await storage.readRaw(scope, draft, current)).toEqual(draft)
    unsubscribe()
  })

  it('generates distinct UUIDs on HTTP origins where randomUUID is unavailable', () => {
    const random = vi.fn()
      .mockImplementationOnce((bytes: Uint8Array) => bytes.fill(0))
      .mockImplementationOnce((bytes: Uint8Array) => bytes.fill(1))
    vi.stubGlobal('crypto', { getRandomValues: random })
    expect(newEditorDraftId()).toBe('00000000-0000-4000-8000-000000000000')
    expect(newEditorDraftId()).toBe('01010101-0101-4101-8101-010101010101')
  })
})

describe('editor draft write queue', () => {
  it('starts the first write immediately and keeps only the latest waiting snapshot', async () => {
    let done: (value: boolean) => void = () => {}
    const gate = new Promise<boolean>((resolve) => { done = resolve })
    const put = vi.fn().mockReturnValueOnce(gate).mockResolvedValue(true)
    const queue = new EditorDraftWriteQueue({ put }, current)
    const first = recording(1, '第一段')
    const newest = recording(3, '第三段')
    const saving = queue.enqueue(first)
    const oldWaiting = queue.enqueue(recording(2, '第二段'))
    const newWaiting = queue.enqueue(newest)
    expect(newWaiting).toBe(oldWaiting)
    expect(put).toHaveBeenCalledTimes(1)
    expect(put).toHaveBeenNthCalledWith(1, first, expect.any(Function), false)
    done(true)
    expect(await Promise.all([saving, oldWaiting, newWaiting, queue.flush()])).toEqual([true, true, true, true])
    expect(put).toHaveBeenCalledTimes(2)
    expect(put).toHaveBeenNthCalledWith(2, newest, expect.any(Function), true)
  })

  it('closes pending writes by invalidating the in-flight guard and cannot accept another snapshot', async () => {
    let done: (value: boolean) => void = () => {}
    const gate = new Promise<boolean>((resolve) => { done = resolve })
    const put = vi.fn().mockReturnValue(gate)
    const queue = new EditorDraftWriteQueue({ put }, current)
    const saving = queue.enqueue(recording(1, '已发出的第一稿'))
    const pending = queue.enqueue(recording(2, '待写第二稿'))
    queue.close()
    expect(put.mock.calls[0][1]()).toBe(false)
    expect(await pending).toBe(false)
    done(true)
    expect(await saving).toBe(false)
    expect(await queue.enqueue(recording(3, '旧组件迟到回执'))).toBe(false)
    expect(put).toHaveBeenCalledTimes(1)
  })
})
