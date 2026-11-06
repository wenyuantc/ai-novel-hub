import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { cardApi } from '../../api/cards'
import type { CardImportResult, CardPreview } from '../../api/types'
import { CardImportController } from './cardImportController'

const source = { format: 'ainovel.cards', version: 4, characters: [{ ref: 'local', name: '林远', fields: [] }], organizations: [], careers: [] }
const file = (data: unknown = source) => ({ name: '设定卡片.json', size: 100, text: async () => JSON.stringify(data) })
const preview = (over: Partial<CardPreview> = {}): CardPreview => ({ fields_revision: 2, categories_revision: 0, can_import: true, field_definitions: [], references: [], characters: [{ name: '林远', status: 'new', rename_to: null }], organizations: [], careers: [], relationships: [], warnings: [], ...over })
const result: CardImportResult = { characters: [], organizations: [], careers: [], relationships_created: 0, members_created: 0, careers_assigned: 0, warnings: [] }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }
function setup(initial = preview()) {
  let allowed = true
  const api = { ...cardApi, preview: vi.fn<typeof cardApi.preview>(async () => initial), import: vi.fn<typeof cardApi.import>(async () => result) }
  const imported = vi.fn()
  const controller = new CardImportController(1, api, () => allowed, imported)
  controller.activate()
  return { controller, api, imported, deny: () => { allowed = false } }
}

