import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { settingCategoriesApi } from '../../api/settingCategories'
import type { SettingCategory, SettingCategoryEntryPage, SettingFieldDefinitionList } from '../../api/types'
import { invalidateProject, subscribeProjectRefresh } from '../../utils/projectRefresh'
import { CategoryEditor } from './categoryEditor'

const item: SettingCategory = { id: 4, project_id: 1, name: '同盟', applicable_types: ['character'], match_mode: 'all', sort: 0, rules: [{ id: 8, category_id: 4, kind: 'tag', tag: '同盟', sort: 0 }] }
const definitions: SettingFieldDefinitionList = { items: [], total: 0, fields_revision: 2 }
const mutation = (revision = 3, name = item.name) => ({ item: { ...item, name }, categories_revision: revision, fields_revision: 2 })
const result: SettingCategoryEntryPage = { category: item, items: [], total: 0, page: 1, page_size: 20, position: null, categories_revision: 3, fields_revision: 2 }
function fixture() {
  let canRead = true; let canEdit = true; let identity = 'actor:1'
  const api = { ...settingCategoriesApi, get: vi.fn().mockResolvedValue(mutation()), list: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 1, categories_revision: 3, fields_revision: 2 }),
    update: vi.fn().mockResolvedValue(mutation(4, '新名称')), create: vi.fn().mockResolvedValue(mutation(4)), remove: vi.fn().mockResolvedValue({ categories_revision: 4, fields_revision: 2 }), preview: vi.fn().mockResolvedValue(result) }
  const changed = vi.fn()
  const metadata = vi.fn().mockResolvedValue(definitions)
  const controller = new CategoryEditor(1, api, metadata, () => canRead, () => canEdit, () => identity, changed)
  controller.activate()
  const session = () => { const state = controller.getSnapshot(); return state.activeKey ? state.sessions[state.activeKey] : null }
  return { controller, api, metadata, session, changed, read: (value: boolean) => { canRead = value }, write: (value: boolean) => { canEdit = value }, identity: (value: string) => { identity = value } }
}

