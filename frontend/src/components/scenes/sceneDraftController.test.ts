import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { Scene, ScenePage } from '../../api/types'
import { SceneDraftController } from './sceneDraftController'

function scene(id = 1, revision = 2): Scene {
  return {
    id, chapter_id: 9, chapter_no: 1, chapter_title: '章', title: `场景${id}`, content: '甲😀乙', summary: '原摘要',
    sort: id - 1, word_count: 3, content_revision: revision, planning_revision: 0, created_at: '', updated_at: '', separator_before: '', active: true,
    planning: { unit_id: null, binding_epoch: null, revision: 0, raw: {
      modes: { pov: 'inherit', tags: 'inherit', character: 'inherit', world: 'inherit', organization: 'inherit', career: 'inherit', relationship: 'inherit' },
      pov_character_id: null, tag_ids: [], references: [],
    }, effective: { pov_character_id: null, pov_character_name: null, tags: [], references: [], sources: { pov: 'chapter', tags: 'chapter', character: 'chapter', world: 'chapter', organization: 'chapter', career: 'chapter', relationship: 'chapter' } } },
  }
}
const page = (items: Scene[]): ScenePage => ({ items, total: items.length, offset: 0, limit: 100, content_revision: items[0].content_revision, planning_revision: 0 })
function setup() {
  const api = { list: vi.fn(async () => page([scene(), scene(2)])), get: vi.fn(async (id: number) => scene(id)), update: vi.fn(async (id: number) => scene(id, 3)) }
  const controller = new SceneDraftController(9, api, () => true, () => true)
  controller.activate()
  return { api, controller }
}
describe('场景草稿控制器', () => {
  it('保存失败时保留原场景的正文、标题、摘要，不切走', async () => {
    const { api, controller } = setup()
    await controller.load()
    controller.change({ title: '新名', content: '未保存😀', summary: '未保存摘要' })
    api.update.mockRejectedValueOnce(new ApiError(0, '网络错误'))
    expect(await controller.select(2)).toBe(false)
    expect(controller.getSnapshot().scene?.id).toBe(1)
    expect(controller.getSnapshot().draft).toEqual({ title: '新名', content: '未保存😀', summary: '未保存摘要' })
  })
  it('409 只取最新资料供核对，不自动换版本或重发', async () => {
    const { api, controller } = setup()
    await controller.load()
    controller.change({ content: '本地草稿' })
    api.update.mockRejectedValueOnce(new ApiError(409, '版本过期'))
    api.get.mockResolvedValueOnce({ ...scene(1, 4), content: '远端新稿' })
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot().scene?.content_revision).toBe(2)
    expect(controller.getSnapshot().conflict?.content).toBe('远端新稿')
    expect(controller.getSnapshot().draft.content).toBe('本地草稿')
    expect(await controller.save()).toBeNull()
    expect(api.update).toHaveBeenCalledTimes(1)
    controller.acknowledgeLatest()
    expect(controller.getSnapshot().scene?.content_revision).toBe(4)
    expect(controller.getSnapshot().draft.content).toBe('本地草稿')
    await controller.save()
    expect(api.update.mock.calls[1]).toEqual([1, expect.objectContaining({ expected_content_revision: 4, content: '本地草稿' })])
  })
  it('结构冲突即使没有正文草稿，后台刷新也不能绕过显式版本核对', async () => {
    const { api, controller } = setup()
    await controller.load()
    api.get.mockResolvedValueOnce(scene(1, 4))
    await controller.reviewLatest()
    api.list.mockResolvedValueOnce(page([scene(1, 4), scene(2, 4)]))
    api.get.mockResolvedValueOnce(scene(1, 4))
    await controller.load()
    expect(controller.getSnapshot().scene?.content_revision).toBe(2)
    expect(controller.getSnapshot().conflict?.content_revision).toBe(4)
  })
  it('另一场景任务完成后的刷新不覆盖当前草稿', async () => {
    const { api, controller } = setup()
    await controller.load(2)
    controller.change({ content: '场景二草稿' })
    api.list.mockResolvedValueOnce(page([scene(1, 3), scene(2, 3)]))
    api.get.mockResolvedValueOnce(scene(2, 3))
    await controller.load()
    expect(controller.getSnapshot().scene?.id).toBe(2)
    expect(controller.getSnapshot().draft.content).toBe('场景二草稿')
    expect(controller.getSnapshot().externalChange).toBe(true)
    expect(controller.getSnapshot().scene?.content_revision).toBe(2)
  })
  it('章默认仅变化规划版本时更新有效继承，正文草稿和内容版本仍保留', async () => {
    const { api, controller } = setup()
    await controller.load()
    controller.change({ content: '未保存正文' })
    const latest = scene()
    latest.planning_revision = 1
    latest.planning.revision = 1
    latest.planning.effective.pov_character_id = 6
    latest.planning.effective.pov_character_name = '新章默认POV'
    api.list.mockResolvedValueOnce({ ...page([latest]), planning_revision: 1 })
    api.get.mockResolvedValueOnce(latest)
    await controller.load()
    expect(controller.getSnapshot().scene?.planning.effective.pov_character_id).toBe(6)
    expect(controller.getSnapshot().draft.content).toBe('未保存正文')
    expect(controller.getSnapshot().externalChange).toBe(false)
  })
  it('保存期间的失效通知延后读取，不丢弃保存结果或留下saving锁', async () => {
    const { api, controller } = setup()
    await controller.load()
    controller.change({ content: '新稿' })
    let resolve!: (value: Scene) => void
    api.update.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const pending = controller.save()
    expect(await controller.load()).toBe(false)
    api.list.mockResolvedValueOnce(page([scene(1, 3), scene(2, 3)]))
    api.get.mockResolvedValueOnce({ ...scene(1, 3), content: '新稿' })
    resolve({ ...scene(1, 3), content: '新稿' })
    await pending
    await vi.waitFor(() => expect(controller.getSnapshot().loading).toBe(false))
    expect(controller.getSnapshot().saving).toBe(false)
    expect(controller.getSnapshot().draft.content).toBe('新稿')
  })
  it('卸载或撤权后的延迟响应不回填', async () => {
    const { api, controller } = setup()
    let resolve!: (value: ScenePage) => void
    api.list.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const pending = controller.load()
    controller.deactivate()
    resolve(page([scene()]))
    await pending
    expect(controller.getSnapshot().scene).toBeNull()
    expect(api.get).not.toHaveBeenCalled()
  })
  it('拒绝错误章节响应与目录/正文不同版本', async () => {
    const { api, controller } = setup()
    api.get.mockResolvedValueOnce({ ...scene(), chapter_id: 99 })
    expect(await controller.load()).toBe(false)
    expect(controller.getSnapshot().scene).toBeNull()
    api.get.mockResolvedValueOnce(scene(1, 3))
    expect(await controller.load()).toBe(false)
    expect(controller.getSnapshot().error).toContain('版本已变化')
  })
  it('失效深链不改认同序号场景，当前退场时仍保留草稿', async () => {
    const { api, controller } = setup()
    expect(await controller.load(99)).toBe(false)
    expect(controller.getSnapshot().scene).toBeNull()
    await controller.load(1)
    controller.change({ content: '退场前未保存稿' })
    api.list.mockResolvedValueOnce(page([scene(2, 3)]))
    expect(await controller.load()).toBe(false)
    expect(controller.getSnapshot().scene?.id).toBe(1)
    expect(controller.getSnapshot().draft.content).toBe('退场前未保存稿')
    expect(controller.getSnapshot().error).toContain('不会自动')
  })
  it('授权版本变化但仍可编辑时保留草稿，作废旧读取结果', async () => {
    const { api, controller } = setup()
    await controller.load()
    controller.change({ title: '未保存标题', content: '未保存正文' })
    let resolve!: (value: ScenePage) => void
    api.list.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const stale = controller.load()
    controller.authorizationChanged()
    resolve(page([scene(1, 4)]))
    await stale
    expect(controller.getSnapshot().draft).toMatchObject({ title: '未保存标题', content: '未保存正文' })
    expect(controller.getSnapshot().scene?.content_revision).toBe(2)
    expect(controller.getSnapshot().saving).toBe(false)
    expect(api.update).not.toHaveBeenCalled()
  })
  it('保存途中更改授权时保留草稿、等待旧请求落定，并显式核对后才允许再次保存', async () => {
    const { api, controller } = setup()
    await controller.load()
    controller.change({ content: '本地尚需核对' })
    let resolve!: (value: Scene) => void
    api.update.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const pending = controller.save()
    controller.authorizationChanged()
    expect(controller.getSnapshot().saving).toBe(true)
    expect(controller.getSnapshot().draft.content).toBe('本地尚需核对')
    await controller.reviewLatest()
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(await controller.save()).toBeNull()
    resolve({ ...scene(1, 3), content: '服务器已提交的内容' })
    expect(await pending).toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ saving: false, externalChange: true,
      draft: { content: '本地尚需核对' } })
    expect(api.update).toHaveBeenCalledTimes(1)
    await controller.load() // 后台目录刷新即使看到相同版本，也不能代替人工核对。
    expect(controller.getSnapshot().externalChange).toBe(true)
    expect(await controller.save()).toBeNull()
    api.get.mockResolvedValueOnce({ ...scene(1, 3), content: '服务器已提交的内容' })
    await controller.reviewLatest()
    expect(controller.getSnapshot().conflict?.content).toBe('服务器已提交的内容')
    controller.acknowledgeLatest()
    expect(controller.getSnapshot().draft.content).toBe('本地尚需核对')
    await controller.save()
    expect(api.update).toHaveBeenCalledTimes(2)
    expect(api.update.mock.calls[1]).toEqual([1, expect.objectContaining({ expected_content_revision: 3, content: '本地尚需核对' })])
  })
  it('降为只读时清除未提交草稿；完全撤权后清除场景并忽略迟到保存', async () => {
    let read = true
    let edit = true
    const api = { list: vi.fn(async () => page([scene()])), get: vi.fn(async () => scene()), update: vi.fn() }
    const controller = new SceneDraftController(9, api, () => read, () => edit)
    controller.activate()
    await controller.load()
    controller.change({ content: '不能留给只读者的草稿' })
    edit = false
    controller.authorizationChanged()
    expect(controller.getSnapshot().draft.content).toBe('甲😀乙')
    edit = true
    controller.authorizationChanged()
    controller.change({ content: '提交前的新草稿' })
    let resolve!: (value: Scene) => void
    api.update.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const pending = controller.save()
    read = false
    edit = false
    controller.authorizationChanged()
    expect(controller.getSnapshot()).toMatchObject({ scene: null, page: null, draft: { title: '', content: '', summary: '' } })
    resolve({ ...scene(1, 3), content: '旧响应' })
    await pending
    expect(controller.getSnapshot()).toMatchObject({ scene: null, page: null, draft: { content: '' } })
  })
  it('旧保存先于界面完成授权切换时仍进入结果待核对状态，不遗留保存锁', async () => {
    let version = 1
    const api = { list: vi.fn(async () => page([scene()])), get: vi.fn(async () => scene()), update: vi.fn() }
    const controller = new SceneDraftController(9, api, () => true, () => true, () => version)
    controller.activate()
    await controller.load()
    controller.change({ content: '本地草稿' })
    let resolve!: (value: Scene) => void
    api.update.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const pending = controller.save()
    version = 2 // 新权限仍可编辑，但旧轮次立即失效；新的 UI 效果还未运行。
    resolve({ ...scene(1, 3), content: '旧响应' })
    expect(await pending).toBeNull()
    controller.authorizationChanged()
    expect(controller.getSnapshot()).toMatchObject({ saving: false, externalChange: true, draft: { content: '本地草稿' } })
    expect(await controller.save()).toBeNull()
    expect(api.update).toHaveBeenCalledTimes(1)
  })

  it('已发出的保存遇到网络失败时保留草稿，核对前不能重提', async () => {
    const { controller, api } = setup()
    await controller.load()
    controller.change({ content: '本地待核对' })
    api.update.mockRejectedValueOnce(new ApiError(0, '网络中断'))
    expect(await controller.save()).toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ saving: false, externalChange: true, draft: { content: '本地待核对' } })
    expect(await controller.save()).toBeNull()
    expect(api.update).toHaveBeenCalledTimes(1)
    await controller.reviewLatest()
    controller.acknowledgeLatest()
    await controller.save()
    expect(api.update).toHaveBeenCalledTimes(2)
  })
})
