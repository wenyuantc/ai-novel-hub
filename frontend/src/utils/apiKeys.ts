import dayjs from 'dayjs'
import type { ApiKey, ApiKeyCreate, ApiKeyUpdate } from '../api/types'

export type ApiKeyExpiryChoice = 'keep' | '30' | '90' | '180' | 'custom' | 'never'

export interface ApiKeyFormValues {
  name: string
  all_projects: boolean
  project_ids: number[]
  expiry: ApiKeyExpiryChoice
  /** 表单输入的本地时间或带时区的时间，不是服务端返回值。 */
  custom_expires_at?: string
}

type ApiKeyScope = Pick<ApiKey, 'all_projects' | 'project_ids'>

/** 可见ID按集合比较；全部作品模式不受表单残留选择影响。 */
export function hasApiKeyScopeChanged(values: ApiKeyScope, original: ApiKeyScope): boolean {
  if (values.all_projects !== original.all_projects) return true
  if (values.all_projects) return false
  const selected = [...new Set(values.project_ids)].sort((a, b) => a - b)
  const previous = [...new Set(original.project_ids)].sort((a, b) => a - b)
  return selected.length !== previous.length || selected.some((id, index) => id !== previous[index])
}

/** 保持当前有效期时省略字段，永久则显式 null。 */
function buildMetadata(values: ApiKeyFormValues, now: number): Pick<ApiKeyCreate, 'name' | 'expires_at'> {
  const name = values.name.trim()
  if (!name) throw new Error('请输入密钥名称')
  const body = { name }
  if (values.expiry === 'keep') return body
  if (values.expiry === 'never') return { ...body, expires_at: null }
  if (values.expiry === 'custom') {
    if (!values.custom_expires_at) throw new Error('请选择到期时间')
    const date = dayjs(values.custom_expires_at)
    if (!date.isValid()) throw new Error('请选择有效的到期时间')
    if (date.valueOf() <= now) throw new Error('到期时间必须晚于当前时间')
    return { ...body, expires_at: date.toISOString() }
  }
  return { ...body, expires_at: new Date(now + Number(values.expiry) * 24 * 60 * 60 * 1000).toISOString() }
}

/** 创建必须明确选择范围；指定作品不可为空。 */
export function buildApiKeyInput(values: ApiKeyFormValues, now = Date.now()): ApiKeyCreate {
  const body = buildMetadata(values, now)
  const projectIds = values.all_projects ? [] : [...new Set(values.project_ids)]
  if (!values.all_projects && projectIds.length === 0) throw new Error('至少选择一部小说')
  return { ...body, all_projects: values.all_projects, project_ids: projectIds }
}

/** 未调整可见范围时不重写grant，以保留暂不可见的回收站作品授权。 */
export function buildApiKeyUpdate(values: ApiKeyFormValues, original: ApiKeyScope, now = Date.now()): ApiKeyUpdate {
  return hasApiKeyScopeChanged(values, original) ? buildApiKeyInput(values, now) : buildMetadata(values, now)
}
