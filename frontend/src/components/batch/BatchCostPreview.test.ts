import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { BatchCostEstimate } from '../../api/types'
import BatchCostPreview from './BatchCostPreview'

const baseline: BatchCostEstimate = {
  status: 'estimated', currency: 'CNY', estimated_amount: '1.25', steps: [], budgets: [],
}

describe('批量费用预估的计价档位', () => {
  it('展示空闲价、预估时区及排队或执行跨时段的提示', () => {
    const html = renderToStaticMarkup(createElement(BatchCostPreview, {
      estimate: { ...baseline, price_period: 'off_peak', price_timezone: 'Asia/Shanghai' },
    }))
    expect(html).toContain('空闲价')
    expect(html).toContain('北京时间')
    expect(html).toContain('排队或执行跨时段')
  })

  it('固定单价沿用原预估展示，作者承担时隐藏档位与时区', () => {
    const fixed = renderToStaticMarkup(createElement(BatchCostPreview, {
      estimate: { ...baseline, price_period: 'fixed' },
    }))
    expect(fixed).toContain('预计费用 CNY 1.25')
    expect(fixed).not.toContain('排队或执行跨时段')
    expect(fixed).not.toContain('普通输入价')
    const privatePrice = renderToStaticMarkup(createElement(BatchCostPreview, {
      estimate: {
        ...baseline, status: 'author_paid', currency: null, estimated_amount: null,
        price_period: 'peak', price_timezone: 'Asia/Tokyo', assumes_uncached_input: true,
      },
    }))
    expect(privatePrice).toContain('费用由作者承担')
    expect(privatePrice).not.toContain('高峰价')
    expect(privatePrice).not.toContain('Asia/Tokyo')
    expect(privatePrice).not.toContain('普通输入价')
  })

  it('有缓存命中价时说明批量估算按普通输入价', () => {
    const html = renderToStaticMarkup(createElement(BatchCostPreview, {
      estimate: { ...baseline, price_period: 'off_peak', assumes_uncached_input: true },
    }))
    expect(html).toContain('预估按普通输入价计算')
    expect(html).toContain('实际缓存命中会影响费用')
  })
})
