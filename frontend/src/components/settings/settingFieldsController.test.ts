import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { settingFieldsApi } from '../../api/settingFields'
import type { SettingFieldDefinition, SettingFieldDefinitionList, SettingFieldDefinitionMutation } from '../../api/types'
import { SettingFieldsController } from './settingFieldsController'

const field = (over: Partial<SettingFieldDefinition> = {}): SettingFieldDefinition => ({ id: 4, project_id: 1, label: '境界', field_type: 'dropdown', applicable_types: ['character'], reference_types: [], sort: 0, archived: false, options: [{ id: 8, definition_id: 4, label: '练气', sort: 0, archived: false }], ...over })
const page = (revision = 1, item = field()): SettingFieldDefinitionList => ({ fields_revision: revision, items: [item], total: 1 })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }
function setup() {
  let readable = true
  let writable = true
  const api = { ...settingFieldsApi, listDefinitions: vi.fn(async () => page()), getDefinition: vi.fn(async () => ({ item: field({ label: '最新境界' }), fields_revision: 3 })),
    updateDefinition: vi.fn(async () => ({ item: field({ label: '新境界' }), fields_revision: 2 })),
    updateOption: vi.fn(async () => ({ item: { id: 8, definition_id: 4, label: '筑基', sort: 0, archived: false }, fields_revision: 2 })),
    removeDefinition: vi.fn(async () => ({ fields_revision: 2 })), removeOption: vi.fn(async () => ({ fields_revision: 2 })),
  }
  const changed = vi.fn()
  const controller = new SettingFieldsController(1, api, () => readable, () => writable, changed)
  controller.activate()
  return { controller, api, changed, denyReads: () => { readable = false }, denyWrites: () => { writable = false } }
}

