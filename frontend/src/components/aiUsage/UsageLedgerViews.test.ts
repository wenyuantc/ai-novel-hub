import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { UsagePage, UsageRow } from '../../api/aiUsage'
import { UsageLedgerSummary, UsageRowDetail } from './UsageLedgerViews'

vi.mock('antd', async (importOriginal) => ({
  ...await importOriginal<typeof import('antd')>(),
  Drawer: ({ children }: { children: ReactNode }) => children,
}))

const baseline: UsageRow = {
  id: 'test-call', started_at: '2026-11-18T05:00:00Z', actor_id: 1, actor_name: null, payer_id: 1, payer_name: null,
  project_id: null, project_title: null, project_state: 'none', chapter_id: null, task_id: null,
  provider: 'openai_compat', model: 'test-model', kind: 'chapter.generate', model_type: 'text', status: 'succeeded',
  input_tokens: 500, cached_input_tokens: 300, output_tokens: 100, image_count: null,
  input_source: 'provider', output_source: 'provider', image_source: 'unknown',
  currency: 'CNY', estimated_cost: '0.001', pricing_status: 'priced', price_period: 'off_peak', billing_source: 'own',
  input_per_million: '1', cached_input_per_million: '0', output_per_million: '2', per_image: null,
}

function renderDetail(row: UsageRow) {
  return renderToStaticMarkup(createElement(UsageRowDetail, { row, scope: { kind: 'account' }, onClose: () => {} }))
}

describe('调用账单的缓存命中', () => {
  it('在输入总量中展示命中用量、归属档位及零元单价', () => {
    const data: UsagePage = {
      items: [baseline], total: 1, page: 1, page_size: 20, tracking_since: null,
      summary: {
        input_tokens: 500, cached_input_tokens: 300, output_tokens: 100, image_count: 0,
        costs: [], unknown_usage_calls: 0, unpriced_calls: 0,
      },
    }
    const summary = renderToStaticMarkup(createElement(UsageLedgerSummary, { data }))
    expect(summary).toContain('已记录输入 500 tokens（其中缓存命中 300 tokens）')
    const detail = renderDetail(baseline)
    expect(detail).toContain('其中缓存命中 300（模型返回）')
    expect(detail).toContain('空闲价')
    expect(detail).toContain('缓存命中输入 / 百万 tokens')
    expect(detail).toContain('CNY 0<')
  })

  it('未知命中量有普通输入费率提示，零命中量保持已知', () => {
    const unknown = renderDetail({ ...baseline, cached_input_tokens: null })
    expect(unknown).toContain('其中缓存命中 未知')
    expect(unknown).toContain('按普通输入单价估算')
    const zero = renderDetail({ ...baseline, cached_input_tokens: 0 })
    expect(zero).toContain('其中缓存命中 0（模型返回）')
    expect(zero).not.toContain('按普通输入单价估算')
  })

  it('缺少命中价时说明普通费率，作者承担时只显示用量', () => {
    const unconfigured = renderDetail({ ...baseline, cached_input_per_million: null })
    expect(unconfigured).toContain('未设置缓存命中价，按普通输入单价计算')
    const privatePrice = renderDetail({
      ...baseline, billing_source: 'author', pricing_status: 'private', currency: null, estimated_cost: null,
    })
    expect(privatePrice).toContain('其中缓存命中 300（模型返回）')
    expect(privatePrice).toContain('作者承担')
    expect(privatePrice).not.toContain('空闲价')
    expect(privatePrice).not.toContain('缓存命中输入 / 百万 tokens')
    expect(privatePrice).not.toContain('普通输入单价')
  })
})