describe('卡片预览和确认的代次与显式核对', () => {
  it('v5用原预览双版本确认，分类冲突保留输入/映射/双版本，明确采用候选后才可重试', async () => {
    const { controller, api } = setup(preview({ categories_revision: 6 }))
    await controller.selectFile(file({ ...source, version: 5, characters: [{ ...source.characters[0], setting_tags: ['同盟'] }] }))
    api.import.mockRejectedValueOnce(new ApiError(409, '标签已修改', { code: 'stale_revision', scope: 'categories' }))
    await controller.confirm()
    expect(api.import.mock.calls[0][3]).toMatchObject({ expected_fields_revision: 2, expected_categories_revision: 6 })
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: true, preview: { fields_revision: 2, categories_revision: 6 }, metadata: { version: 5 } })
    api.preview.mockResolvedValueOnce(preview({ fields_revision: 7, categories_revision: 10 }))
    await controller.refreshPreview()
    expect(controller.getSnapshot()).toMatchObject({ preview: { fields_revision: 2, categories_revision: 6 }, reviewPreview: { fields_revision: 7, categories_revision: 10 } })
    await controller.confirm(); expect(api.import).toHaveBeenCalledTimes(1)
    controller.acceptReview(); await controller.confirm()
    expect(api.import.mock.calls[1][3]).toMatchObject({ expected_fields_revision: 7, expected_categories_revision: 10 })
  })

  it('v5映射或冲突选择改变后同样必须更新预览；v4确认不强加分类版本', async () => {
    const { controller, api } = setup(preview({ categories_revision: 8 }))
    await controller.selectFile(file({ ...source, version: 5 }))
    controller.setReference('outside', { reference: { kind: 'world', id: 7 }, type: 'world.place', name: '青山', category: 'place' })
    await controller.confirm(); expect(api.import).not.toHaveBeenCalled()
    await controller.refreshPreview(); await controller.confirm()
    expect(api.import.mock.calls[0][3]).toMatchObject({ expected_categories_revision: 8 })
    const old = setup(preview({ categories_revision: 8 })); await old.controller.selectFile(file()); await old.controller.confirm()
    expect(old.api.import.mock.calls[0][3]).not.toHaveProperty('expected_categories_revision')
  })

  it.each(['fields_revision', 'categories_revision'] as const)('v5主动更新时%s变化也仅生成对照，不自动采用新保存版本', async (field) => {
    const { controller, api } = setup(preview({ categories_revision: 6 }))
    await controller.selectFile(file({ ...source, version: 5 }))
    api.preview.mockResolvedValueOnce(preview({ categories_revision: 6, [field]: 20 }))
    await controller.refreshPreview()
    expect(controller.getSnapshot()).toMatchObject({ preview: { fields_revision: 2, categories_revision: 6 }, reviewPreview: { [field]: 20 }, reviewRequired: true })
    await controller.confirm(); expect(api.import).not.toHaveBeenCalled()
    controller.acceptReview(); await controller.confirm()
    expect(api.import.mock.calls[0][3]).toHaveProperty(field === 'fields_revision' ? 'expected_fields_revision' : 'expected_categories_revision', 20)
  })
  it('默认skip不解析typed引用，选择后必须重新预览且确认携带原预览目录版本', async () => {
    const { controller, api } = setup(preview({ can_import: false, references: [{ ref: 'outside', kind: 'world', name: '山门', status: 'unresolved', target: null, reason: '请选择具体目标', source_fields: ['林远 / 驻地'] }] }))
    await controller.selectFile(file())
    await controller.confirm()
    expect(api.import).not.toHaveBeenCalled()
    controller.setReference('outside', { reference: { kind: 'world', id: 7 }, type: 'world.place', name: '青山', category: 'place' })
    await controller.confirm()
    expect(api.import).not.toHaveBeenCalled()
    api.preview.mockResolvedValueOnce(preview({ fields_revision: 5, references: [{ ref: 'outside', kind: 'world', name: '山门', status: 'mapped', target: { kind: 'world', id: 7 }, reason: null, source_fields: ['林远 / 驻地'] }] }))
    await controller.refreshPreview()
    await controller.confirm()
    expect(api.import).toHaveBeenCalledWith(1, source, expect.any(Object), { expected_fields_revision: 5, mappings: { references: { outside: { kind: 'world', id: 7 } }, definitions: {} } }, expect.any(AbortSignal))
  })

  it('409保留输入与原revision，重新预览只产生候选，显式比较采纳后才能确认', async () => {
    const { controller, api } = setup()
    await controller.selectFile(file())
    api.import.mockRejectedValueOnce(new ApiError(409, '字段目录已更新', { code: 'stale_revision', scope: 'fields' }))
    await controller.confirm()
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: true, preview: { fields_revision: 2 }, fileName: '设定卡片.json' })
    api.preview.mockResolvedValueOnce(preview({ fields_revision: 8 }))
    await controller.refreshPreview()
    expect(controller.getSnapshot()).toMatchObject({ preview: { fields_revision: 2 }, reviewPreview: { fields_revision: 8 }, reviewRequired: true })
    await controller.confirm()
    expect(api.import).toHaveBeenCalledTimes(1)
    controller.acceptReview()
    await controller.confirm()
    expect(api.import.mock.calls[1][3]).toMatchObject({ expected_fields_revision: 8 })
  })

  it('新文件立即清除旧映射；慢文件读取和迟到预览不能覆盖后来选择', async () => {
    const { controller, api } = setup()
    await controller.selectFile(file())
    controller.setReference('outside', { reference: { kind: 'world', id: 7 }, type: 'world.place', name: '青山', category: 'place' })
    const slow = deferred<string>()
    const old = controller.selectFile({ name: '慢文件.json', size: 100, text: () => slow.promise })
    expect(controller.getSnapshot()).toMatchObject({ preview: null, mappings: { references: {}, definitions: {} } })
    await controller.selectFile({ ...file(), name: '新文件.json' })
    slow.resolve(JSON.stringify({ ...source, characters: [] }))
    await old
    expect(controller.getSnapshot().fileName).toBe('新文件.json')
    expect(api.preview).toHaveBeenCalledTimes(2)
  })

  it('项目失效保留映射但禁止直接重提；改为包内rename会清除多余显式映射', async () => {
    const { controller, api } = setup(preview({ characters: [{ name: '林远', status: 'conflict', rename_to: '林远（导入）' }] }))
    await controller.selectFile(file())
    controller.setReference('local', { reference: { kind: 'character', id: 7 }, type: 'character', name: '林远', category: null })
    controller.invalidate()
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: true, mappings: { references: { local: { kind: 'character', id: 7 } } }, preview: { fields_revision: 2 } })
    controller.setResolution('characters', '林远', 'rename')
    expect(controller.getSnapshot().mappings.references).toEqual({})
    await controller.confirm()
    expect(api.import).not.toHaveBeenCalled()
  })

  it('权限和卸载阻止所有迟到结果及回调；旧版重命名保留兼容行为', async () => {
    const { controller, api, imported, deny } = setup()
    await controller.selectFile(file({ ...source, version: 3 }))
    controller.setResolution('characters', '林远', 'rename')
    const pending = deferred<CardImportResult>()
    api.import.mockReturnValueOnce(pending.promise)
    const saving = controller.confirm()
    deny()
    pending.resolve(result)
    await saving
    expect(imported).not.toHaveBeenCalled()
    expect(controller.getSnapshot().preview).toBeNull()
    controller.deactivate()
  })

  it('不同文件的晚到服务端预览、首次预览时的项目失效和卸载均不回填', async () => {
    const { controller, api } = setup()
    const late = deferred<CardPreview>()
    api.preview.mockReturnValueOnce(late.promise)
    const first = controller.selectFile({ ...file(), name: '旧文件.json' })
    await Promise.resolve()
    await controller.selectFile({ ...file(), name: '新文件.json' })
    late.resolve(preview({ fields_revision: 99 }))
    await first
    expect(controller.getSnapshot()).toMatchObject({ fileName: '新文件.json', preview: { fields_revision: 2 } })
    const pending = deferred<CardPreview>()
    api.preview.mockReturnValueOnce(pending.promise)
    const reading = controller.selectFile(file())
    await Promise.resolve()
    controller.invalidate()
    pending.resolve(preview({ fields_revision: 100 }))
    await reading
    expect(controller.getSnapshot().preview).toBeNull()
    const afterClose = deferred<CardPreview>()
    api.preview.mockReturnValueOnce(afterClose.promise)
    const closing = controller.selectFile(file())
    await Promise.resolve()
    controller.deactivate()
    afterClose.resolve(preview())
    await closing
    expect(controller.getSnapshot().preview).toBeNull()
  })
})