describe('作品字段配置请求与草稿所有者', () => {
  it('409后保留草稿、原快照和原版本，刷新不会重提或更新保存版本', async () => {
    const { controller, api } = setup()
    await controller.refresh()
    controller.openDefinition(field())
    controller.editDefinition({ label: '我的境界' })
    api.updateDefinition.mockRejectedValueOnce(new ApiError(409, '目录已更新', { code: 'stale_revision', scope: 'fields' }))
    await controller.save()
    api.listDefinitions.mockResolvedValueOnce(page(3, field({ label: '其他人修改' })))
    await controller.refresh()
    const editor = controller.getSnapshot().editor
    expect(editor).toMatchObject({ expectedRevision: 1, reviewRequired: true, original: { label: '境界' }, draft: { label: '我的境界' } })
    await controller.save()
    expect(api.updateDefinition).toHaveBeenCalledTimes(1)
    api.listDefinitions.mockResolvedValueOnce(page(3, field({ label: '最新境界' })))
    await controller.review()
    expect(controller.getSnapshot().editor).toMatchObject({ expectedRevision: 1, original: { label: '境界' }, draft: { label: '我的境界' }, reviewed: { fields_revision: 3 } })
    controller.useReviewed()
    expect(controller.getSnapshot().editor).toMatchObject({ expectedRevision: 3, reviewRequired: false, draft: { label: '最新境界' } })
    expect(controller.getSnapshot().data?.fields_revision).toBe(3)
  })

  it('依赖冲突与版本冲突分开，缩小适用范围失败仍能修正原草稿', async () => {
    const { controller, api } = setup()
    await controller.refresh()
    controller.openDefinition(field())
    controller.editDefinition({ applicable_types: ['organization'] })
    api.updateDefinition.mockRejectedValueOnce(new ApiError(409, '已有角色使用该字段', { code: 'incompatible_field_scope' }))
    await controller.save()
    expect(controller.getSnapshot().editor).toMatchObject({ reviewRequired: false, expectedRevision: 1, draft: { applicable_types: ['organization'] } })
    expect(controller.getSnapshot().error).toBe('已有角色使用该字段')
    controller.editDefinition({ applicable_types: ['character', 'organization'] })
    await controller.save()
    expect(api.updateDefinition).toHaveBeenCalledTimes(2)
  })

  it('后端成功前列表不变，关闭编辑仍可继续原草稿', async () => {
    const { controller, api, changed } = setup()
    await controller.refresh()
    controller.openDefinition(field())
    controller.editDefinition({ label: '新境界' })
    controller.closeEditor()
    expect(controller.getSnapshot().editor).toMatchObject({ open: false, draft: { label: '新境界' } })
    controller.resumeEditor()
    const pending = deferred<SettingFieldDefinitionMutation>()
    api.updateDefinition.mockReturnValueOnce(pending.promise)
    const saving = controller.save()
    expect(controller.getSnapshot().data?.items[0].label).toBe('境界')
    pending.resolve({ item: field({ label: '新境界' }), fields_revision: 2 })
    await saving
    expect(controller.getSnapshot().data?.items[0].label).toBe('新境界')
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('项目刷新取消迟到写回并保留原版本，随后读新目录也不能覆盖草稿', async () => {
    const { controller, api, changed } = setup()
    await controller.refresh()
    controller.openOption(field(), field().options[0])
    controller.editOption({ label: '筑基' })
    const pending = deferred<{ item: SettingFieldDefinition['options'][number]; fields_revision: number }>()
    api.updateOption.mockReturnValueOnce(pending.promise)
    const saving = controller.save()
    controller.invalidate()
    api.listDefinitions.mockResolvedValueOnce(page(4))
    await controller.refresh()
    pending.resolve({ item: field().options[0], fields_revision: 2 })
    await saving
    expect(controller.getSnapshot().editor).toMatchObject({ expectedRevision: 1, reviewRequired: true, draft: { label: '筑基' } })
    expect(controller.getSnapshot().data?.fields_revision).toBe(4)
    expect(changed).not.toHaveBeenCalled()
  })

  it('读请求代次与卸载阻止旧响应回填；实时撤写阻止保存和删除', async () => {
    const { controller, api, denyWrites } = setup()
    const stale = deferred<SettingFieldDefinitionList>()
    api.listDefinitions.mockReturnValueOnce(stale.promise)
    const first = controller.refresh()
    await controller.refresh()
    stale.resolve(page(99))
    await first
    expect(controller.getSnapshot().data?.fields_revision).toBe(1)
    controller.openDefinition(field())
    denyWrites()
    await controller.save()
    await controller.remove()
    expect(api.updateDefinition).not.toHaveBeenCalled()
    expect(api.removeDefinition).not.toHaveBeenCalled()
    const late = deferred<SettingFieldDefinitionList>()
    api.listDefinitions.mockReturnValueOnce(late.promise)
    const reading = controller.refresh()
    controller.deactivate()
    late.resolve(page(100))
    await reading
    expect(controller.getSnapshot().data?.fields_revision).toBe(1)
  })

  it('撤读立即清空内容，选项停用或名称修改按原稳定ID提交', async () => {
    const { controller, api, denyReads } = setup()
    await controller.refresh()
    controller.openOption(field(), field().options[0])
    controller.editOption({ archived: true })
    await controller.save()
    expect(api.updateOption).toHaveBeenCalledWith(1, 4, 8, { expected_fields_revision: 1, label: '练气', sort: 0, archived: true }, expect.any(AbortSignal))
    denyReads()
    await controller.refresh()
    expect(controller.getSnapshot()).toMatchObject({ data: null, editor: null })
  })

  it('核对一次完整目录再采用版本，避免其他旧字段快照配上新版本', async () => {
    const { controller, api } = setup()
    await controller.refresh()
    controller.openDefinition(field())
    controller.editDefinition({ label: '我的修改' })
    controller.invalidate()
    api.listDefinitions.mockResolvedValueOnce({ fields_revision: 5, total: 2, items: [field({ label: '最新境界' }), field({ id: 9, label: '另一字段的新名称' })] })
    await controller.review()
    expect(controller.getSnapshot().data).toMatchObject({ fields_revision: 5, items: [{ label: '最新境界' }, { label: '另一字段的新名称' }] })
    expect(controller.getSnapshot().editor).toMatchObject({ expectedRevision: 1, original: { label: '境界' }, draft: { label: '我的修改' } })
    controller.useReviewed()
    expect(controller.getSnapshot().editor?.expectedRevision).toBe(5)
  })

  it('关闭后的迟到确认不能保存；写权限中途撤销后清除忙状态并保留草稿', async () => {
    const { controller, api, denyWrites } = setup()
    await controller.refresh()
    controller.openDefinition(field())
    controller.editDefinition({ label: '新境界' })
    controller.closeEditor()
    await controller.save()
    await controller.remove()
    expect(api.updateDefinition).not.toHaveBeenCalled()
    expect(api.removeDefinition).not.toHaveBeenCalled()
    controller.resumeEditor()
    const pending = deferred<SettingFieldDefinitionMutation>()
    api.updateDefinition.mockReturnValueOnce(pending.promise)
    const saving = controller.save()
    denyWrites()
    pending.resolve({ item: field({ label: '新境界' }), fields_revision: 2 })
    await saving
    expect(controller.getSnapshot()).toMatchObject({ saving: false, editor: { reviewRequired: true, draft: { label: '新境界' } } })
  })
})
