import { getSchema } from '@tiptap/core'
import { history, redoDepth, undo } from '@tiptap/pm/history'
import { EditorState } from '@tiptap/pm/state'
import { StarterKit } from '@tiptap/starter-kit'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { RichDocument, RichDocumentOut } from '../../api/richDocument'
import type { Scene, ScenePage } from '../../api/types'
import { planSectionCreation, planSectionDeletion } from './richSectionOperations'
import { RichMarkerDocument } from './richMarkerState'
import { AuthorSection } from './richSectionNode'
import { mapSectionMarkerTransaction } from './richSectionMarkerMapping'
import { syncSceneSaveBaseline } from './richSectionHistory'
import { editorFromSectionDocument, projectSectionDocument } from './richSectionProjection'
import { createSectionPlanTransaction, mappedSectionIdentities } from './richSectionTransaction'
import { FocusableListItem, RichPrefixes } from './richTextExtensions'
import { SceneDraftController, sceneDraftDirty, type SceneDraftRecord } from './sceneDraftController'

const document = (content = '甲\r\n🙂乙'): RichDocument => ({
  content, formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [],
})
const scene = (revision = 1): Scene => ({
  id: 4, chapter_id: 9, chapter_no: 1, chapter_title: '章', title: '景', content: '甲\r\n🙂乙', summary: '',
  sort: 0, word_count: 3, content_revision: revision, planning_revision: 0, created_at: '2026-11-17T12:34:56Z',
  updated_at: '', separator_before: '', active: true,
  planning: { unit_id: null, binding_epoch: null, revision: 0, raw: { modes: { pov: 'inherit', tags: 'inherit', character: 'inherit', world: 'inherit', organization: 'inherit', career: 'inherit', relationship: 'inherit' }, pov_character_id: null, tag_ids: [], references: [] }, effective: { pov_character_id: null, pov_character_name: null, tags: [], references: [], sources: { pov: 'chapter', tags: 'chapter', character: 'chapter', world: 'chapter', organization: 'chapter', career: 'chapter', relationship: 'chapter' } } },
})
const page = (row: Scene): ScenePage => ({ items: [row], total: 1, offset: 0, limit: 100, content_revision: row.content_revision, planning_revision: 0 })
const out = (revision = 1, value = document()): RichDocumentOut => ({ content_revision: revision, planning_revision: 0, document: value })
const SECTION = '12345678-1234-4123-8123-123456789abc'
const MARKER = '12345678-1234-4123-8123-123456789def'
const schema = getSchema([StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
  horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
RichMarkerDocument, FocusableListItem, RichPrefixes, AuthorSection])
const editorState = (value: RichDocument) => EditorState.create({
  schema, doc: schema.nodeFromJSON(editorFromSectionDocument(value)), plugins: [history()],
})

function setup() {
  const api = { list: vi.fn(async () => page(scene())), get: vi.fn(async () => scene()), update: vi.fn(async () => scene(2)),
    getDocument: vi.fn(async () => out()), saveDocument: vi.fn(async (_id: number, body: { document: RichDocument }) => out(2, body.document)) }
  const controller = new SceneDraftController(9, api, () => true, () => true)
  controller.activate()
  return { api, controller }
}