describe('分类草稿与双版本保存', () => {
  it.each(['new', 'existing'] as const)('%s成功保存的自身广播不使刚保存快照过期，其他草稿与后续外部广播仍须对照', async (mode) => {
    const f = fixture()
    const stop = subscribeProjectRefresh((pid, resources) => { if (pid === 1 && resources.includes('setting-categories')) f.controller.invalidate() })
    f.controller.setChanged((id) => { f.changed(id); invalidateProject(1, ['setting-categories']) })
    try {
      f.api.get.mockResolvedValueOnce({ ...mutation(), item: { ...item, id: 9, rules: [{ ...item.rules[0], category_id: 9 }] } })
      await f.controller.open(9)
      f.controller.edit({ name: '另一分类的未保存草稿' })
      f.controller.close()
      await f.controller.open(mode === 'new' ? null : 4)
      f.controller.edit({ name: '新名称', applicable_types: ['character'], rules: [{ key: 'tag', kind: 'tag', tag: '同盟', sort: 0 }] })
      f.api.create.mockResolvedValueOnce(mutation(4, '新名称'))
      await f.controller.save()
      expect(f.session()).toMatchObject({ categoryId: 4, open: false, reviewRequired: false, base: { categories_revision: 4, fields_revision: 2 } })
      expect(f.controller.getSnapshot().sessions['category-9']).toMatchObject({ reviewRequired: true, draft: { name: '另一分类的未保存草稿' }, base: { categories_revision: 3 } })
      await f.controller.open(4)
      expect(f.session()?.reviewRequired).toBe(false)
      expect(f.controller.deleteToken()).not.toBeNull()
      f.api.preview.mockResolvedValueOnce({ ...result, categories_revision: 4 })
      await f.controller.preview({ page: 1 })
      expect(f.session()?.preview?.value.categories_revision).toBe(4)
      invalidateProject(1, ['setting-categories'])
      expect(f.session()).toMatchObject({ reviewRequired: true, base: { categories_revision: 4 }, preview: null })
      expect(f.controller.deleteToken()).toBeNull()
    } finally { stop() }
  })

  it('只改名差量PATCH带两原版本且不替换规则行；成功后才广播', async () => {
    const f = fixture(); await f.controller.open(4)
    f.controller.edit({ name: '新名称' })
    expect(f.changed).not.toHaveBeenCalled()
    await f.controller.save()
    expect(f.api.update.mock.calls[0]?.[2]).toEqual({ name: '新名称', expected_categories_revision: 3, expected_fields_revision: 2 })
    expect(f.changed).toHaveBeenCalledWith(4)
  })

  it.each(['categories', 'fields'])('%s冲突保留原草稿和两版本，读取对照也不自动采用', async (scope) => {
    const f = fixture(); await f.controller.open(4)
    f.controller.edit({ name: '我的草稿' })
    f.api.update.mockRejectedValueOnce(new ApiError(409, '版本冲突', { code: 'stale_revision', scope, current_revision: 9 }))
    await f.controller.save()
    expect(f.session()?.reviewRequired).toBe(true)
    expect(f.session()?.base).toEqual({ categories_revision: 3, fields_revision: 2 })
    f.api.get.mockResolvedValue(mutation(9, '别人改过'))
    await f.controller.review()
    expect(f.session()?.draft.name).toBe('我的草稿')
    expect(f.session()?.original?.name).toBe('同盟')
    expect(f.session()?.base?.categories_revision).toBe(3)
    await f.controller.save()
    expect(f.api.update).toHaveBeenCalledTimes(1)
    f.controller.useReview(true)
    expect(f.session()?.base?.categories_revision).toBe(9)
    expect(f.session()?.draft.name).toBe('我的草稿')
    await f.controller.save()
    expect(f.api.update.mock.calls[1]?.[2]).toMatchObject({ expected_categories_revision: 9, expected_fields_revision: 2 })
  })

  it('服务器条件索引定位到原草稿，关闭重开与新建草稿分别保留', async () => {
    const f = fixture(); await f.controller.open(4)
    f.controller.edit({ name: '我的分类' })
    f.api.update.mockRejectedValueOnce(new ApiError(422, '条件不可用', { rule_index: 0 }))
    await f.controller.save()
    expect(f.session()?.issueIndex).toBe(0)
    f.controller.close(); await f.controller.open(null)
    f.controller.edit({ name: '新建草稿' }); f.controller.close()
    await f.controller.open(4); expect(f.session()?.draft.name).toBe('我的分类')
    f.controller.close(); await f.controller.open(null); expect(f.session()?.draft.name).toBe('新建草稿')
  })

  it('删除确认必须绑定原分类/快照且确认时复查权限', async () => {
    const f = fixture(); await f.controller.open(4)
    const token = f.controller.deleteToken()
    expect(token).not.toBeNull()
    f.controller.close(); await f.controller.open(4)
    await f.controller.remove(token!)
    expect(f.api.remove).not.toHaveBeenCalled()
    const fresh = f.controller.deleteToken()!
    f.write(false)
    await f.controller.remove(fresh)
    expect(f.api.remove).not.toHaveBeenCalled()
  })

  it('权限身份切换隔离迟到预览；撤销读取清空草稿及结果', async () => {
    const f = fixture(); await f.controller.open(4)
    let resolve!: (value: SettingCategoryEntryPage) => void
    f.api.preview.mockReturnValueOnce(new Promise((ok) => { resolve = ok }))
    const pending = f.controller.preview({ page: 1, page_size: 20 })
    f.identity('actor:2'); resolve(result); await pending
    expect(f.session()?.preview).toBeNull()
    f.read(false); f.controller.invalidate()
    expect(f.controller.getSnapshot().sessions).toEqual({})
  })

  it('只读仍可预览，元数据/预览版本不同不能拼合也不能替换保存版本', async () => {
    const f = fixture(); f.write(false); await f.controller.open(4)
    await f.controller.preview({ page: 1, page_size: 20 })
    expect(f.session()?.preview?.value.total).toBe(0)
    f.api.preview.mockResolvedValueOnce({ ...result, fields_revision: 7 })
    await f.controller.preview({ page: 1 })
    expect(f.session()?.preview).toBeNull()
    expect(f.session()?.reviewRequired).toBe(true)
    expect(f.session()?.base?.fields_revision).toBe(2)
    await f.controller.save(); expect(f.api.update).not.toHaveBeenCalled()
  })

  it('分类与完整字段元数据读取版本不符时拒绝编辑，可在同版本重试恢复', async () => {
    const f = fixture(); f.metadata.mockResolvedValueOnce({ ...definitions, fields_revision: 99 })
    await f.controller.open(4)
    expect(f.session()?.base).toBeNull()
    expect(f.session()?.error).toContain('同一版本')
    await f.controller.save(); expect(f.api.update).not.toHaveBeenCalled()
    await f.controller.open(4)
    expect(f.session()?.base).toEqual({ categories_revision: 3, fields_revision: 2 })
    expect(f.api.get).toHaveBeenCalledTimes(2)
  })

  it('保存期间授权代次改变不能关闭草稿或广播旧结果，原双版本留待核对', async () => {
    const f = fixture(); await f.controller.open(4); f.controller.edit({ name: '保留草稿' })
    let resolve!: (value: ReturnType<typeof mutation>) => void
    f.api.update.mockReturnValueOnce(new Promise((ok) => { resolve = ok }))
    const saving = f.controller.save(); f.identity('actor:2'); resolve(mutation(4)); await saving
    expect(f.changed).not.toHaveBeenCalled()
    expect(f.session()).toMatchObject({ draft: { name: '保留草稿' }, open: true, saving: false, reviewRequired: true, base: { categories_revision: 3, fields_revision: 2 } })
  })
})
