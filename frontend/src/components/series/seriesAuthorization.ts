/** 系列邀请重授时同步淘汰旧请求；普通内容 revision 不会改变授权代次。 */
export class SeriesAuthorizationScope {
  private version: string | null | undefined
  private epoch = 0
  private listeners = new Set<() => void>()

  constructor(version?: string | null) { this.version = version }
  capture = () => this.epoch
  current = (ticket: number) => ticket === this.epoch
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  adopt = (version: string | null) => {
    if (this.version === undefined) { this.version = version; return }
    if (this.version === version) return
    this.version = version
    this.epoch += 1
    for (const listener of this.listeners) listener()
  }
}

export function validSeriesAuthorizationVersion(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && /^[a-f0-9]{64}$/.test(value))
}

export function validSeriesAccess(value: { is_owner: boolean; authorization_version: string | null }): boolean {
  return value.is_owner === true ? value.authorization_version === null
    : value.is_owner === false && typeof value.authorization_version === 'string'
      && validSeriesAuthorizationVersion(value.authorization_version)
}
import type { ProjectAccess } from '../../api/types'


export function validProjectSeriesAccess(role: ProjectAccess['role'], value: unknown): value is string | null {
  if (!validSeriesAuthorizationVersion(value)) return false
  return role === 'owner' ? value === null : role !== null && typeof value === 'string'
}
