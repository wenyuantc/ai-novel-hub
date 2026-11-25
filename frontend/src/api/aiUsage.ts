import { request, type RequestOptions } from './client'
import type { ModelType, ProviderName, UsagePricePeriod } from './types'

export type UsageCurrency = 'CNY' | 'USD'
export type UsageSource = 'provider' | 'estimated' | 'unknown'
export type UsageStatus = 'pending' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
export type UsagePricingStatus = 'pending' | 'priced' | 'missing_price' | 'missing_usage' | 'unconfirmed' | 'private'
export type UsageScope = { kind: 'account' } | { kind: 'admin' } | { kind: 'project'; projectId: number }
export type BudgetScope = Exclude<UsageScope, { kind: 'admin' }>

export interface UsageFilters {
  project_id?: number
  chapter_id?: number
  config_id?: number
  actor_id?: number
  payer_id?: number
  provider?: ProviderName
  model?: string
  kind?: string
  model_type?: ModelType
  status?: UsageStatus
  from?: string
  to?: string
  page?: number
  page_size?: number
}

export interface UsageRow {
  id: string
  started_at: string
  actor_id: number | null
  actor_name: string | null
  payer_id: number | null
  payer_name: string | null
  project_id: number | null
  project_title: string | null
  project_state: 'linked' | 'none' | 'deleted' | 'private'
  chapter_id: number | null
  task_id: number | null
  provider: string | null
  model: string | null
  kind: string
  model_type: ModelType
  status: UsageStatus
  input_tokens: number | null
  cached_input_tokens: number | null
  output_tokens: number | null
  image_count: number | null
  input_source: UsageSource
  output_source: UsageSource
  image_source: UsageSource
  currency: UsageCurrency | null
  estimated_cost: string | null
  pricing_status: UsagePricingStatus
  price_period?: UsagePricePeriod | null
  billing_source: 'own' | 'author' | 'other' | 'unknown'
  input_per_million: string | null
  cached_input_per_million?: string | null
  output_per_million: string | null
  per_image: string | null
}

export interface UsageSummary {
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  image_count: number
  unknown_usage_calls: number
  unpriced_calls: number
  costs: { currency: UsageCurrency; amount: string; calls: number }[]
}

export interface UsagePage {
  items: UsageRow[]
  total: number
  page: number
  page_size: number
  tracking_since: string | null
  summary: UsageSummary
}

export interface UsagePriceRates {
  input_per_million: string | null
  cached_input_per_million?: string | null
  output_per_million: string | null
  per_image: string | null
}

export interface UsageOffPeakWindow {
  start: string
  end: string
}

export interface UsageOffPeakPrice extends UsagePriceRates, UsageOffPeakWindow {
  timezone: string
  additional_windows?: UsageOffPeakWindow[]
}

export interface UsageModelPrice extends UsagePriceRates {
  currency: UsageCurrency
  off_peak?: UsageOffPeakPrice | null
}

export interface UsagePriceRevision {
  version: string
  provider: ProviderName
  model: string
  model_type: ModelType
  price: UsageModelPrice | null
}

export interface UsageBudgetInput { limit: string; threshold_percent: number }

export interface UsageBudget extends UsageBudgetInput {
  instance_key: string
  scope: BudgetScope['kind']
  currency: UsageCurrency
  spent: string
  unpriced_calls: number
  state: 'normal' | 'near' | 'exceeded'
  period_start: string | null
  period_end: string | null
}

type ReadOptions = Pick<RequestOptions, 'signal' | 'isCurrent'>
type WriteOptions = ReadOptions & Required<Pick<RequestOptions, 'editorActor'>>

function usagePath(scope: UsageScope, chapterId?: number): string {
  if (scope.kind === 'admin') return '/admin/ai-usage'
  if (scope.kind === 'account') return '/ai-usage'
  const project = `/projects/${scope.projectId}`
  return chapterId === undefined ? `${project}/ai-usage` : `${project}/chapters/${chapterId}/ai-usage`
}

export const aiUsageApi = {
  list(scope: UsageScope, filters: UsageFilters, options: ReadOptions) {
    const query = { ...filters }
    if (scope.kind === 'project') {
      delete query.project_id
      delete query.chapter_id
    }
    return request<UsagePage>(usagePath(scope, filters.chapter_id), { query, ...options })
  },
  getBudgets: (scope: BudgetScope, options: ReadOptions, budgetKey?: string) =>
    request<UsageBudget[]>(`${usagePath(scope)}/budgets`, { ...options, query: { budget_key: budgetKey } }),
  setBudget: (scope: BudgetScope, currency: UsageCurrency, body: UsageBudgetInput | null, options: WriteOptions, budgetKey?: string) =>
    request<UsageBudget | null>(`${usagePath(scope)}/budgets/${currency}`, { method: 'PUT', body, ...options, query: { budget_key: budgetKey } }),
  getPrice: (id: number, options: ReadOptions) => request<UsagePriceRevision>(`/ai-configs/${id}/price`, options),
  setPrice: (id: number, version: string, price: UsageModelPrice | null, options: WriteOptions) =>
    request<UsagePriceRevision>(`/ai-configs/${id}/price`, { method: 'PUT', body: { expected_version: version, price }, ...options }),
}
