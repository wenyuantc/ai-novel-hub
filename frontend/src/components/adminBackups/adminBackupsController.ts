import { adminBackupsApi } from '../../api/adminBackups'
import { ApiError, errorMessage } from '../../api/client'
import type { BackupDestination, BackupManualRequest, BackupOverview, BackupRunFilters, BackupRunList, BackupRunOut, BackupSettingsChanges, BackupSettingsOut } from '../../api/types'
import { backupChanges, backupDraftError } from './backupSettings'

export interface BackupResource<T> { data: T | null; loading: boolean; error: string | null }
export interface BackupsState {
  overview: BackupResource<BackupOverview>
  settings: BackupResource<BackupSettingsOut>
  list: BackupResource<BackupRunList>
  detail: BackupResource<BackupRunOut>
  detailId: number | null
  query: BackupRunFilters
  changes: BackupSettingsChanges
  invalidNumbers: string[]
  saving: boolean
  starting: boolean
  downloading: string | null
  reviewRequired: boolean
  manualUncertain: boolean
  autoRefresh: boolean
  denied: boolean
  notice: string | null
  error: string | null
}
const empty = <T>(): BackupResource<T> => ({ data: null, loading: false, error: null })
type Channel = 'overview' | 'settings' | 'list' | 'detail' | 'save' | 'run' | 'download'
const channels: Channel[] = ['overview', 'settings', 'list', 'detail', 'save', 'run', 'download']
export interface BackupRights { read: () => boolean; configure: () => boolean; run: () => boolean; download: () => boolean }
const defaultRights: BackupRights = { read: () => true, configure: () => true, run: () => true, download: () => true }

function newManualRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  // 普通HTTP部署没有randomUUID，getRandomValues仍可用；保持v4格式和安全随机源。
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

