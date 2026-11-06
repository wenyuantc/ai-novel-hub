import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { SettingEntryRef, SettingTagsOut } from '../../api/types'
import { SettingTagsSession, type SettingTagsAccess } from './settingTagsController'

const entry: SettingEntryRef = { kind: 'character', id: 7 }
const snapshot = (revision = 1, tags = ['原标签'], ref = entry): SettingTagsOut => ({
  categories_revision: revision, setting_tags: tags,
  entry: { reference: ref, name: '阿青', type: 'character', category: null },
})
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }
function setup() {
  let access: SettingTagsAccess = { identity: 'actor:1:auth:1', canRead: true, canEdit: true }
  const api = {
    getTags: vi.fn<(pid: number, ref: SettingEntryRef, signal?: AbortSignal) => Promise<SettingTagsOut>>(async (_pid, ref) => snapshot(1, ['原标签'], ref)),
    updateTags: vi.fn<(pid: number, ref: SettingEntryRef, body: { setting_tags: string[]; expected_categories_revision: number }, signal?: AbortSignal) => Promise<SettingTagsOut>>(async (_pid, ref, body) => snapshot(body.expected_categories_revision + 1, body.setting_tags, ref)),
    suggestTags: vi.fn(async () => ({ categories_revision: 1, items: ['建议标签'], has_more: false })),
  }
  const changed = vi.fn()
  const session = new SettingTagsSession(3, api, () => access, changed)
  const controller = session.get(entry)
  return { session, controller, api, changed, setAccess: (patch: Partial<SettingTagsAccess>) => { access = { ...access, ...patch } } }
}

