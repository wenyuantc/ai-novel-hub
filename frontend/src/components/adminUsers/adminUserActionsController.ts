import { errorMessage } from '../../api/client'
import { adminApi } from '../../api/admin'
import type { AdminUser, ResetPasswordRequest } from '../../api/types'
import { canConfirmDelete, rowActions, userProjectCountFromError } from '../../utils/adminUsers'
import { createRecycleRequestGate } from '../../utils/recycleBin'
import { useAuthStore } from '../../stores/auth'

export type AdminUserAction = 'reset' | 'active' | 'delete'
export interface AdminUserActionTarget { user: AdminUser; action: AdminUserAction }
export interface AdminUserActionsState {
  target: AdminUserActionTarget | null
  busy: boolean
  confirmation: string
  error: string | null
}
interface Callbacks {
  changed: () => void
  deleted: () => void
  success: (message: string) => void
}

function permitted(target: AdminUserActionTarget, actorId: number) {
  const actions = rowActions(target.user, actorId, useAuthStore.getState().user)
  return { reset: actions.canResetPassword, active: actions.canToggleActive, delete: actions.canDelete }[target.action]
}

/** 只拥有当前页的目标账号动作；失效不撤回已发请求，但绝不交付旧响应。 */
export class AdminUserActionsController {
  private state: AdminUserActionsState = { target: null, busy: false, confirmation: '', error: null }
  private listeners = new Set<() => void>()
  private gate = createRecycleRequestGate()
  private active = false

  constructor(private readonly actorId: number, private readonly allowed: () => boolean, private callbacks: Callbacks, private readonly api: Pick<typeof adminApi, 'resetPassword' | 'updateUser' | 'deleteUser'> = adminApi) {}

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  setCallbacks(callbacks: Callbacks) { this.callbacks = callbacks }
  private update(patch: Partial<AdminUserActionsState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  activate() { this.active = true }
  deactivate() { this.active = false; this.gate.invalidate(); this.update({ target: null, busy: false, confirmation: '', error: null }) }
  private canUse() { if (!this.allowed()) { this.deactivate(); return false }; return this.active }
  private current(ticket: number, target: AdminUserActionTarget) { return this.canUse() && this.gate.isCurrent(ticket) && this.state.target === target && permitted(target, this.actorId) }

  open = (user: AdminUser, action: AdminUserAction) => {
    const target = { user: { ...user }, action }
    if (!this.canUse() || this.state.busy || this.state.target || !permitted(target, this.actorId)) return
    this.gate.invalidate()
    this.update({ target, confirmation: '', error: null })
  }
  close = () => { if (!this.state.busy) { this.gate.invalidate(); this.update({ target: null, confirmation: '', error: null }) } }
  setConfirmation = (confirmation: string) => { if (this.canUse() && !this.state.busy) this.update({ confirmation }) }
  canSubmit = () => {
    const { target, busy, confirmation } = this.state
    return !!target && !busy && permitted(target, this.actorId) && (target.action !== 'delete' || canConfirmDelete(confirmation, target.user.username))
  }

  /** prepare 的异步表单校验也在同一代次内，校验迟到不能提交新目标。 */
  submit = async (target: AdminUserActionTarget, prepare?: () => Promise<ResetPasswordRequest | null>): Promise<boolean> => {
    if (!this.canUse() || this.state.target !== target || !this.canSubmit()) return false
    const ticket = this.gate.begin()
    this.update({ busy: true, error: null })
    try {
      const password = target.action === 'reset' ? await prepare?.() : undefined
      if (!this.current(ticket, target) || (target.action === 'reset' && !password)) return false
      const { user, action } = target
      if (action === 'reset' && password) await this.api.resetPassword(user.id, password)
      else if (action === 'active') await this.api.updateUser(user.id, { is_active: !user.is_active })
      else if (action === 'delete') await this.api.deleteUser(user.id)
      if (!this.current(ticket, target)) return false
      this.update({ target: null, busy: false, confirmation: '', error: null })
      this.callbacks.success(action === 'reset' ? `已重置「${user.username}」的密码，其已登录会话已失效` : action === 'delete' ? `已删除用户「${user.username}」` : '已更新')
      if (action === 'delete') this.callbacks.deleted()
      else this.callbacks.changed()
      return true
    } catch (err) {
      if (!this.current(ticket, target)) return false
      const count = target.action === 'delete' ? userProjectCountFromError(err) : null
      this.update({ busy: false, error: errorMessage(err), ...(count === null ? {} : { target: { ...target, user: { ...target.user, project_count: count } } }) })
      if (count !== null) this.callbacks.changed()
      return false
    } finally {
      if (this.current(ticket, target)) this.update({ busy: false })
    }
  }
}
