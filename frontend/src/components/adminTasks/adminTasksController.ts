import { adminApi } from '../../api/admin'
import { adminTasksApi } from '../../api/adminTasks'
import { ApiError, errorMessage } from '../../api/client'
import type { AdminTaskBrief, AdminTaskDetail, AdminTaskList, AdminTaskListQuery, AdminUser, Page } from '../../api/types'
import { adminTaskCancellationOutcome, adminTaskCancelReason, adminTaskTitle, isAdminTaskKey } from '../../utils/adminTasks'
import { auditValidPage } from '../../utils/auditLogs'

export interface TaskResource<T> { data: T | null; loading: boolean; error: string | null }
export interface CancellationNotice {
  taskKey: string
  title: string
  text: string
  type: 'info' | 'warning' | 'success'
  operationId?: string
  auditLogId?: number | null
  checking?: boolean
}
interface AdminTasksState {
  query: AdminTaskListQuery
  list: TaskResource<AdminTaskList>
  detailKey: string | null
  detail: TaskResource<AdminTaskDetail>
  owners: TaskResource<Page<AdminUser>>
  cancelTarget: AdminTaskBrief | null
  cancelLoading: boolean
  submitting: boolean
  cancelError: string | null
  notice: CancellationNotice | null
  autoRefresh: boolean
  denied: boolean
}
interface Loaders {
  list: typeof adminTasksApi.list
  get: typeof adminTasksApi.get
  cancel: typeof adminTasksApi.cancel
  owners: typeof adminApi.listUsers
}
type Channel = 'list' | 'detail' | 'owners' | 'confirmation' | 'mutation' | 'watch'
const channels: Channel[] = ['list', 'detail', 'owners', 'confirmation', 'mutation', 'watch']
const empty = <T>(): TaskResource<T> => ({ data: null, loading: false, error: null })

/** 管理监控的页面级请求所有者；不缓存他人任务到个人 store。 */
export class AdminTasksController {
  private state: AdminTasksState
  private active = false
  private listeners = new Set<() => void>()
  private flights = new Map<Channel, AbortController>()
  private generations = new Map<Channel, number>()
  private watchKey: string | null = null
  private uncertain = false