describe('条目整理标签的草稿和请求所有者', () => {
  it.each(['character', 'world', 'organization', 'career'] as const)('删除%s后清除缓存与在途读取，同ID新条目不继承草稿', async (kind) => {
    const { session, api, changed } = setup()
    const ref = { kind, id: 7 }
    const removed = session.get(ref)
    api.getTags.mockResolvedValueOnce(snapshot(1, [], ref))
    await removed.open()
    removed.edit(['旧条目未保存标签'])
    removed.setSearch('旧搜索')
    const other = session.get({ kind, id: 8 })
    await other.open()
    other.edit(['另一条目的草稿'])
    other.close()
    const pending = deferred<SettingTagsOut>()
    api.getTags.mockReturnValueOnce(pending.promise)
    const reading = removed.refresh()
    const signal = api.getTags.mock.calls.at(-1)?.[2]
    session.forget(ref)
    expect(signal?.aborted).toBe(true)
    expect(removed.getSnapshot()).toMatchObject({ base: null, original: null, draft: [], search: '' })
    expect(other.getSnapshot().draft).toEqual(['另一条目的草稿'])
    pending.resolve(snapshot(9, ['迟到的旧标签'], ref))
    await reading
    await removed.save()
    expect(api.updateTags).not.toHaveBeenCalled()
    const recreated = session.get(ref)
    expect(recreated).not.toBe(removed)
    api.getTags.mockResolvedValueOnce(snapshot(1, [], ref))
    await recreated.open()
    expect(recreated.getSnapshot()).toMatchObject({ base: { categories_revision: 1 }, draft: [], search: '' })
    recreated.edit(['新条目标签'])
    await recreated.save()
    expect(api.updateTags.mock.calls[0]?.[2]).toEqual({ setting_tags: ['新条目标签'], expected_categories_revision: 1 })
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it.each(['refresh', 'compare'] as const)('%s确认条目已不存在时撤下旧标签与草稿并禁止保存', async (method) => {
    const { controller, api } = setup()
    await controller.open()
    controller.edit(['已删除条目的草稿'])
    controller.setSearch('旧搜索')
    api.getTags.mockRejectedValueOnce(new ApiError(404, '条目不存在'))
    await controller[method]()
    expect(controller.getSnapshot()).toMatchObject({ base: null, original: null, latest: null, draft: [], search: '', error: '条目不存在' })
    await controller.save()
    expect(api.updateTags).not.toHaveBeenCalled()
  })

  it('清空只写 [] 和原 categories 版本，不混外层名称、基础字段或职业事实', async () => {
    const { controller, api, changed } = setup()
    await controller.open()
    controller.edit([])
    await controller.save()
    expect(api.updateTags.mock.calls[0]?.slice(0, 3)).toEqual([3, entry, { setting_tags: [], expected_categories_revision: 1 }])
    expect(changed).toHaveBeenCalledWith(entry)
    expect(controller.getSnapshot()).toMatchObject({ base: { categories_revision: 2, setting_tags: [] }, draft: [] })
  })

  it('刷新期间继续输入，迟到元数据不覆盖草稿、原快照和保存版本', async () => {
    const { controller, api } = setup()
    await controller.open()
    const pending = deferred<SettingTagsOut>()
    api.getTags.mockReturnValueOnce(pending.promise)
    const reading = controller.refresh()
    controller.edit(['我正在输入'])
    pending.resolve(snapshot(4, ['别人的修改']))
    await reading
    expect(controller.getSnapshot()).toMatchObject({ draft: ['我正在输入'], base: { categories_revision: 1 }, original: { setting_tags: ['原标签'] }, latest: { categories_revision: 4 }, reviewRequired: true })
  })

  it('409 保留原稿和版本；读取对照也不改版本，明确接受后才允许重提', async () => {
    const { controller, api } = setup()
    await controller.open()
    controller.edit(['我的标签'])
    api.updateTags.mockRejectedValueOnce(new ApiError(409, '整理配置已有变化', { code: 'stale_revision', scope: 'categories' }))
    await controller.save()
    await controller.save()
    expect(api.updateTags).toHaveBeenCalledTimes(1)
    api.getTags.mockResolvedValueOnce(snapshot(5, ['对方标签']))
    await controller.compare()
    expect(controller.getSnapshot()).toMatchObject({ draft: ['我的标签'], base: { categories_revision: 1 }, original: { setting_tags: ['原标签'] }, latest: { categories_revision: 5 }, reviewRequired: true, reviewed: true })
    await controller.save()
    expect(api.updateTags).toHaveBeenCalledTimes(1)
    controller.acceptComparison()
    await controller.save()
    expect(api.updateTags.mock.calls[1]?.[2]).toEqual({ setting_tags: ['我的标签'], expected_categories_revision: 5 })
  })

  it('业务冲突与过期版本分开；请求结果未知必须先对照，确认已保存时不再重发', async () => {
    const { controller, api } = setup()
    await controller.open()
    controller.edit(['我的标签'])
    api.updateTags.mockRejectedValueOnce(new ApiError(409, '其他业务约束', { code: 'dependency' }))
    await controller.save()
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: false, draft: ['我的标签'] })
    api.updateTags.mockRejectedValueOnce(new ApiError(0, '连接中断'))
    await controller.save()
    expect(controller.getSnapshot().reviewRequired).toBe(true)
    api.getTags.mockResolvedValueOnce(snapshot(2, ['我的标签']))
    await controller.compare()
    controller.acceptComparison()
    await controller.save()
    expect(api.updateTags).toHaveBeenCalledTimes(2)
  })

  it('关闭重开以及 A-B-A 切换都保留各自标签草稿和原版本', async () => {
    const { session, controller } = setup()
    await controller.open()
    controller.edit(['A 的草稿'])
    controller.setSearch('尚未按回车')
    controller.close()
    expect(controller.getSnapshot().reviewRequired).toBe(false)
    const other = session.get({ kind: 'world', id: 7 })
    await other.open()
    other.edit(['B 的草稿'])
    other.close()
    expect(session.get(entry)).toBe(controller)
    await controller.open()
    expect(controller.getSnapshot()).toMatchObject({ draft: ['A 的草稿'], search: '尚未按回车', base: { categories_revision: 1 }, original: { setting_tags: ['原标签'] } })
    expect(other.getSnapshot().draft).toEqual(['B 的草稿'])
    session.deactivate()
    expect(controller.getSnapshot()).toMatchObject({ base: null, draft: [], search: '' })
    expect(other.getSnapshot().draft).toEqual([])
    const nextProject = new SettingTagsSession(4)
    expect(nextProject.get(entry).getSnapshot().base).toBeNull()
  })

  it('建议有界且旧搜索不能回填，建议失败仍能保存合法自定义标签', async () => {
    const { controller, api } = setup()
    await controller.open()
    const pending = deferred<{ categories_revision: number; items: string[]; has_more: boolean }>()
    api.suggestTags.mockReturnValueOnce(pending.promise)
    controller.setSearch('旧')
    const old = controller.suggest()
    controller.setSearch('新')
    pending.resolve({ categories_revision: 1, items: ['旧候选'], has_more: false })
    await old
    expect(controller.getSnapshot().suggestions).toEqual([])
    api.suggestTags.mockRejectedValueOnce(new Error('建议加载失败'))
    await controller.suggest()
    expect(controller.getSnapshot().suggestionError).toContain('建议加载失败')
    await controller.save()
    expect(api.updateTags.mock.calls[0]?.[2]).toEqual({ setting_tags: ['原标签', '新'], expected_categories_revision: 1 })
    expect(api.suggestTags).toHaveBeenCalledWith(3, { q: '新', limit: 20 }, expect.any(AbortSignal))
  })

  it('actor/授权代次变化使旧读写失效；失读撤下内容，失写不能提交', async () => {
    const { session, controller, api, changed, setAccess } = setup()
    await controller.open()
    controller.edit(['尚未保存'])
    const pending = deferred<SettingTagsOut>()
    api.updateTags.mockReturnValueOnce(pending.promise)
    const saving = controller.save()
    setAccess({ identity: 'actor:1:auth:2', canEdit: false })
    session.accessChanged()
    pending.resolve(snapshot(8, ['旧请求结果']))
    await saving
    expect(controller.getSnapshot()).toMatchObject({ draft: ['尚未保存'], base: { categories_revision: 1 }, reviewRequired: true })
    expect(changed).not.toHaveBeenCalled()
    await controller.save()
    expect(api.updateTags).toHaveBeenCalledTimes(1)
    const reading = deferred<SettingTagsOut>()
    api.getTags.mockReturnValueOnce(reading.promise)
    const read = controller.refresh()
    setAccess({ identity: 'actor:2:auth:1', canRead: false })
    session.accessChanged()
    reading.resolve(snapshot(9, ['不可见数据']))
    await read
    expect(controller.getSnapshot()).toMatchObject({ base: null, original: null, latest: null, draft: [], suggestions: [] })
  })

  it('关闭后的迟到读写不回填；保存中关闭标记结果待核对并保留原稿', async () => {
    const { controller, api, changed } = setup()
    const pendingRead = deferred<SettingTagsOut>()
    api.getTags.mockReturnValueOnce(pendingRead.promise)
    const opening = controller.open()
    controller.close()
    pendingRead.resolve(snapshot())
    await opening
    expect(controller.getSnapshot().base).toBeNull()
    await controller.open()
    controller.edit(['关闭前草稿'])
    const pendingSave = deferred<SettingTagsOut>()
    api.updateTags.mockReturnValueOnce(pendingSave.promise)
    const saving = controller.save()
    controller.close()
    pendingSave.resolve(snapshot(2, ['关闭前草稿']))
    await saving
    expect(changed).not.toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ draft: ['关闭前草稿'], base: { categories_revision: 1 }, reviewRequired: true })
  })

  it('初次读取失败可重试，后续读取失败不清空已有标签和输入', async () => {
    const { controller, api } = setup()
    api.getTags.mockRejectedValueOnce(new ApiError(503, '暂时无法读取'))
    await controller.open()
    expect(controller.getSnapshot()).toMatchObject({ base: null, loading: false, error: '暂时无法读取' })
    await controller.refresh()
    controller.edit(['保留草稿'])
    controller.setSearch('尚未提交的输入')
    api.getTags.mockRejectedValueOnce(new ApiError(503, '重新读取失败'))
    await controller.refresh()
    expect(controller.getSnapshot()).toMatchObject({ draft: ['保留草稿'], search: '尚未提交的输入', base: { categories_revision: 1 }, loading: false, error: '重新读取失败' })
  })

  it('元数据广播保留原草稿与版本，旧读取不能覆盖保存后的新版本', async () => {
    const { session, controller, api } = setup()
    await controller.open()
    controller.edit(['草稿'])
    api.getTags.mockResolvedValueOnce(snapshot(3, ['最新标签']))
    session.refresh()
    await Promise.resolve()
    expect(controller.getSnapshot()).toMatchObject({ draft: ['草稿'], base: { categories_revision: 1 }, original: { categories_revision: 1 }, latest: { categories_revision: 3 }, reviewRequired: true })
    api.getTags.mockResolvedValueOnce(snapshot(3, ['最新标签']))
    await controller.compare()
    controller.acceptComparison()
    const reading = deferred<SettingTagsOut>()
    api.getTags.mockReturnValueOnce(reading.promise)
    const refresh = controller.refresh()
    // 保存按钮在读取期间禁用；旧请求取消后才允许保存。
    controller.close()
    await controller.open()
    api.getTags.mockResolvedValueOnce(snapshot(3, ['最新标签']))
    await controller.compare()
    controller.acceptComparison()
    await controller.save()
    reading.resolve(snapshot(1, ['旧读取']))
    await refresh
    expect(controller.getSnapshot()).toMatchObject({ base: { categories_revision: 4, setting_tags: ['草稿'] }, draft: ['草稿'] })
  })

  it('直接失去写权限时也在提交前复查；迟到响应必须同时匹配授权代次与条目', async () => {
    const { controller, api, changed, setAccess } = setup()
    await controller.open()
    controller.edit(['保留输入'])
    setAccess({ canEdit: false })
    await controller.save()
    expect(api.updateTags).not.toHaveBeenCalled()
    expect(controller.getSnapshot().draft).toEqual(['保留输入'])
    setAccess({ canEdit: true })
    const pending = deferred<SettingTagsOut>()
    api.getTags.mockReturnValueOnce(pending.promise)
    const read = controller.refresh()
    setAccess({ identity: 'actor:2:auth:1' })
    pending.resolve(snapshot(6, ['旧身份结果']))
    await read
    expect(controller.getSnapshot()).toMatchObject({ base: { categories_revision: 1 }, draft: ['保留输入'], loading: false })
    expect(changed).not.toHaveBeenCalled()
    api.getTags.mockResolvedValueOnce(snapshot(8, ['错误目标'], { kind: 'world', id: 7 }))
    await controller.compare()
    expect(controller.getSnapshot().error).toContain('当前条目不一致')
    expect(controller.getSnapshot().base?.categories_revision).toBe(1)
  })

  it('非法标签保留在草稿中供修正，原响应与调用方数组不能反向修改原快照', async () => {
    const { controller, api } = setup()
    const initial = snapshot()
    api.getTags.mockResolvedValueOnce(initial)
    await controller.open()
    initial.setting_tags.push('外部数组突变')
    const invalid = ['😀'.repeat(51)]
    controller.edit(invalid)
    invalid.push('外部输入突变')
    await controller.save()
    expect(api.updateTags).not.toHaveBeenCalled()
    expect(controller.getSnapshot().draft).toEqual(['😀'.repeat(51)])
    expect(controller.getSnapshot().draftError).toContain('50')
    expect(controller.getSnapshot().original?.setting_tags).toEqual(['原标签'])
    controller.edit([' 修正 ', '修正'])
    await controller.save()
    expect(api.updateTags.mock.calls[0]?.[2]).toEqual({ setting_tags: ['修正'], expected_categories_revision: 1 })
  })

  it('建议查询使用规范标签且限20项，超长输入不发请求并保留供修正', async () => {
    const { controller, api } = setup()
    await controller.open()
    controller.setSearch('  门派  ')
    await controller.suggest()
    expect(api.suggestTags).toHaveBeenCalledWith(3, { q: '门派', limit: 20 }, expect.any(AbortSignal))
    controller.setSearch('😀'.repeat(51))
    await controller.suggest()
    await controller.save()
    expect(api.suggestTags).toHaveBeenCalledTimes(1)
    expect(api.updateTags).not.toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ search: '😀'.repeat(51), draftError: '每个整理标签不能超过 50 字' })
    controller.setSearch('有效标签')
    await controller.save()
    expect(api.updateTags.mock.calls[0]?.[2]).toEqual({ setting_tags: ['原标签', '有效标签'], expected_categories_revision: 1 })
  })
})
