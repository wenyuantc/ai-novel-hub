import dayjs from 'dayjs'
import type { ApiKeyCreate } from '../api/types'

export type ApiKeyExpiryChoice = 'keep' | '30' | '90' | '180' | 'custom' | 'never'

export interface ApiKeyFormValues {
  name: string
  all_projects: boolean
  project_ids: number[]
  expiry: ApiKeyExpiryChoice
  /** 表单输入的本地时间或带时区的时间，不是服务端返回值。 */
  custom_expires_at?: string
}

/** 表单整体提交；保持当前有效期时省略字段，永久则显式 null。 */
export function buildApiKeyInput(values: ApiKeyFormValues, now = Date.now()): ApiKeyCreate {
  const name = values.name.trim()
  if (!name) throw new Error('请输入密钥名称')
  const projectIds = values.all_projects ? [] : [...new Set(values.project_ids)]
  if (!values.all_projects && projectIds.length === 0) throw new Error('至少选择一部小说')
  const body: ApiKeyCreate = { name, all_projects: values.all_projects, project_ids: projectIds }
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