  constructor(readonly owner: number, query: AdminTaskListQuery = {}, private loaders: Loaders = {
    ...adminTasksApi, owners: adminApi.listUsers,
  }, private authorized: () => boolean = () => true, private canCancel: () => boolean = () => true, private canReadOwners: () => boolean = () => true) {
    this.state = {
      query: { sort: 'created_desc', page: 1, page_size: 20, ...query }, list: empty(), detailKey: null, detail: empty(), owners: empty(),
      cancelTarget: null, cancelLoading: false, submitting: false, cancelError: null, notice: null, autoRefresh: true, denied: false,
    }
  }
  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { if (!this.state.denied && this.authorized()) this.active = true }
  revoke() { this.deny() }
  revokeWrites() { this.closeCancellation() }
  revokeOwners() { this.invalidate('owners'); this.update({ owners: empty() }) }
  deactivate() {
    this.active = false
    channels.forEach((channel) => this.invalidate(channel))
    this.watchKey = null
    this.update({ list: empty(), detailKey: null, detail: empty(), owners: empty(), cancelTarget: null, cancelLoading: false, submitting: false, cancelError: null, notice: null })
  }
  setAutoRefresh(autoRefresh: boolean) { this.update({ autoRefresh }) }
  setQuery(query: AdminTaskListQuery) {
    if (!this.usable()) return
    this.invalidate('list')
    this.closeDetail()
    this.invalidate('watch')
    this.watchKey = null
    this.update({ query: { sort: 'created_desc', page: 1, page_size: 20, ...query }, list: empty(), notice: null })
    this.refreshList()
  }
  setPage(page: number, pageSize: number) {
    this.setQuery({ ...this.state.query, page: pageSize !== this.state.query.page_size ? 1 : page, page_size: pageSize })
  }
  refreshAll = () => { this.refreshList(); this.refreshDetail(); this.refreshWatch() }
  refreshList = () => {
    const query = this.state.query
    this.run('list', async (signal) => {
      let data = await this.loaders.list(query, signal)
      const page = auditValidPage(query.page ?? 1, data.total, query.page_size ?? 20)
      if (page !== query.page) data = await this.loaders.list({ ...query, page }, signal)
      return { data, page }
    }, () => this.update({ list: { ...this.state.list, loading: true } }), ({ data, page }) => {
      this.update({ list: { data, loading: false, error: null }, query: { ...query, page } })
    }, (err) => this.update({ list: { ...this.state.list, loading: false, error: errorMessage(err) } }))
  }
  openDetail(taskKey: string) {
    if (!this.usable() || !isAdminTaskKey(taskKey)) return
    const key = taskKey.toLowerCase()
    if (this.state.detailKey === key) return
    this.closeDetail()
    this.update({ detailKey: key, detail: empty() })
    this.refreshDetail()
  }
  closeDetail = () => {
    this.invalidate('detail')
    this.closeCancellation()
    this.update({ detailKey: null, detail: empty() })
  }
  refreshDetail = () => {
    const key = this.state.detailKey
    if (!key) return
    this.run('detail', (signal) => this.getExact(key, signal),
      () => this.update({ detail: { ...this.state.detail, loading: true } }),
      (data) => { this.update({ detail: { data, loading: false, error: null } }); this.observeCancellation(key, data) },
      (err) => {
        this.update({ detail: { data: err instanceof ApiError && err.status === 404 ? null : this.state.detail.data, loading: false,
          error: err instanceof ApiError && err.status === 404 ? '任务已移除，不再提供明细；不会跳转到复用编号的新任务。' : errorMessage(err) } })
        this.failCancellationRead(key, err)
      })
  }
  searchOwners = (q = '') => {
    if (!this.usable() || !this.canReadOwners()) return
    this.invalidate('owners')
    this.run('owners', (signal) => this.loaders.owners({ q: q.slice(0, 64), page_size: 20 }, signal),
      () => this.update({ owners: { ...this.state.owners, loading: true } }),
      (data) => this.update({ owners: { data, loading: false, error: null } }),
      (err) => this.update({ owners: { ...this.state.owners, loading: false, error: errorMessage(err) } }))
  }
  openCancellation = (task: AdminTaskBrief) => {
    if (!this.usable() || !this.canCancel() || this.state.submitting || (this.state.notice?.taskKey === task.task_key && this.state.notice.checking) || !task.can_cancel || task.cancellation_pending || !isAdminTaskKey(task.task_key)) return
    this.closeCancellation()
    this.update({ cancelTarget: task, cancelLoading: true, cancelError: null })
    this.run('confirmation', (signal) => this.getExact(task.task_key!, signal), () => {},
      (data) => this.update({ cancelTarget: data, cancelLoading: false, cancelError: data.can_cancel && !data.cancellation_pending ? null : '任务状态已变化或已在取消中，请关闭确认并刷新。' }),
      (err) => this.update({ cancelTarget: this.state.cancelTarget ? { ...this.state.cancelTarget, can_cancel: false } : null, cancelLoading: false, cancelError: err instanceof ApiError && err.status === 404 ? '任务已移除，无法取消。' : errorMessage(err) }))
  }
  closeCancellation = () => {
    this.invalidate('confirmation')
    this.invalidate('mutation')
    this.update({ cancelTarget: null, cancelLoading: false, submitting: false, cancelError: null })
  }
  submitCancellation(reasonValue: string) {
    const target = this.state.cancelTarget
    if (!this.usable() || !this.canCancel() || this.state.submitting || this.state.cancelLoading || !target?.can_cancel || target.cancellation_pending || !isAdminTaskKey(target.task_key)) return
    let reason: string
    try { reason = adminTaskCancelReason(reasonValue) }
    catch (err) { this.update({ cancelError: errorMessage(err) }); return }
    const key = target.task_key
    this.run('mutation', (signal) => this.loaders.cancel(key, { confirm: true, reason }, signal),
      () => this.update({ submitting: true, cancelError: null }),
      (result) => {
        this.watchKey = key
        this.uncertain = false
        this.update({ cancelTarget: null, submitting: false, notice: { taskKey: key, title: adminTaskTitle(target), type: 'info',
          text: '取消请求已受理，尚未确认任务已取消。正在重新读取实际状态。', operationId: result.operation_id, auditLogId: result.audit_log_id, checking: true } })
        this.refreshAfterMutation()
      },
      (err) => {
        const status = err instanceof ApiError ? err.status : 0
        const uncertain = status === 0 || status >= 500
        this.watchKey = uncertain ? key : null
        this.uncertain = uncertain
        this.update({ cancelTarget: null, submitting: false, notice: { taskKey: key, title: adminTaskTitle(target), type: 'warning',
          checking: uncertain, text: uncertain ? '取消请求结果不确定，正在刷新核对；不会自动重复提交。' : status === 404 ? '任务已移除，无法取消；不会改动同编号的新任务。' : status === 409 ? '任务已结束或状态发生变化，请查看刷新后的实际状态。' : errorMessage(err) } })
        this.refreshAfterMutation()
      })
  }

