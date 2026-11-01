import type { AuthConfig, SystemSettingBoolItem, SystemSettingIntKey, SystemSettingItem, SystemSettingKey, SystemSettingSource, SystemSettings } from '../api/types'

/** 镜像后端SQL Integer计数的技术边界，不是另设用户配额。 */
export const MAX_OPEN_API_RATE_LIMIT_PER_MINUTE = 2_147_483_647

export function settingIntegerMaximum(key: SystemSettingIntKey): number {
  return key === 'open_api_rate_limit_per_minute' ? MAX_OPEN_API_RATE_LIMIT_PER_MINUTE : Number.MAX_SAFE_INTEGER
}

export const SYSTEM_SETTING_LABELS: Record<SystemSettingKey, string> = {
  registration_enabled: '开放自主注册',
  task_max_concurrency: '全局任务并发上限',
  task_max_per_user: '单用户任务并发上限',
  auto_analyze_enabled: '自动章节分析',
  mcp_enabled: 'MCP 插件能力',
  open_api_rate_limit_per_minute: '开放 API 每分钟限额',
}

export const SYSTEM_SETTING_SOURCE: Record<SystemSettingSource, string> = { admin: '后台覆盖', deployment: '部署配置', default: '代码默认' }

export const SYSTEM_SETTING_HINTS: Record<SystemSettingKey, string> = {
  registration_enabled: '仅控制自主注册，不影响登录或管理员创建账号。',
  task_max_concurrency: '全站同时运行的任务数。降低上限不会取消运行中的任务，其余任务继续排队。',
  task_max_per_user: '每位用户同时运行的任务数，仍受全局上限约束。',
  auto_analyze_enabled: '控制后续生成、润色、重写及批量生成的自动分析，不影响手动分析和已经排队的分析。',
  mcp_enabled: '关闭后阻止后续外部工具调用；已发出的调用允许结束，不能撤回已有外部副作用。',
  open_api_rate_limit_per_minute: '每把 API Key 每个 UTC 分钟的请求上限，最大 2,147,483,647（数据库计数边界）；修改不清空本分钟计数，不影响网页登录接口。',
}

export function isBooleanSetting(item: SystemSettingItem): item is SystemSettingBoolItem {
  return item.key === 'registration_enabled' || item.key === 'auto_analyze_enabled' || item.key === 'mcp_enabled'
}

export function settingValue(value: boolean | number | null): string {
  return value === null ? '沿用部署值' : typeof value === 'boolean' ? value ? '开启' : '关闭' : String(value)
}

/** 从安全管理投影应用公开两字段，不把管理来源/并发等扩散至公开store。 */
export function publicConfigFromSettings(data: SystemSettings): AuthConfig {
  const registration = data.items.find((item) => item.key === 'registration_enabled')
  const mcp = data.items.find((item) => item.key === 'mcp_enabled')
  if (!registration || !mcp || typeof registration.value !== 'boolean' || typeof mcp.value !== 'boolean') throw new Error('系统设置响应缺少公开开关')
  return { registration_enabled: registration.value, mcp_enabled: mcp.value }
}
