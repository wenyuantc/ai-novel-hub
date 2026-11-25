import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { SystemSettings, TaskDrainStatus, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { DRAIN_STATUS_POLL_MS, SystemSettingsController } from './systemSettingsController'
import { MAX_OPEN_API_RATE_LIMIT_PER_MINUTE } from '../../utils/systemSettings'
import { deferred, drainStatus, settings } from './systemSettingsTestFixtures'
import { watchSystemSettingsAuthorization } from './useSystemSettings'

afterEach(() => { vi.useRealTimers() })

function setup() {
  const api = { get: vi.fn().mockResolvedValue(settings()), update: vi.fn().mockResolvedValue(settings(1, false, false)), drainStatus: vi.fn().mockResolvedValue(drainStatus()) }
  const apply = vi.fn()
  let allowed = true
  const controller = new SystemSettingsController(api, () => allowed, apply)
  controller.activate()
  return { api, apply, controller, deny: () => { allowed = false } }
}

describe('系统设置授权订阅', () => {
  it('只读用户的首次读取不被公开设置刷新中止，授权代次变更则撤销旧读并重读', async () => {
    const auth = useAuthStore.getState()
    const reader: User = { id: 7, username: '只读管理', is_admin: false, is_super_admin: false, roles: [],
      effective_permissions: ['settings.read'], authorization_revision: 1, created_at: '' }
    useAuthStore.setState({ status: 'authenticated', user: reader })
    const { api, controller } = setup()
    const refreshDrain = vi.fn()
    const stop = watchSystemSettingsAuthorization(reader.id, controller, refreshDrain)
    try {
      const initial = deferred<SystemSettings>()
      api.get.mockReturnValueOnce(initial.promise)
      const pending = controller.refresh()
      const initialSignal = api.get.mock.calls[0][0] as AbortSignal
      useAuthStore.getState().applyConfig({ registration_enabled: false, mcp_enabled: false })
      expect(initialSignal.aborted).toBe(false)
      expect(refreshDrain).not.toHaveBeenCalled()
      initial.resolve(settings(1))
      await pending
      expect(controller.getSnapshot().data?.revision).toBe(1)

      const obsolete = deferred<SystemSettings>()
      api.get.mockReturnValueOnce(obsolete.promise).mockResolvedValueOnce(settings(2))
      const oldRead = controller.refresh()
      const obsoleteSignal = api.get.mock.calls[1][0] as AbortSignal
      useAuthStore.setState({ user: { ...reader, authorization_revision: 2 } })
      expect(obsoleteSignal.aborted).toBe(true)
      expect(refreshDrain).toHaveBeenCalledTimes(1)
      expect(api.get).toHaveBeenCalledTimes(3)
      obsolete.resolve(settings(99))
      await oldRead
      expect(controller.getSnapshot().data?.revision).toBe(2)
    } finally {
      stop()
      useAuthStore.setState(auth, true)
    }
  })
})

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

describe('排空状态刷新', () => {
  it('独立读状态，失败则保留最近成功取样并标为过期，草稿不变', async () => {
    const { api, apply, controller } = setup()
    await controller.refresh()
    controller.edit('task_admission_closed', true)
    const known = drainStatus({ running: 1 })
    api.drainStatus.mockResolvedValueOnce(known).mockRejectedValueOnce(new ApiError(0, '离线'))
    await controller.refreshDrainStatus()
    expect(controller.getSnapshot()).toMatchObject({ drain: { data: known, loading: false, stale: false }, changes: { task_admission_closed: true } })
    await controller.refreshDrainStatus()
    expect(controller.getSnapshot()).toMatchObject({ drain: { data: known, loading: false, stale: true, error: '离线' }, changes: { task_admission_closed: true }, data: { revision: 0 } })
    expect(apply).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('另一管理员切换开关后自动核对一次有效配置，同一不一致不重复请求', async () => {
    const { api, controller } = setup()
    await controller.refresh()
    api.drainStatus.mockResolvedValue(drainStatus({ enabled: true }))
    await controller.refreshDrainStatus()
    await controller.refreshDrainStatus()
    expect(api.get).toHaveBeenCalledTimes(2)
    // 如果先读到与取样一致的原值，再出现新的不一致，允许重新核对。
    api.drainStatus.mockResolvedValueOnce(drainStatus())
    await controller.refreshDrainStatus()
    api.get.mockResolvedValueOnce(settings(1, true, true, true))
    await controller.refreshDrainStatus()
    expect(controller.getSnapshot().data?.revision).toBe(1)
    expect(controller.getSnapshot().data?.items.find((item) => item.key === 'task_admission_closed')?.value).toBe(true)
    await controller.refreshDrainStatus()
    expect(api.get).toHaveBeenCalledTimes(3)
  })

  it('定时状态与旧配置矛盾时保留草稿并进入核对，后续轮询不反复读取完整配置', async () => {
    const { api, controller } = setup()
    await controller.refresh()
    controller.edit('mcp_enabled', false)
    api.get.mockResolvedValueOnce(settings(2, true, true, true))
    api.drainStatus.mockResolvedValue(drainStatus({ enabled: true }))
    await controller.refreshDrainStatus()
    expect(controller.getSnapshot()).toMatchObject({
      changes: { mcp_enabled: false }, data: { revision: 0 }, reviewRequired: true,
      drain: { data: { enabled: true } },
    })
    await controller.refreshDrainStatus()
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(api.update).not.toHaveBeenCalled()
  })

  it('已过期的排空取样不能引起有效配置刷新', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    const { api, controller } = setup()
    await controller.refresh()
    const delayed = deferred<TaskDrainStatus>()
    api.drainStatus.mockReturnValueOnce(delayed.promise)
    const pending = controller.refreshDrainStatus()
    vi.advanceTimersByTime(2 * DRAIN_STATUS_POLL_MS)
    delayed.resolve(drainStatus({ enabled: true }))
    await pending
    expect(controller.getSnapshot().drain.stale).toBe(true)
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('慢请求不重叠，两轮未收到新取样时撤下排空结论', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    const { api, controller } = setup()
    const slow = deferred<TaskDrainStatus>()
    api.drainStatus.mockResolvedValueOnce(drainStatus({ enabled: true })).mockReturnValueOnce(slow.promise)
    await controller.refreshDrainStatus()
    vi.advanceTimersByTime(DRAIN_STATUS_POLL_MS)
    const pending = controller.refreshDrainStatus()
    expect(controller.getSnapshot().drain).toMatchObject({ stale: false, loading: true })
    vi.advanceTimersByTime(DRAIN_STATUS_POLL_MS)
    await controller.refreshDrainStatus()
    expect(controller.getSnapshot().drain).toMatchObject({ stale: true, loading: true })
    expect(api.drainStatus).toHaveBeenCalledTimes(2)
    slow.resolve(drainStatus({ enabled: true, finalizing: 1 }))
    await pending
    expect(controller.getSnapshot().drain).toMatchObject({ stale: false, loading: false, data: { finalizing: 1 } })
  })

  it('设置保存后立即丢弃旧取样并另读，迟到响应不能倒退结论', async () => {
    const { api, controller } = setup()
    const old = deferred<TaskDrainStatus>()
    const latest = deferred<TaskDrainStatus>()
    api.update.mockResolvedValueOnce(settings(1, true, true, true))
    api.drainStatus.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    await controller.refresh()
    const first = controller.refreshDrainStatus()
    const signal = api.drainStatus.mock.calls[0][0] as AbortSignal
    controller.edit('task_admission_closed', true)
    await controller.save()
    expect(signal.aborted).toBe(true)
    expect(api.drainStatus).toHaveBeenCalledTimes(2)
    expect(controller.getSnapshot()).toMatchObject({ changes: {}, data: { revision: 1 }, drain: { data: null, loading: true } })
    latest.resolve(drainStatus({ enabled: true, running: 1 }))
    await latest.promise
    old.resolve(drainStatus({ paused: 2 }))
    await first
    expect(controller.getSnapshot().drain).toMatchObject({ data: { enabled: true, running: 1, paused: 0 }, loading: false })
  })

  it('撤销请求代次与 StrictMode 卸载后忽略迟到响应，读权限失效即清空', async () => {
    const { api, controller } = setup()
    await controller.refresh()
    const old = deferred<TaskDrainStatus>()
    api.drainStatus.mockReturnValueOnce(old.promise)
    const first = controller.refreshDrainStatus()
    const [oldSignal, isOldCurrent] = api.drainStatus.mock.calls[0] as [AbortSignal, () => boolean]
    controller.invalidateDrainStatus()
    expect(oldSignal.aborted).toBe(true)
    expect(isOldCurrent()).toBe(false)
    controller.deactivate()
    controller.activate()
    await controller.refreshDrainStatus()
    old.resolve(drainStatus({ enabled: true, paused: 2 }))
    await first
    expect(controller.getSnapshot().drain).toMatchObject({ data: { paused: 0 }, loading: false })
    expect(api.get).toHaveBeenCalledTimes(1)
    api.drainStatus.mockRejectedValueOnce(new ApiError(403, '无权'))
    await controller.refreshDrainStatus()
    expect(controller.getSnapshot()).toMatchObject({ denied: true, data: null, drain: { data: null, loading: false } })
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
  it.each([401, 403])('%s按读取与写入能力分别撤销', async (status) => {
    const { api, controller } = setup()
    await controller.refresh(); controller.edit('mcp_enabled', false)
    api.update.mockRejectedValueOnce(new ApiError(status, '无权'))
    await controller.save()
    expect(controller.getSnapshot()).toMatchObject({ denied: status === 401, data: status === 401 ? null : expect.objectContaining({revision: 0}), changes: {}, loading: false, saving: false })
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