/** 本页拥有请求代次、秘密草稿和稳定受理意图，不把整库记录写入个人任务store。 */
export class AdminBackupsController {
  private state: BackupsState = {
    overview: empty(), settings: empty(), list: empty(), detail: empty(), detailId: null, query: { page: 1, page_size: 20 },
    changes: {}, invalidNumbers: [], saving: false, starting: false, downloading: null, reviewRequired: false,
    manualUncertain: false, autoRefresh: true, denied: false, notice: null, error: null,
  }
  private active = false
  private flights = new Map<Channel, AbortController>()
  private generations = new Map<Channel, number>()
  private listeners = new Set<() => void>()
  private manualIntent: BackupManualRequest | null = null
  constructor(private loaders = adminBackupsApi, private rights = defaultRights, private newId: () => string = newManualRequestId) {}
  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { if (this.rights.read()) this.active = true }
  deactivate() {
    this.active = false
    channels.forEach((key) => this.invalidate(key))
    this.manualIntent = null
    this.update({ overview: empty(), settings: empty(), list: empty(), detail: empty(), detailId: null,
      changes: {}, invalidNumbers: [], saving: false, starting: false, downloading: null, reviewRequired: false,
      manualUncertain: false, notice: null, error: null })
  }
  revoke() { this.deactivate(); this.update({ denied: true, autoRefresh: false }) }
  revokeConfigure() {
    this.invalidate('settings'); this.invalidate('save')
    this.update({ settings: empty(), changes: {}, invalidNumbers: [], saving: false, reviewRequired: false })
  }
  revokeActions() {
    this.revokeConfigure()
    this.invalidate('run'); this.invalidate('download')
    this.manualIntent = null
    this.update({ starting: false, downloading: null, manualUncertain: false, notice: null })
  }
  setAutoRefresh(value: boolean) { this.update({ autoRefresh: value }) }
  refreshAll = () => { void this.refreshOverview(); void this.refreshList(); void this.refreshDetail() }
  refreshOverview = () => this.job('overview', (signal) => this.loaders.overview(signal),
    () => this.update({ overview: { ...this.state.overview, loading: true } }),
    (data) => this.update({ overview: { data, loading: false, error: null } }),
    (err) => this.update({ overview: { ...this.state.overview, loading: false, error: errorMessage(err) } }))
  refreshList = () => {
    const query = this.state.query
    return this.job('list', async (signal) => {
      let data = await this.loaders.list(query, signal)
      const page = Math.min(query.page ?? 1, Math.max(1, Math.ceil(data.total / (query.page_size ?? 20))))
      if (page !== query.page) data = await this.loaders.list({ ...query, page }, signal)
      return { data, page }
    }, () => this.update({ list: { ...this.state.list, loading: true } }),
    ({ data, page }) => this.update({ list: { data, loading: false, error: null }, query: { ...query, page } }),
    (err) => this.update({ list: { ...this.state.list, loading: false, error: errorMessage(err) } }))
  }
  setQuery(query: BackupRunFilters) {
    if (!this.usable()) return
    this.invalidate('list'); this.closeDetail()
    this.update({ query: { page: 1, page_size: 20, ...query }, list: empty() }); void this.refreshList()
  }
  openDetail(id: number) {
    if (!this.usable()) return
    this.closeDetail(); this.update({ detailId: id }); void this.refreshDetail()
  }
  closeDetail = () => { this.invalidate('detail'); this.update({ detailId: null, detail: empty() }) }
  refreshDetail = () => {
    const id = this.state.detailId
    if (!id) return Promise.resolve()
    return this.job('detail', (signal) => this.loaders.get(id, signal),
      () => this.update({ detail: { ...this.state.detail, loading: true } }),
      (data) => this.update({ detail: { data, loading: false, error: null } }),
      (err) => this.update({ detail: { data: null, loading: false, error: errorMessage(err) } }))
  }
  refreshSettings = () => {
    if (!this.rights.configure() || this.state.saving) return Promise.resolve()
    return this.job('settings', (signal) => this.loaders.settings(signal),
      () => this.update({ settings: { ...this.state.settings, loading: true } }), (data) => {
        const dirty = Object.keys(this.state.changes).length > 0 || this.state.invalidNumbers.length > 0
        const changed = dirty && this.state.settings.data?.revision !== data.revision
        this.update({ settings: { data: changed ? this.state.settings.data : data, loading: false, error: null },
          reviewRequired: this.state.reviewRequired || changed,
          ...(changed ? { notice: '配置版本已有变化，草稿已保留。请放弃草稿并重新载入后编辑。' } : {}) })
      }, (err) => this.update({ settings: { ...this.state.settings, loading: false, error: errorMessage(err) } }))
  }
  edit<K extends keyof BackupSettingsChanges>(key: K, value: BackupSettingsChanges[K]) {
    if (!this.usable() || !this.rights.configure() || !this.state.settings.data || this.state.saving || this.state.reviewRequired) return
    const changes = { ...this.state.changes, [key]: value }
    if (value === undefined || (typeof value === 'string' && key.endsWith('password') && value === '')) delete changes[key]
    this.update({ changes: backupChanges(changes), invalidNumbers: this.state.invalidNumbers.filter((item) => item !== key), error: null, notice: null })
  }
  editNumber(key: 'local_keep' | 's3_keep' | 'smtp_port', value: number | null) {
    if (!this.usable() || !this.rights.configure() || this.state.saving || this.state.reviewRequired) return
    const max = key === 'smtp_port' ? 65535 : 1000
    if (value === null || !Number.isSafeInteger(value) || value < 1 || value > max) this.update({ invalidNumbers: [...new Set([...this.state.invalidNumbers, key])] })
    else this.edit(key, value)
  }
  reloadSettings = async () => {
    if (!this.usable() || !this.rights.configure() || this.state.saving) return
    this.invalidate('settings')
    this.update({ settings: empty(), changes: {}, invalidNumbers: [], reviewRequired: false, error: null, notice: null })
    await this.refreshSettings()
  }
  save = async () => {
    const settings = this.state.settings.data
    if (!this.usable() || !this.rights.configure() || !settings || this.state.saving || this.state.settings.loading || this.state.reviewRequired || this.state.invalidNumbers.length) return
    const changes = backupChanges(this.state.changes)
    if (!Object.keys(changes).length) return
    const problem = backupDraftError(changes)
    if (problem) { this.update({ error: problem }); return }
    await this.job('save', (signal) => this.loaders.update({ revision: settings.revision, changes }, signal),
      () => this.update({ saving: true, error: null, notice: null }),
      (data) => { this.update({ settings: { data, loading: false, error: null }, changes: {}, invalidNumbers: [], saving: false, reviewRequired: false,
        notice: '配置已保存，对之后受理的备份生效；运行中的备份和旧文件保持原配置。' }); this.invalidate('overview'); void this.refreshOverview() },
      (err) => {
        const status = err instanceof ApiError ? err.status : 0
        const reviewRequired = status === 0 || status === 409 || status >= 500
        this.update({ saving: false, reviewRequired, error: status === 409 ? '配置版本冲突，已保留草稿；请刷新核实并重新载入，不能覆盖其他管理员修改。' : reviewRequired ? '保存结果尚不确定，请刷新核实后重新载入；不会自动重发。' : errorMessage(err) })
      })
  }
  startBackup = async () => {
    const overview = this.state.overview.data
    if (!this.usable() || !this.rights.run() || !overview || this.state.starting || (!this.manualIntent && (!overview.ready || overview.active_run))) return
    this.manualIntent ??= { request_id: this.newId(), settings_revision: overview.revision }
    const intent = this.manualIntent
    await this.job('run', (signal) => this.loaders.run(intent, signal),
      () => this.update({ starting: true, error: null, notice: null }),
      (data) => { this.manualIntent = null; this.update({ starting: false, manualUncertain: false, notice: `备份 #${data.id} 已受理，请查看实际执行结果。` }); this.refreshAll() },
      (err) => {
        const status = err instanceof ApiError ? err.status : 0
        const uncertain = status === 0 || status >= 500
        if (!uncertain) this.manualIntent = null
        this.update({ starting: false, manualUncertain: uncertain, error: uncertain ? '受理结果尚不确定。请先刷新核实；再次核实原请求时沿用同一请求标识，不会创建新意图。' : errorMessage(err) }); this.refreshAll()
      })
  }
  download = async (runId: number, destination: BackupDestination) => {
    if (!this.usable() || !this.rights.download() || this.state.downloading) return
    await this.job('download', (signal) => this.loaders.download(runId, destination, () => this.usable() && this.rights.download(), signal),
      () => this.update({ downloading: `${runId}:${destination}`, error: null, notice: null }),
      (started) => this.update({ downloading: null, notice: started ? '已开始下载。请在浏览器下载列表核对结果。' : null }),
      (err) => { this.update({ downloading: null, error: errorMessage(err) }); this.refreshAll() })
  }
  private update(patch: Partial<BackupsState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  private usable() { return this.active && !this.state.denied && this.rights.read() }
  private invalidate(channel: Channel) {
    this.generations.set(channel, (this.generations.get(channel) ?? 0) + 1)
    this.flights.get(channel)?.abort(); this.flights.delete(channel)
  }
  private async job<T>(channel: Channel, loader: (signal: AbortSignal) => Promise<T>, loading: () => void, success: (data: T) => void, failure: (err: unknown) => void) {
    if (!this.usable() || this.flights.has(channel)) return
    const controller = new AbortController()
    const generation = (this.generations.get(channel) ?? 0) + 1
    this.generations.set(channel, generation); this.flights.set(channel, controller)
    const authorized = () => ['save', 'settings'].includes(channel) ? this.rights.configure() : channel === 'run' ? this.rights.run() : channel === 'download' ? this.rights.download() : true
    const valid = () => this.usable() && authorized() && !controller.signal.aborted && this.generations.get(channel) === generation
    loading()
    try { const data = await loader(controller.signal); if (valid()) success(data) }
    catch (err) {
      if (!valid()) return
      if (err instanceof ApiError && err.status === 401) this.revoke()
      else if (err instanceof ApiError && err.status === 403) {
        if (channel === 'settings' || channel === 'save') { this.revokeConfigure(); this.update({ error: '配置权限已失效，连接参数和秘密草稿已清空。' }) }
        else if (channel === 'run' || channel === 'download') { this.revokeActions(); this.update({ error: '操作权限已失效，请刷新权限后重试。' }) }
        else this.revoke()
      } else failure(err)
    } finally { if (this.flights.get(channel) === controller) this.flights.delete(channel) }
  }
}