  private refreshAfterMutation() {
    this.invalidate('list')
    this.invalidate('detail')
    this.invalidate('watch')
    this.refreshAll()
  }
  private refreshWatch() {
    const key = this.watchKey
    // 已打开同一详情时复用详情请求，不并发读取同一UUID两次。
    if (!key || this.state.detailKey === key) return
    this.run('watch', (signal) => this.getExact(key, signal), () => {},
      (task) => this.observeCancellation(key, task), (err) => this.failCancellationRead(key, err))
  }
  private observeCancellation(key: string, task: AdminTaskDetail) {
    if (this.watchKey !== key) return
    const pending = task.cancellation_pending
    let text = adminTaskCancellationOutcome(task)
    if (!text && this.uncertain) text = '刷新后未发现待收尾取消标记，尚不能确认原请求是否受理。请核对任务后重新确认，不会自动重发。'
    const notice = this.state.notice
    if (notice?.taskKey === key) this.update({ notice: { ...notice, checking: pending || (!text && !this.uncertain), text: text ?? '已受理；当前仍在执行，请继续刷新确认实际结果。', type: task.status === 'cancelled' && !pending ? 'success' : 'info' } })
    if (!pending && (text !== null || this.uncertain)) this.watchKey = null
  }
  private failCancellationRead(key: string, err: unknown) {
    if (this.watchKey !== key || this.state.notice?.taskKey !== key) return
    const missing = err instanceof ApiError && err.status === 404
    this.update({ notice: { ...this.state.notice, checking: !missing, type: 'warning', text: missing ? '任务已移除，不能继续确认实际终态；取消受理审计独立保留。' : '取消后状态刷新失败，当前结果可能已过期；请手动刷新，不会自动重发取消请求。' } })
    if (missing) this.watchKey = null
  }
  private async getExact(key: string, signal: AbortSignal) {
    const data = await this.loaders.get(key, signal)
    if (data.task_key !== key) throw new Error('任务执行标识不匹配，无法展示或操作')
    return data
  }
  private usable() { return this.active && !this.state.denied && this.authorized() }
  private update(patch: Partial<AdminTasksState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  private invalidate(channel: Channel) {
    this.generations.set(channel, (this.generations.get(channel) ?? 0) + 1)
    this.flights.get(channel)?.abort()
    this.flights.delete(channel)
  }
  private deny() {
    this.deactivate()
    this.update({ denied: true, autoRefresh: false })
  }
  private run<T>(channel: Channel, loader: (signal: AbortSignal) => Promise<T>, loading: () => void, success: (data: T) => void, failure: (err: unknown) => void) {
    if (!this.usable() || this.flights.has(channel)) return
    const controller = new AbortController()
    const generation = (this.generations.get(channel) ?? 0) + 1
    this.generations.set(channel, generation)
    this.flights.set(channel, controller)
    const valid = () => this.usable() && !controller.signal.aborted && this.generations.get(channel) === generation && (channel === 'owners' ? this.canReadOwners() : channel === 'mutation' || channel === 'confirmation' ? this.canCancel() : true)
    loading()
    void (async () => {
      try { const data = await loader(controller.signal); if (valid()) success(data) }
      catch (err: unknown) {
        // 同一仍有效actor的旧目标403也是权限撤销；账号已换/卸载则不能干扰新会话。
        if (this.usable() && err instanceof ApiError && (err.status === 401 || err.status === 403) && !['owners', 'mutation', 'confirmation'].includes(channel)) this.deny()
        else if (!valid()) return
        else failure(err)
      } finally { if (this.flights.get(channel) === controller) this.flights.delete(channel) }
    })()
  }
}
