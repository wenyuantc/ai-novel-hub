import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { settingAiVisibilityApi, SettingAiSettings } from '../../api/settingAiVisibility'
import type { SettingEntry, SettingEntryRef } from '../../api/types'
import { SettingAiSession } from './settingAiController'
import { settingAiDraft, settingAiDraftError, settingAiPatch } from './settingAiDraft'

const owner: SettingEntryRef = { kind: 'organization', id: 3 }
const target: SettingEntry = { reference: { kind: 'career', id: 3 }, type: 'career', name: '剑修', category: null }
const settings = (over: Partial<SettingAiSettings> = {}): SettingAiSettings => ({
  ai_mode: null, mention_enabled: false, mention_case_sensitive: true, mention_exclusions: ['山门外'], ai_hidden_builtin_fields: [],
  hideable_builtin_fields: ['description', 'purpose'], links: [target], ai_visibility_revision: 8, ...over,
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((yes) => { resolve = yes })
  return { promise, resolve }
}
function setup() {
  let access = { identity: 'author', canRead: true, canEdit: true }
  const get = vi.fn<typeof settingAiVisibilityApi.get>(async () => settings())
  const update = vi.fn<typeof settingAiVisibilityApi.update>(async () => settings({ ai_visibility_revision: 9 }))
  const changed = vi.fn()
  const session = new SettingAiSession(1, () => access, changed, { get, update })
  return { session, controller: session.forEntry(owner), get, update, changed,
    setAccess: (next: Partial<typeof access>) => { access = { ...access, ...next }; session.accessChanged() } }
}

describe('独立的 AI 与提及草稿', () => {
  it('只提交作者修改项，空列表清除，沿用旧规则不是可提交的档位', () => {
    const base = settings()
    const draft = { ...settingAiDraft(base), mention_enabled: true, mention_exclusions: [], links: [] }
    expect(settingAiPatch(base, draft)).toEqual({ expected_ai_visibility_revision: 8, mention_enabled: true, mention_exclusions: [], links: [] })
    expect(settingAiDraftError({ ...draft, mention_exclusions: ['😀'.repeat(80)] })).toBeNull()
    expect(settingAiDraftError({ ...draft, mention_exclusions: ['😀'.repeat(81)] })).toContain('80')
    expect(settingAiDraftError({ ...draft, mention_exclusions: [' '] })).toContain('不能为空')
    expect(settingAiDraftError({ ...draft, mention_exclusions: Array.from({ length: 21 }, (_, i) => `词${i}`) })).toContain('20')
  })

  it('保存冲突后保留原草稿，读取和采用最新版本均不自动重发', async () => {
    const { controller, get, update, changed } = setup()
    await controller.open()
    controller.edit({ ai_mode: 'never', ai_hidden_builtin_fields: ['description'] })
    update.mockRejectedValueOnce(new ApiError(409, '其他人已保存', { code: 'stale_revision' }))
    await controller.save()
    expect(controller.getSnapshot()).toMatchObject({ draft: { ai_mode: 'never' }, base: { ai_visibility_revision: 8 }, reviewRequired: true })
    await controller.save()
    expect(update).toHaveBeenCalledTimes(1)

    get.mockResolvedValueOnce(settings({ ai_visibility_revision: 9, mention_enabled: true, mention_exclusions: [] }))
    await controller.refresh()
    expect(controller.getSnapshot().latest?.ai_visibility_revision).toBe(9)
    controller.acceptLatest(true)
    expect(update).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().draft).toMatchObject({ ai_mode: 'never', mention_enabled: true, mention_exclusions: [] })
    await controller.save()
    expect(update.mock.calls[1][2]).toEqual({ expected_ai_visibility_revision: 9, ai_mode: 'never', ai_hidden_builtin_fields: ['description'] })
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('冲突对照合并逐项变更，不撤销另一会话新增的隐藏字段或排除词', async () => {
    const { controller, get, update } = setup()
    get.mockResolvedValueOnce(settings({ ai_hidden_builtin_fields: ['description'] }))
    await controller.open()
    controller.edit({ ai_hidden_builtin_fields: [], mention_exclusions: [] })
    update.mockRejectedValueOnce(new ApiError(409, '其他人已保存', { code: 'stale_revision' }))
    await controller.save()
    get.mockResolvedValueOnce(settings({ ai_visibility_revision: 9,
      ai_hidden_builtin_fields: ['description', 'purpose'], mention_exclusions: ['山门外', '天池旁'] }))
    await controller.refresh()
    controller.acceptLatest(true)
    expect(update).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().draft).toMatchObject({ ai_hidden_builtin_fields: ['purpose'], mention_exclusions: ['天池旁'] })
    await controller.save()
    expect(update.mock.calls[1][2]).toEqual({ expected_ai_visibility_revision: 9,
      ai_hidden_builtin_fields: ['purpose'], mention_exclusions: ['天池旁'] })
  })

  it('关闭详情不丢草稿，另一同 ID 类型不共享旧草稿', async () => {
    const { session, controller } = setup()
    await controller.open()
    controller.edit({ ai_mode: 'never' })
    controller.close()
    const other = session.forEntry(target.reference)
    await other.open()
    expect(other.getSnapshot().draft?.ai_mode).toBeNull()
    await controller.open()
    expect(controller.getSnapshot().draft?.ai_mode).toBe('never')
  })

  it('读取遭遇并发修改也先保护已编辑草稿，不允许旧版本直送', async () => {
    const { controller, get, update } = setup()
    await controller.open()
    controller.edit({ ai_mode: 'manual_only' })
    get.mockRejectedValueOnce(new ApiError(409, '读取时版本已变化', { code: 'stale_revision' }))
    await controller.refresh()
    await controller.save()
    expect(update).not.toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: true, draft: { ai_mode: 'manual_only' } })
  })

  it('撤权时中断在途写入、保留只读草稿，随后无权读取会清除内容', async () => {
    const { controller, get, update, setAccess, changed } = setup()
    await controller.open()
    controller.edit({ ai_mode: 'never' })
    const write = deferred<SettingAiSettings>()
    update.mockReturnValueOnce(write.promise)
    const saving = controller.save()
    get.mockResolvedValueOnce(settings())
    setAccess({ identity: 'read-only', canEdit: false })
    write.resolve(settings({ ai_mode: 'never', ai_visibility_revision: 9 }))
    await saving
    expect(update.mock.calls[0][3]?.aborted).toBe(true)
    expect(controller.getSnapshot().draft?.ai_mode).toBe('never')
    expect(controller.getSnapshot().base?.ai_visibility_revision).toBe(8)
    expect(controller.getSnapshot().reviewRequired).toBe(true)
    expect(changed).not.toHaveBeenCalled()
    await controller.save()
    expect(update).toHaveBeenCalledTimes(1)
    setAccess({ identity: 'revoked', canRead: false })
    expect(controller.getSnapshot()).toMatchObject({ base: null, draft: null })
  })

  it('读取失败可以重试；晚到的旧读取不能覆盖新响应', async () => {
    const { controller, get } = setup()
    get.mockRejectedValueOnce(new ApiError(503, '读取失败'))
    await controller.open()
    expect(controller.getSnapshot()).toMatchObject({ error: '读取失败', base: null })
    const prior = deferred<SettingAiSettings>()
    get.mockReturnValueOnce(prior.promise).mockResolvedValueOnce(settings({ ai_visibility_revision: 11 }))
    const stale = controller.refresh()
    await controller.refresh()
    prior.resolve(settings({ ai_visibility_revision: 10 }))
    await stale
    expect(controller.getSnapshot()).toMatchObject({ error: null, base: { ai_visibility_revision: 11 } })
  })
})