describe('完整场景文档草稿', () => {
  it('请求发送前持久化冻结快照，期间撤权使网络写入不发生', async () => {
    const { controller, api } = setup()
    await controller.load()
    const source = document('落盘后才发送')
    controller.changeDocument(source)
    let finish!: () => void
    const beforeWrite = vi.fn<(record: SceneDraftRecord) => Promise<void>>(
      () => new Promise<void>((resolve) => { finish = resolve }),
    )
    const saving = controller.save('document', beforeWrite)
    expect(beforeWrite.mock.calls[0]?.[0]).toMatchObject({ attempt: { kind: 'document', payload: { document: source } } })
    expect(api.saveDocument).not.toHaveBeenCalled()
    controller.deactivate()
    finish()
    expect(await saving).toBeNull()
    expect(api.saveDocument).not.toHaveBeenCalled()
  })

  it('确认自动正文快照期间的新输入仍待保存，独立摘要不随正文提交', async () => {
    const { api, controller } = setup()
    await controller.load()
    const sent = document('首次正文')
    const next = document('请求途中继续写')
    controller.changeDocument(sent)
    controller.change({ summary: '手动摘要' })
    let finish!: (result: RichDocumentOut) => void
    api.saveDocument.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    api.get.mockResolvedValueOnce({ ...scene(2), content: sent.content })
    const saving = controller.save('document')
    controller.changeDocument(next)
    finish(out(2, sent))
    expect(await saving).not.toBeNull()
    expect(api.update).not.toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ scene: { content_revision: 2, content: sent.content },
      documentDraft: next, draft: { content: next.content, summary: '手动摘要' } })
    expect(sceneDraftDirty(controller.getSnapshot())).toBe(true)
  })

  it('中文组合输入提交后才应用在途回执，组合后的完整正文保留为下一稿', async () => {
    const { api, controller } = setup()
    await controller.load()
    const sent = document('先保存')
    controller.changeDocument(sent)
    let finish!: (result: RichDocumentOut) => void
    api.saveDocument.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    api.get.mockResolvedValueOnce({ ...scene(2), content: sent.content })
    const pending = controller.save('document')
    controller.setComposing(true)
    finish(out(2, sent))
    await Promise.resolve()
    expect(controller.getSnapshot()).toMatchObject({ scene: { content_revision: 1 }, saving: true })
    controller.changeDocument(document('先保存，组合输入'))
    controller.setComposing(false)
    expect(await pending).not.toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ scene: { content_revision: 2, content: '先保存' },
      documentDraft: { content: '先保存，组合输入' } })
  })

  it('新建区块在途撤销后补退场身份，元数据同步不破坏 PM 重做历史', async () => {
    const { api, controller } = setup()
    const base = document()
    await controller.load()
    let pm = editorState(base)
    pm = pm.apply(createSectionPlanTransaction(pm, { action: 'create', key: SECTION, cursor: {
      scope: 'prose', offset: 1, leftSection: null, rightSection: null,
    } }, base))
    const sent = projectSectionDocument(pm.doc.toJSON())
    controller.changeDocument(sent, { mappedMarkerKeys: [], explicitMarkerIntent: false, explicitSectionIntent: true })
    let finish!: (result: RichDocumentOut) => void
    api.saveDocument.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    api.get.mockResolvedValueOnce(scene(2))
    const pending = controller.save('document')
    expect(undo(pm, (transaction) => { pm = pm.apply(transaction) })).toBe(true)
    controller.changeDocument(projectSectionDocument(pm.doc.toJSON()), {
      mappedMarkerKeys: [], explicitMarkerIntent: true, explicitSectionIntent: true,
    })
    expect(controller.getSnapshot().documentDraft?.retired_section_keys).toEqual([])
    finish(out(2, sent))
    expect(await pending).not.toBeNull()
    const rebased = controller.getSnapshot().documentDraft!
    expect(rebased).toMatchObject({ sections: [], retired_section_keys: [SECTION] })
    const sync = syncSceneSaveBaseline(pm, sent)!
    expect(sync.getMeta('addToHistory')).toBe(false)
    pm = pm.apply(sync)
    expect(projectSectionDocument(pm.doc.toJSON())).toEqual(rebased)
    expect(redoDepth(pm)).toBe(1)
    api.get.mockResolvedValueOnce(scene(3))
    api.saveDocument.mockResolvedValueOnce(out(3, rebased))
    expect(await controller.save('document')).not.toBeNull()
    expect(api.saveDocument).toHaveBeenLastCalledWith(4, expect.objectContaining({ delete_section_keys: [SECTION],
      expected_content_revision: 2 }), expect.any(Function))
  })

  it('在途删除区块后真实 undo 得到的恢复证据通过下一次 CAS', async () => {
    const { api, controller } = setup()
    const base = planSectionCreation(document(), { scope: 'prose', offset: 1, leftSection: null, rightSection: null }, SECTION)
    api.getDocument.mockResolvedValueOnce(out(1, base))
    await controller.load()
    let pm = editorState(base)
    pm = pm.apply(createSectionPlanTransaction(pm, { action: 'delete', key: SECTION }, base))
    const deletion = projectSectionDocument(pm.doc.toJSON())
    controller.changeDocument(deletion, { mappedMarkerKeys: [], explicitMarkerIntent: false, explicitSectionIntent: true })
    let finish!: (result: RichDocumentOut) => void
    api.saveDocument.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    api.get.mockResolvedValueOnce(scene(2))
    const pending = controller.save('document')
    expect(undo(pm, (transaction) => { pm = pm.apply(transaction) })).toBe(true)
    const restored = projectSectionDocument(pm.doc.toJSON())
    controller.changeDocument(restored, { mappedMarkerKeys: [], explicitMarkerIntent: true,
      explicitSectionIntent: true, restoredSectionKeys: restored.sections.map((item) => item.key) })
    finish(out(2, deletion))
    expect(await pending).not.toBeNull()
    api.get.mockResolvedValueOnce(scene(3))
    api.saveDocument.mockResolvedValueOnce(out(3, restored))
    expect(await controller.save('document')).not.toBeNull()
    expect(api.saveDocument).toHaveBeenLastCalledWith(4, expect.objectContaining({ restore_section_keys: [SECTION],
      add_section_keys: [], expected_content_revision: 2 }), expect.any(Function))
  })

  it('正文保存途中标记失效，按已确认快照重算摘录但保留未保存片段', async () => {
    const { api, controller } = setup()
    const base = document()
    base.markers = [{ key: MARKER, section_key: null, status: 'active', start: 0, end: 1,
      color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }]
    api.getDocument.mockResolvedValueOnce(out(1, base))
    await controller.load()
    let pm = editorState(base)
    const edit = (transaction: ReturnType<typeof pm.tr.insertText>) => {
      const before = projectSectionDocument(pm.doc.toJSON())
      const markers = mapSectionMarkerTransaction(transaction, before.markers, base)!
      const sectionKeys = mappedSectionIdentities(transaction)!
      pm = pm.apply(transaction.setDocAttribute('markers', markers))
      controller.changeDocument(projectSectionDocument(pm.doc.toJSON()), { mappedMarkerKeys: markers.map((item) => item.key),
        explicitMarkerIntent: false, mappedSectionKeys: sectionKeys })
      return projectSectionDocument(pm.doc.toJSON())
    }
    const sent = edit(pm.tr.insertText('前', 1))
    let finish!: (result: RichDocumentOut) => void
    api.saveDocument.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    api.get.mockResolvedValueOnce({ ...scene(2), content: sent.content })
    const pending = controller.save('document')
    edit(pm.tr.insertText('中', 1))
    const tail = edit(pm.tr.delete(3, 4))
    expect(tail.markers[0]).toMatchObject({ status: 'stale', excerpt: '甲', last_start: 0, draft_last_start: 2 })
    finish(out(2, sent))
    expect(await pending).not.toBeNull()
    const rebased = controller.getSnapshot().documentDraft!
    expect(rebased.markers[0]).toMatchObject({ status: 'stale', excerpt: '甲', last_start: 1, draft_last_start: 2 })
    const sync = syncSceneSaveBaseline(pm, sent)!
    expect(projectSectionDocument(pm.apply(sync).doc.toJSON())).toEqual(rebased)
    api.get.mockResolvedValueOnce({ ...scene(3), content: tail.content })
    api.saveDocument.mockResolvedValueOnce(out(3, rebased))
    expect(await controller.save('document')).not.toBeNull()
  })

  it('未知结果读取仍是原基线时禁止换稿，明确重试发送原快照而非其后编辑', async () => {
    const { api, controller } = setup()
    await controller.load()
    const sent = document('原写入')
    controller.changeDocument(sent)
    api.saveDocument.mockRejectedValueOnce(new ApiError(0, '网络中断'))
    await controller.save('document')
    const tail = document('网络故障后继续写')
    controller.changeDocument(tail)
    await controller.reviewLatest()
    expect(controller.getSnapshot()).toMatchObject({ unknownSave: true, unknownRetryable: true })
    expect(controller.acknowledgeLatest()).toBe(false)
    expect(controller.adoptLatest()).toBe(false)
    api.get.mockResolvedValueOnce({ ...scene(2), content: sent.content })
    expect(await controller.retryUnknown()).not.toBeNull()
    expect(api.saveDocument.mock.calls[1]?.[1]).toMatchObject({ document: sent, expected_content_revision: 1 })
    expect(controller.getSnapshot()).toMatchObject({ unknownSave: false, documentDraft: tail, scene: { content_revision: 2 } })
  })

  it('重开后用完整且规范化的标记内容结算旧回执，不再次提交正文', async () => {
    const original = setup()
    await original.controller.load()
    const saved = document()
    saved.markers = [
      { key: MARKER, section_key: null, status: 'active', start: 0, end: 1, color: 'blue',
        excerpt: '', excerpt_truncated: false, last_start: null },
      { key: SECTION, section_key: null, status: 'active', start: 3, end: 4, color: 'red',
        excerpt: '', excerpt_truncated: false, last_start: null },
    ]
    original.controller.changeDocument(saved, { mappedMarkerKeys: [], explicitMarkerIntent: true })
    original.api.saveDocument.mockRejectedValueOnce(new ApiError(0, '响应丢失'))
    await original.controller.save('document')
    original.controller.change({ summary: '提交后的独立摘要' })
    const stored: unknown = JSON.parse(JSON.stringify(original.controller.captureDraft()))

    const reopened = setup()
    const remote = { ...saved, markers: [...saved.markers].reverse().map((marker) => ({
      draft_last_start: null, draft_excerpt_truncated: false, draft_excerpt: null, ...marker,
    })) }
    reopened.api.list.mockResolvedValue(page(scene(2)))
    reopened.api.get.mockResolvedValue(scene(2))
    reopened.api.getDocument.mockResolvedValue(out(2, remote))
    await reopened.controller.load()
    const record = original.controller.captureDraft()!
    // 只变更意图数据不能借复原入口重放旧请求。
    const invalid = { ...record, attempt: { ...record.attempt!,
      payload: { ...record.attempt!.payload!, add_marker_keys: [] } } }
    expect(await reopened.controller.restoreDraft(invalid)).toBe('invalid')
    expect(await reopened.controller.restoreDraft(stored)).toBe('restored')
    expect(reopened.controller.getSnapshot()).toMatchObject({ unknownSave: false, scene: { content_revision: 2 },
      draft: { summary: '提交后的独立摘要' }, documentDraft: remote,
      lastOwnSave: null })
    expect(reopened.api.saveDocument).not.toHaveBeenCalled()
  })

  it('正文成功后元数据收到 403 时，只将该元数据阶段留为未知', async () => {
    const { api, controller } = setup()
    await controller.load()
    const local = document('成功正文')
    controller.changeDocument(local)
    controller.change({ title: '新的场景名', summary: '新的摘要' })
    api.get.mockResolvedValue({ ...scene(2), content: local.content })
    api.getDocument.mockResolvedValue(out(2, local))
    api.update.mockRejectedValueOnce(new ApiError(403, '响应生成期间权限变化'))
    expect(await controller.save()).toBeNull()
    expect(controller.captureDraft()).toMatchObject({ base: { content_revision: 2 },
      draft: { summary: '新的摘要' }, attempt: { kind: 'metadata', baseRevision: 2 } })
    expect(controller.getSnapshot().lastOwnSave).toMatchObject({ from: 1, to: 2 })
    expect(await controller.reviewLatest()).toBe(true)
    expect(controller.getSnapshot().unknownRetryable).toBe(true)
    api.update.mockResolvedValueOnce({ ...scene(3), content: local.content, title: '新的场景名', summary: '新的摘要' })
    expect(await controller.retryUnknown()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot()).toMatchObject({ unknownSave: false, scene: { content_revision: 3, summary: '新的摘要' } })
  })


  it('保存格式与正文同一 CAS，不规范化未改动的换行', async () => {
    const { api, controller } = setup()
    await controller.load()
    const changed = document('甲\r\n🙂乙!')
    changed.formatting.inline = [{ kind: 'bold', start: 3, end: 4 }]
    controller.changeDocument(changed)
    api.get.mockResolvedValueOnce({ ...scene(2), content: changed.content })
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledWith(4, expect.objectContaining({ expected_content_revision: 1, document: changed }), expect.any(Function))
    expect(controller.getSnapshot().draft.content).toBe('甲\r\n🙂乙!')
    expect(controller.getSnapshot().document?.document).toEqual(changed)
  })
  it('服务器结构同时变化时不可用旧版元数据覆盖新结构', async () => {
    const { api, controller } = setup()
    await controller.load()
    const local = document('甲\r\n🙂乙!')
    controller.changeDocument(local)
    api.saveDocument.mockRejectedValueOnce(new ApiError(409, '版本过期'))
    api.get.mockResolvedValueOnce(scene(2))
    const remote = document()
    remote.formatting.inline = [{ kind: 'bold', start: 0, end: 1 }]
    api.getDocument.mockResolvedValueOnce(out(2, remote))
    expect(await controller.save()).toBeNull()
    controller.acknowledgeLatest()
    expect(controller.getSnapshot().scene?.content_revision).toBe(1)
    expect(controller.getSnapshot().documentDraft).toEqual(local)
    expect(controller.getSnapshot().error).toContain('结构')
    expect(api.saveDocument).toHaveBeenCalledTimes(1)
  })
  it('完整文档写入后网络失败时保留结构草稿，核对前不重复提交', async () => {
    const { api, controller } = setup()
    await controller.load()
    const local = document('甲\r\n🙂乙!')
    controller.changeDocument(local)
    api.saveDocument.mockRejectedValueOnce(new ApiError(0, '网络中断'))
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ documentDraft: local, externalChange: true, saving: false })
    expect(await controller.save()).toBeNull()
    expect(api.saveDocument).toHaveBeenCalledTimes(1)
    await controller.reviewLatest()
    expect(controller.acknowledgeLatest()).toBe(false)
    api.get.mockResolvedValueOnce({ ...scene(2), content: local.content })
    expect(await controller.retryUnknown()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledTimes(2)
  })
  it('未建立事务映射前拒绝改变包含标记的文档，不凭相同 UUID 冒充映射', async () => {
    const { api, controller } = setup()
    const source = document()
    source.markers.push({ key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active',
      start: 0, end: 1, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    controller.changeDocument({ ...source, content: `新${source.content}` })
    expect(controller.getSnapshot().documentDraft).toEqual(source)
    expect(controller.getSnapshot().error).toContain('映射')
    expect(api.saveDocument).not.toHaveBeenCalled()
  })
  it('仅接受事务证明过的标记映射，并将证明随同完整文档提交', async () => {
    const { api, controller } = setup()
    const source = document()
    source.markers.push({ key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active',
      start: 0, end: 1, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    const changed = { ...source, content: `新${source.content}`, markers: source.markers.map((item) => ({ ...item, start: 1, end: 2 })) }
    controller.changeDocument(changed, { mappedMarkerKeys: [source.markers[0].key], explicitMarkerIntent: false })
    api.get.mockResolvedValueOnce({ ...scene(2), content: changed.content })
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledWith(4, expect.objectContaining({ mapped_marker_keys: [source.markers[0].key] }), expect.any(Function))
  })
  it('新增和删除标记必须来自显式事务，不能依靠投影缺项推断', async () => {
    const { api, controller } = setup()
    await controller.load()
    const local = document()
    local.markers.push({ key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active',
      start: 0, end: 1, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    controller.changeDocument(local)
    expect(controller.getSnapshot().documentDraft?.markers).toEqual([])
    controller.changeDocument(local, { mappedMarkerKeys: [], explicitMarkerIntent: true })
    api.get.mockResolvedValueOnce(scene(2))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledWith(4, expect.objectContaining({ add_marker_keys: [local.markers[0].key] }), expect.any(Function))
  })
  it('响应错配不能清掉本地完整草稿或假定保存成功', async () => {
    const { api, controller } = setup()
    await controller.load()
    const local = document('甲\r\n🙂乙!')
    controller.changeDocument(local)
    api.saveDocument.mockResolvedValueOnce(out(2, document('错误正文')))
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot().documentDraft).toEqual(local)
    expect(controller.getSnapshot().scene?.content_revision).toBe(1)
    expect(controller.getSnapshot().externalChange).toBe(true)
  })
  it('保存后的历史重做只能用真实历史事务显式复活刚删除的同景身份', async () => {
    const { api, controller } = setup()
    const source = document()
    const marker = { key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active' as const,
      start: 0, end: 1, color: 'blue' as const, excerpt: '', excerpt_truncated: false, last_start: null }
    source.markers.push(marker)
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    controller.changeDocument({ ...source, markers: [] }, { mappedMarkerKeys: [], explicitMarkerIntent: true })
    api.get.mockResolvedValueOnce(scene(2))
    expect(await controller.save()).not.toBeNull()
    controller.changeDocument(source, { mappedMarkerKeys: [], explicitMarkerIntent: true })
    expect(controller.getSnapshot().documentDraft?.markers).toEqual([])
    expect(controller.getSnapshot().error).toContain('历史')
    controller.changeDocument(source, { mappedMarkerKeys: [], explicitMarkerIntent: true, restoredMarkerKeys: [marker.key] })
    api.get.mockResolvedValueOnce(scene(3))
    api.saveDocument.mockResolvedValueOnce(out(3, source))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenLastCalledWith(4, expect.objectContaining({
      add_marker_keys: [], restore_marker_keys: [marker.key], expected_content_revision: 2,
    }), expect.any(Function))
  })

  it('组合输入时标记为未保存并拒绝保存／延迟刷新，完成后仍保留草稿', async () => {
    const { api, controller } = setup()
    await controller.load()
    controller.setComposing(true)
    expect(sceneDraftDirty(controller.getSnapshot())).toBe(true)
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot().error).toContain('输入法')
    expect(await controller.load()).toBe(false)
    expect(api.saveDocument).not.toHaveBeenCalled()
    expect(api.list).toHaveBeenCalledTimes(1)
    controller.changeDocument(document('甲拼音\r\n🙂乙'))
    controller.setComposing(false)
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledTimes(2))
    expect(controller.getSnapshot().draft.content).toBe('甲拼音\r\n🙂乙')
    expect(sceneDraftDirty(controller.getSnapshot())).toBe(true)
  })

  it('只有退场区块身份时保留墓碑并允许编辑正文，不能改动其身份', async () => {
    const { api, controller } = setup()
    const source = { ...document(), retired_section_keys: ['12345678-1234-4123-8123-123456789abc'] }
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    controller.changeDocument({ ...source, retired_section_keys: [] })
    expect(controller.getSnapshot().documentDraft).toEqual(source)
    const changed = { ...source, content: `${source.content}!` }
    controller.changeDocument(changed)
    api.get.mockResolvedValueOnce({ ...scene(2), content: changed.content })
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledWith(4, expect.objectContaining({ document: changed, delete_section_keys: [] }), expect.any(Function))
  })

  it('正文变化时，每个存活区块必须有事务映射证据而不是仅凭同一 UUID', async () => {
    const { api, controller } = setup()
    const source = document()
    source.sections.push({ key: 'a', anchor: 1, order: 0, title: '备选', content: '备🙂',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    const changed = { ...source, content: `新${source.content}`, sections: [{ ...source.sections[0], anchor: 2 }] }
    controller.changeDocument(changed)
    expect(controller.getSnapshot().documentDraft).toEqual(source)
    expect(controller.getSnapshot().error).toContain('映射')
    controller.changeDocument(changed, { mappedMarkerKeys: [], explicitMarkerIntent: false, mappedSectionKeys: ['a'] })
    api.get.mockResolvedValueOnce({ ...scene(2), content: changed.content })
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledWith(4, expect.objectContaining({ mapped_section_keys: ['a'] }), expect.any(Function))
  })

  it('区块内文字变化要证明该区块活动标记的映射；空白标题不能保存', async () => {
    const { api, controller } = setup()
    const source = document()
    source.sections.push({ key: 'a', anchor: 1, order: 0, title: '备选', content: '备🙂',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    source.markers.push({ key: 'm', section_key: 'a', status: 'active', start: 0, end: 2, color: 'red',
      excerpt: '', excerpt_truncated: false, last_start: null })
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    const changed = { ...source, sections: [{ ...source.sections[0], content: '备X🙂', title: '　' }],
      markers: [{ ...source.markers[0], end: 3 }] }
    controller.changeDocument(changed, { mappedMarkerKeys: [], explicitMarkerIntent: false, mappedSectionKeys: ['a'] })
    expect(controller.getSnapshot().documentDraft).toEqual(source)
    controller.changeDocument(changed, { mappedMarkerKeys: ['m'], explicitMarkerIntent: false, mappedSectionKeys: ['a'] })
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot().error).toContain('区块名称')
    expect(api.saveDocument).not.toHaveBeenCalled()
    controller.changeDocument({ ...changed, sections: [{ ...changed.sections[0], title: '有效名称' }] },
      { mappedMarkerKeys: [], explicitMarkerIntent: false, explicitSectionIntent: true })
    api.get.mockResolvedValueOnce(scene(2))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledWith(4, expect.objectContaining({ mapped_marker_keys: ['m'] }), expect.any(Function))
  })

  it('区块超限给中文提示且保留全部草稿，缩减到码点边界后可保存', async () => {
    const { api, controller } = setup()
    const source = document()
    source.sections.push({ key: 'a', anchor: 1, order: 0, title: '备选', content: '原文',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    const text = '🙂'.repeat(500_000)
    const over = { ...source, sections: [{ ...source.sections[0], content: `${text}字` }] }
    controller.changeDocument(over, { mappedMarkerKeys: [], explicitMarkerIntent: false, mappedSectionKeys: ['a'] })
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot().error).toContain('第 1 个区块的正文超过 50 万字符')
    expect(controller.getSnapshot().documentDraft).toEqual(over)
    expect(api.saveDocument).not.toHaveBeenCalled()
    const legal = { ...source, sections: [{ ...source.sections[0], content: text }] }
    controller.changeDocument(legal, { mappedMarkerKeys: [], explicitMarkerIntent: false, mappedSectionKeys: ['a'] })
    api.get.mockResolvedValueOnce(scene(2))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledWith(4, expect.objectContaining({ document: legal }), expect.any(Function))
  })

  it('服务端元数据字节超限不估算替代，显示 8 MiB 上限并保留草稿供修改', async () => {
    const { api, controller } = setup()
    await controller.load()
    const changed = document()
    changed.formatting.inline = [{ kind: 'bold', start: 0, end: 1 }]
    controller.changeDocument(changed)
    api.saveDocument.mockRejectedValueOnce(new ApiError(422, '请求参数校验失败：document: 文档元数据总量超过上限'))
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot().error).toContain('元数据合计超过 8 MiB')
    expect(controller.getSnapshot().documentDraft).toEqual(changed)
    expect(controller.getSnapshot().scene?.content_revision).toBe(1)
    api.get.mockResolvedValueOnce(scene(2))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenCalledTimes(2)
  })

  it('区块新建和跨保存撤销复活显式提交意图及原身份，附属标记不丢', async () => {
    const { api, controller } = setup()
    const source = document()
    source.sections.push({ key: 'a', anchor: 1, order: 0, title: '备选', content: '备🙂',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    source.markers.push({ key: 'm', section_key: 'a', status: 'active', start: 0, end: 2, color: 'red',
      excerpt: '', excerpt_truncated: false, last_start: null })
    api.getDocument.mockResolvedValueOnce(out(1, source))
    await controller.load()
    const created = planSectionCreation(source, { scope: 'prose', offset: 1, leftSection: 'a', rightSection: null }, 'b')
    controller.changeDocument(created)
    expect(controller.getSnapshot().documentDraft).toEqual(source)
    controller.changeDocument(created, { mappedMarkerKeys: [], explicitMarkerIntent: false, explicitSectionIntent: true })
    api.get.mockResolvedValueOnce(scene(2))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenLastCalledWith(4, expect.objectContaining({ add_section_keys: ['b'] }), expect.any(Function))

    const deleted = planSectionDeletion(created, 'a', created)
    controller.changeDocument(deleted, { mappedMarkerKeys: ['m'], explicitMarkerIntent: false, explicitSectionIntent: true })
    api.get.mockResolvedValueOnce(scene(3))
    api.saveDocument.mockResolvedValueOnce(out(3, deleted))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenLastCalledWith(4, expect.objectContaining({ delete_section_keys: ['a'], mapped_marker_keys: ['m'] }), expect.any(Function))
    controller.changeDocument(created, { mappedMarkerKeys: ['m'], explicitMarkerIntent: true, explicitSectionIntent: true })
    expect(controller.getSnapshot().documentDraft).toEqual(deleted)
    controller.changeDocument(created, { mappedMarkerKeys: ['m'], explicitMarkerIntent: true, explicitSectionIntent: true,
      restoredSectionKeys: ['a'] })
    api.get.mockResolvedValueOnce(scene(4))
    api.saveDocument.mockResolvedValueOnce(out(4, created))
    expect(await controller.save()).not.toBeNull()
    expect(api.saveDocument).toHaveBeenLastCalledWith(4, expect.objectContaining({ restore_section_keys: ['a'], add_section_keys: [],
      mapped_marker_keys: ['m'], expected_content_revision: 3 }), expect.any(Function))
  })

})
