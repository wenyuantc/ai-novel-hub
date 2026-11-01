import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { SystemSettings } from '../../api/types'
import { SystemSettingsController } from './systemSettingsController'
import { MAX_OPEN_API_RATE_LIMIT_PER_MINUTE } from '../../utils/systemSettings'
import { deferred, settings } from './systemSettingsTestFixtures'

function setup() {
  const api = { get: vi.fn().mockResolvedValue(settings()), update: vi.fn().mockResolvedValue(settings(1, false, false)) }
  const apply = vi.fn()
  let allowed = true
  const controller = new SystemSettingsController(api, () => allowed, apply)
  controller.activate()
  return { api, apply, controller, deny: () => { allowed = false } }
}

describe('系统设置草稿与读取', () => {
  it('StrictMode取消首次加载不会留下loading，旧响应不能覆盖重装结果', async () => {
    const old = deferred<SystemSettings>()
    const { api, controller } = setup()
    api.get.mockReturnValueOnce(old.promise).mockResolvedValueOnce(settings(2, false))
    const first = controller.refresh()
    expect(controller.getSnapshot().loading).toBe(true)
    controller.deactivate(); controller.activate()
    await controller.refresh()
    expect(controller.getSnapshot()).toMatchObject({ loading: false, data: { revision: 2 } })
    old.resolve(settings())
    await first
    expect(controller.getSnapshot().data?.revision).toBe(2)
  })
  it('刷新保留草稿，服务端版本改变则禁止保存直到明确重载', async () => {
    const { api, controller } = setup()
    await controller.refresh()
    controller.edit('mcp_enabled', false)
    await controller.refresh()
    expect(controller.getSnapshot().changes).toEqual({ mcp_enabled: false })
    api.get.mockResolvedValue(settings(1))
    await controller.refresh()
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: true, changes: { mcp_enabled: false }, data: { revision: 0 } })
    await controller.save(); expect(api.update).not.toHaveBeenCalled()
    await controller.reload()
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: false, changes: {}, data: { revision: 1 } })
  })
  it('false是明确修改，null恢复覆盖，未编辑字段不发请求', async () => {
    const { api, controller, apply } = setup()
    api.get.mockResolvedValue(settings(1))
    await controller.refresh()
    controller.edit('mcp_enabled', false)
    controller.restore('registration_enabled')
    await controller.save()
    expect(api.update).toHaveBeenCalledExactlyOnceWith({ revision: 1, changes: { mcp_enabled: false, registration_enabled: null } }, expect.any(AbortSignal))
    expect(controller.getSnapshot()).toMatchObject({ changes: {}, saving: false })
    expect(apply).toHaveBeenLastCalledWith({ registration_enabled: false, mcp_enabled: false })
  })
  it('未覆盖字段恢复只丢弃该项草稿，输入清空/非正整数不得误当null恢复', async () => {
    const { api, controller } = setup()
    await controller.refresh()
    controller.edit('mcp_enabled', false); controller.restore('mcp_enabled')
    expect(controller.getSnapshot().changes).toEqual({})
    controller.edit('mcp_enabled', false)
    for (const bad of [null, 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
      controller.editNumber('task_max_concurrency', bad)
      await controller.save()
      expect(api.update).not.toHaveBeenCalled()
      expect(controller.getSnapshot().invalidNumbers).toContain('task_max_concurrency')
    }
    controller.editNumber('task_max_concurrency', 3)
    expect(controller.getSnapshot().invalidNumbers).toEqual([])
    expect(controller.getSnapshot().changes).toEqual({ mcp_enabled: false, task_max_concurrency: 3 })
  })
  it('读取失败保留上次数据和草稿，不显示假成功', async () => {
    const { api, controller } = setup()
    await controller.refresh(); controller.edit('mcp_enabled', false)
    api.get.mockRejectedValueOnce(new ApiError(0, '离线'))
    await controller.refresh()
    expect(controller.getSnapshot()).toMatchObject({ data: { revision: 0 }, changes: { mcp_enabled: false }, error: '离线', loading: false })
  })
  it('开放API限额拒绝数据库不可执行值，而不额外收窄两级并发', async () => {
    const { api, controller } = setup()
    await controller.refresh()
    controller.editNumber('open_api_rate_limit_per_minute', MAX_OPEN_API_RATE_LIMIT_PER_MINUTE + 1)
    await controller.save()
    expect(api.update).not.toHaveBeenCalled()
    expect(controller.getSnapshot().invalidNumbers).toContain('open_api_rate_limit_per_minute')
    controller.editNumber('open_api_rate_limit_per_minute', MAX_OPEN_API_RATE_LIMIT_PER_MINUTE)
    expect(controller.getSnapshot().invalidNumbers).toEqual([])
    expect(controller.getSnapshot().changes.open_api_rate_limit_per_minute).toBe(MAX_OPEN_API_RATE_LIMIT_PER_MINUTE)
    controller.editNumber('task_max_concurrency', MAX_OPEN_API_RATE_LIMIT_PER_MINUTE + 1)
    controller.editNumber('task_max_per_user', MAX_OPEN_API_RATE_LIMIT_PER_MINUTE + 1)
    expect(controller.getSnapshot().invalidNumbers).toEqual([])
  })
})

