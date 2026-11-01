import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { AdminUser } from '../../api/types'
import { AdminUserActionsController } from './adminUserActionsController'
import type { AdminUserAction, AdminUserActionTarget } from './adminUserActionsController'

const user: AdminUser = { id: 2, username: 'alice', is_admin: false, is_active: true, created_at: '2026-10-30T00:00:00Z', project_count: 0 }
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej }); return { promise, resolve, reject } }
function setup() {
  const callbacks = { changed: vi.fn(), deleted: vi.fn(), success: vi.fn() }
  const api = { resetPassword: vi.fn(async () => {}), updateUser: vi.fn(async () => user), deleteUser: vi.fn(async () => {}) }
  let allowed = true
  const controller = new AdminUserActionsController(1, () => allowed, callbacks, api)
  controller.activate()
  return { controller, callbacks, api, deny: () => { allowed = false } }
}
function target(controller: AdminUserActionsController): AdminUserActionTarget { const result = controller.getSnapshot().target; if (!result) throw new Error('缺少待确认目标'); return result }

describe('账号动作真实提交路径', () => {
  it.each<AdminUserAction>(['reset', 'active', 'admin', 'delete'])('%s 使用原 API，确认后才产生动作及成功回调', async (action) => {
    const { controller, callbacks, api } = setup()
    controller.open(user, action)
    expect(api.updateUser).not.toHaveBeenCalled()
    expect(api.resetPassword).not.toHaveBeenCalled()
    expect(api.deleteUser).not.toHaveBeenCalled()
    if (action === 'delete') controller.setConfirmation(user.username)
    await expect(controller.submit(target(controller), async () => ({ new_password: 'secret123' }))).resolves.toBe(true)
    if (action === 'reset') expect(api.resetPassword).toHaveBeenCalledWith(2, { new_password: 'secret123' })
    else if (action === 'active') expect(api.updateUser).toHaveBeenCalledWith(2, { is_active: false })
    else if (action === 'admin') expect(api.updateUser).toHaveBeenCalledWith(2, { is_admin: true })
    else expect(api.deleteUser).toHaveBeenCalledWith(2)
    expect(callbacks.success).toHaveBeenCalledOnce()
    expect(action === 'delete' ? callbacks.deleted : callbacks.changed).toHaveBeenCalledOnce()
    expect(controller.getSnapshot().target).toBeNull()
  })

  it('本人所有动作、有任何作品的删号均不可打开；其他动作保留', () => {
    const { controller } = setup()
    for (const action of ['reset', 'active', 'admin', 'delete'] as const) {
      controller.open({ ...user, id: 1 }, action)
      expect(controller.getSnapshot().target).toBeNull()
    }
    controller.open({ ...user, project_count: 1 }, 'delete')
    expect(controller.getSnapshot().target).toBeNull()
    controller.open({ ...user, project_count: 1 }, 'active')
    expect(target(controller).action).toBe('active')
  })

  it('删号确认逐字相等，不 trim、折叠大小写或自动重发', async () => {
    const { controller, api } = setup()
    controller.open(user, 'delete')
    for (const input of ['', ' alice', 'alice ', 'Alice']) {
      controller.setConfirmation(input)
      await expect(controller.submit(target(controller))).resolves.toBe(false)
    }
    expect(api.deleteUser).not.toHaveBeenCalled()
    controller.setConfirmation('alice')
    await controller.submit(target(controller))
    expect(api.deleteUser).toHaveBeenCalledOnce()
  })

  it('异步表单校验期间同步阻止双击；校验失败不请求且可再次确认', async () => {
    const { controller, api } = setup()
    const check = deferred<{ new_password: string } | null>()
    controller.open(user, 'reset')
    const selected = target(controller)
    const first = controller.submit(selected, () => check.promise)
    expect(controller.getSnapshot().busy).toBe(true)
    await expect(controller.submit(selected, async () => ({ new_password: 'second' }))).resolves.toBe(false)
    controller.close()
    expect(controller.getSnapshot().target).toBe(selected)
    check.resolve(null)
    await expect(first).resolves.toBe(false)
    expect(api.resetPassword).not.toHaveBeenCalled()
    expect(controller.getSnapshot().busy).toBe(false)
    await controller.submit(selected, async () => ({ new_password: 'valid123' }))
    expect(api.resetPassword).toHaveBeenCalledOnce()
  })

  it('校验尚未结束即离页时不再发出请求；StrictMode清理后可以重新激活', async () => {
    const { controller, api, callbacks } = setup()
    const check = deferred<{ new_password: string }>()
    controller.open(user, 'reset')
    const old = controller.submit(target(controller), () => check.promise)
    controller.deactivate()
    controller.activate()
    controller.open({ ...user, id: 3, username: 'bob' }, 'active')
    check.resolve({ new_password: 'old123' })
    await expect(old).resolves.toBe(false)
    expect(api.resetPassword).not.toHaveBeenCalled()
    expect(target(controller).user.id).toBe(3)
    expect(callbacks.changed).not.toHaveBeenCalled()
    await expect(controller.submit(target(controller))).resolves.toBe(true)
    expect(api.updateUser).toHaveBeenCalledWith(3, { is_active: false })
  })

  it.each([false, true])('已发请求迟到%s时不交付旧成功/失败，也不清除新目标', async (failing) => {
    const { controller, api, callbacks } = setup()
    const request = deferred<AdminUser>()
    api.updateUser.mockReturnValueOnce(request.promise)
    controller.open(user, 'active')
    const old = controller.submit(target(controller))
    controller.deactivate()
    controller.activate()
    controller.open({ ...user, id: 3 }, 'admin')
    if (failing) request.reject(new ApiError(409, '旧错误'))
    else request.resolve(user)
    await expect(old).resolves.toBe(false)
    expect(target(controller).user.id).toBe(3)
    expect(controller.getSnapshot().error).toBeNull()
    expect(callbacks.success).not.toHaveBeenCalled()
    expect(callbacks.changed).not.toHaveBeenCalled()
  })

  it('权限在提交前或响应落地前失效时，关闭目标且不交付导航/刷新回调', async () => {
    const first = setup()
    first.controller.open(user, 'delete')
    const selected = target(first.controller)
    first.controller.setConfirmation('alice')
    first.deny()
    await expect(first.controller.submit(selected)).resolves.toBe(false)
    expect(first.api.deleteUser).not.toHaveBeenCalled()
    const second = setup()
    const request = deferred<void>()
    second.api.deleteUser.mockReturnValueOnce(request.promise)
    second.controller.open(user, 'delete')
    second.controller.setConfirmation('alice')
    const work = second.controller.submit(target(second.controller))
    second.deny()
    request.resolve()
    await expect(work).resolves.toBe(false)
    expect(second.controller.getSnapshot().target).toBeNull()
    expect(second.callbacks.deleted).not.toHaveBeenCalled()
    expect(second.callbacks.success).not.toHaveBeenCalled()
  })

  it('切换/取消目标后，旧确认对象不能操作新目标', async () => {
    const { controller, api } = setup()
    controller.open(user, 'active')
    const old = target(controller)
    controller.close()
    controller.open({ ...user, id: 3 }, 'active')
    await expect(controller.submit(old)).resolves.toBe(false)
    expect(api.updateUser).not.toHaveBeenCalled()
    expect(target(controller).user.id).toBe(3)
  })

  it('删号409收窄最新全部作品数并禁止再次确认；待收尾任务错误不伪造成计数', async () => {
    const { controller, api, callbacks } = setup()
    api.deleteUser.mockRejectedValueOnce(new ApiError(409, '名下仍有作品', { project_count: 2 }))
    controller.open(user, 'delete')
    controller.setConfirmation('alice')
    await controller.submit(target(controller))
    expect(target(controller).user.project_count).toBe(2)
    expect(controller.canSubmit()).toBe(false)
    expect(controller.getSnapshot().error).toBe('名下仍有作品')
    expect(callbacks.changed).toHaveBeenCalledOnce()
    expect(callbacks.deleted).not.toHaveBeenCalled()
    controller.close()
    api.deleteUser.mockRejectedValueOnce(new ApiError(409, '取消请求尚未收尾', { code: 'user_has_pending_tasks' }))
    controller.open(user, 'delete')
    controller.setConfirmation('alice')
    await controller.submit(target(controller))
    expect(target(controller).user.project_count).toBe(0)
    expect(controller.getSnapshot().error).toBe('取消请求尚未收尾')
    expect(api.deleteUser).toHaveBeenCalledTimes(2)
  })
})
