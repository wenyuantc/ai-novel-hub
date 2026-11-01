import { ApiError, errorMessage } from '../../api/client'
import { systemSettingsApi } from '../../api/systemSettings'
import type { AuthConfig, SystemSettingChanges, SystemSettingIntKey, SystemSettingKey, SystemSettings } from '../../api/types'
import { publicConfigFromSettings, settingIntegerMaximum } from '../../utils/systemSettings'

export interface SystemSettingsState {
  data: SystemSettings | null
  changes: SystemSettingChanges
  invalidNumbers: SystemSettingIntKey[]
  loading: boolean
  saving: boolean
  denied: boolean
  reviewRequired: boolean
  error: string | null
  notice: string | null
}

/** 本页单一请求/草稿所有者；读取不覆盖草稿，写请求不自动重放。 */
export class SystemSettingsController {
  private state: SystemSettingsState = { data: null, changes: {}, invalidNumbers: [], loading: false, saving: false, denied: false, reviewRequired: false, error: null, notice: null }
  private listeners = new Set<() => void>()
  private active = false
  private generation = 0
  private request: AbortController | null = null

  constructor(private readonly loaders = systemSettingsApi, private readonly allowed: () => boolean = () => true, private readonly applyPublic: (config: AuthConfig) => void = () => {}) {}

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.active = true }
  deactivate() {
    const saving = this.state.saving
    this.active = false
    this.cancel()
    this.update({ loading: false, saving: false, ...(saving ? { reviewRequired: true, error: '保存结果尚不能确认，请先刷新核实当前状态，不要重复提交。' } : {}) })
  }
  revoke = () => { this.active = false; this.cancel(); this.update({ data: null, changes: {}, invalidNumbers: [], denied: true, loading: false, saving: false, error: null, notice: null }) }

  private update(patch: Partial<SystemSettingsState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach((listener) => listener()) }
  private cancel() { this.generation += 1; this.request?.abort(); this.request = null }
  private valid(generation: number, controller: AbortController) {
    if (!this.allowed()) { this.revoke(); return false }
    return this.active && !this.state.denied && generation === this.generation && !controller.signal.aborted
  }
  private canUse() { if (!this.allowed()) { this.revoke(); return false }; return this.active && !this.state.denied }

  refresh = async () => {
    if (!this.canUse() || this.state.loading || this.state.saving) return
    const controller = new AbortController()
    const generation = ++this.generation
    this.request = controller
    this.update({ loading: true, error: null })
    try {
      const data = await this.loaders.get(controller.signal)
      if (!this.valid(generation, controller)) return
      const dirty = Object.keys(this.state.changes).length > 0 || this.state.invalidNumbers.length > 0
      if (dirty && this.state.data && data.revision !== this.state.data.revision) {
        this.update({ loading: false, reviewRequired: true, notice: '有效配置已有变化，已保留未保存修改。请重新载入并核对后编辑，不能覆盖其他管理员的更新。' })
      } else {
        this.update({ data, loading: false, reviewRequired: false, notice: dirty ? '已核实当前有效配置，未保存修改仍保留；请核对后再保存。' : null })
        this.applyPublic(publicConfigFromSettings(data))
      }
    } catch (err) {
      if (!this.valid(generation, controller)) return
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) this.revoke()
      else this.update({ loading: false, error: errorMessage(err) })
    } finally {
      if (this.request === controller) this.request = null
    }
  }

  edit<K extends SystemSettingKey>(key: K, value: SystemSettingChanges[K]) {
    if (!this.canUse() || this.state.saving || this.state.reviewRequired || !this.state.data) return
    const item = this.state.data.items.find((entry) => entry.key === key)
    if (!item) return
    const changes = { ...this.state.changes }
    if (value === item.value || value === undefined) delete changes[key]
    else changes[key] = value
    this.update({ changes, invalidNumbers: this.state.invalidNumbers.filter((entry) => entry !== key), error: null, notice: null })
  }

  editNumber(key: SystemSettingIntKey, value: number | null) {
    if (!this.canUse() || this.state.saving || this.state.reviewRequired) return
    if (value === null || !Number.isSafeInteger(value) || value < 1 || value > settingIntegerMaximum(key)) {
      this.update({ invalidNumbers: [...new Set([...this.state.invalidNumbers, key])], notice: null })
    } else this.edit(key, value)
  }

  restore(key: SystemSettingKey) {
    if (!this.canUse() || this.state.saving || this.state.reviewRequired || !this.state.data) return
    const item = this.state.data.items.find((entry) => entry.key === key)
    if (!item) return
    const changes = { ...this.state.changes }
    if (item.overridden) changes[key] = null
    else delete changes[key]
    this.update({ changes, invalidNumbers: this.state.invalidNumbers.filter((entry) => entry !== key), error: null, notice: null })
  }

  reload = async () => {
    if (!this.canUse() || this.state.saving) return
    this.cancel()
    this.update({ data: null, changes: {}, invalidNumbers: [], loading: false, reviewRequired: false, error: null, notice: null })
    await this.refresh()
  }

  save = async () => {
    if (!this.canUse() || !this.state.data || this.state.loading || this.state.saving || this.state.reviewRequired || this.state.invalidNumbers.length || !Object.keys(this.state.changes).length) return
    this.cancel()
    const controller = new AbortController()
    const generation = ++this.generation
    this.request = controller
    const body = { revision: this.state.data.revision, changes: { ...this.state.changes } }
    this.update({ saving: true, error: null, notice: null })
    try {
      const data = await this.loaders.update(body, controller.signal)
      if (!this.valid(generation, controller)) return
      this.applyPublic(publicConfigFromSettings(data))
      this.update({ data, changes: {}, invalidNumbers: [], saving: false, reviewRequired: false, notice: '设置已保存并立即生效，无需重启。运行中的任务和已发出的外部调用不会被强制中断。' })
    } catch (err) {
      if (!this.valid(generation, controller)) return
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) this.revoke()
      else {
        const conflict = err instanceof ApiError && err.status === 409
        const uncertain = !(err instanceof ApiError) || err.status === 0 || err.status >= 500
        this.update({ saving: false, reviewRequired: conflict || uncertain, error: conflict ? '配置已被其他管理员更新。未保存修改已保留，请刷新核对；不会自动覆盖。' : uncertain ? '保存结果尚不能确认，请先刷新核实当前状态，不要重复提交。' : errorMessage(err) })
      }
    } finally {
      if (this.request === controller) this.request = null
    }
  }
}