describe('保存与权限竞态', () => {
  it.each([0, 409, 500])('%s保存结果先核实，不自动重发/丢草稿', async (status) => {
    const { api, controller } = setup()
    await controller.refresh(); controller.edit('mcp_enabled', false)
    api.update.mockRejectedValueOnce(new ApiError(status, '拒绝'))
    await controller.save(); await controller.save()
    expect(api.update).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: true, changes: { mcp_enabled: false }, saving: false })
    await controller.refresh()
    expect(controller.getSnapshot().reviewRequired).toBe(false)
    expect(api.update).toHaveBeenCalledTimes(1)
  })
  it('422保持旧有效值和编辑，允许改正；忙时不会重复发PATCH或刷新', async () => {
    const { api, controller } = setup()
    await controller.refresh(); controller.edit('mcp_enabled', false)
    const result = deferred<SystemSettings>()
    api.update.mockReturnValueOnce(result.promise)
    const save = controller.save()
    controller.edit('mcp_enabled', true); await controller.save(); await controller.refresh()
    expect(api.update).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledTimes(1)
    result.reject(new ApiError(422, '校验失败'))
    await save
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: false, data: { revision: 0 }, changes: { mcp_enabled: false }, error: '校验失败' })
  })
  it.each([401, 403])('%s清空数据与草稿', async (status) => {
    const { api, controller } = setup()
    await controller.refresh(); controller.edit('mcp_enabled', false)
    api.update.mockRejectedValueOnce(new ApiError(status, '无权'))
    await controller.save()
    expect(controller.getSnapshot()).toMatchObject({ denied: true, data: null, changes: {}, loading: false, saving: false })
  })
  it('卸载、换账号或降权后的旧保存响应不能应用公开配置/复活数据', async () => {
    const { api, apply, controller, deny } = setup()
    await controller.refresh(); apply.mockClear(); controller.edit('mcp_enabled', false)
    const result = deferred<SystemSettings>()
    api.update.mockReturnValueOnce(result.promise)
    const save = controller.save()
    deny(); controller.revoke()
    result.resolve(settings(1, false, false)); await save
    expect(apply).not.toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ denied: true, data: null, changes: {} })
  })
  it('异步落地自身复核权限，即使订阅清理尚未触发也撤下旧数据', async () => {
    const { api, apply, controller, deny } = setup()
    await controller.refresh(); apply.mockClear()
    const result = deferred<SystemSettings>()
    api.get.mockReturnValueOnce(result.promise)
    const refresh = controller.refresh()
    deny(); result.resolve(settings(1)); await refresh
    expect(apply).not.toHaveBeenCalled()
    expect(controller.getSnapshot()).toMatchObject({ denied: true, data: null, loading: false })
  })
})
