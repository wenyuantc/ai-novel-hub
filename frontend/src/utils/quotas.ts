import type { QuotaConfig, QuotaItem } from '../api/types'

export interface Draft { mode: QuotaConfig['mode']; value: number | null }
export const draftOf = (config: QuotaConfig): Draft => ({ mode: config.mode, value: config.mode === 'limited' ? config.value : 0 })
export function quotaConfig(draft: Draft): QuotaConfig | null {
  if (draft.mode !== 'limited') return { mode: draft.mode }
  return draft.value !== null && Number.isSafeInteger(draft.value) && draft.value >= 0 && draft.value <= 2_147_483_647 ? { mode: 'limited', value: draft.value } : null
}
export function quotaDescription(item: QuotaItem): string {
  const value = item.effective_limit === null ? '不限额' : String(item.effective_limit)
  return item.config.mode === 'inherit' ? `继承默认：${value}` : item.config.mode === 'unlimited' ? '用户指定：不限额' : `用户指定：${value}`
}

