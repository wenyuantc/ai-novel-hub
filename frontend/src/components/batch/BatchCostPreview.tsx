import { Alert, Space, Typography } from 'antd'
import type { BatchCostEstimate } from '../../api/types'
import { DEFAULT_USAGE_PRICE_TIMEZONE, USAGE_PRICE_PERIOD_LABELS } from '../aiUsage/usagePresentation'

const statusText: Record<BatchCostEstimate['status'], string> = {
  estimated: '预计费用',
  missing_price: '模型单价缺失，暂时无法计算费用',
  unavailable: '模型配置暂时无法用于费用预估',
  author_paid: '本批使用作者授权的模型，费用由作者承担',
}

export default function BatchCostPreview({ estimate }: { estimate: BatchCostEstimate }) {
  const showPriceDetails = estimate.status !== 'author_paid'
  const period = showPriceDetails ? estimate.price_period : null
  const timezone = estimate.price_timezone === DEFAULT_USAGE_PRICE_TIMEZONE ? '北京时间' : estimate.price_timezone
  const steps = estimate.steps.map((step) => {
    const label = step.kind === 'generation' ? '正文生成' : '自动分析'
    return `${label} ${step.calls} 次${step.estimated_amount === null ? '' : `（${estimate.currency} ${step.estimated_amount}）`}`
  })
  return (
    <Alert
      type="info"
      showIcon
      message={estimate.estimated_amount === null
        ? statusText[estimate.status] : `预计费用 ${estimate.currency} ${estimate.estimated_amount}`}
      description={(
        <Space direction="vertical" size={4}>
          <Typography.Text>{steps.join('、')}</Typography.Text>
          {showPriceDetails && estimate.assumes_uncached_input && (
            <Typography.Text type="secondary">预估按普通输入价计算，实际缓存命中会影响费用。</Typography.Text>
          )}
          <Typography.Text type="secondary">
            按目标字数、提示词与上下文大小估算。实际用量随生成结果变化，重试、续写和向量调用可能增加费用。
          </Typography.Text>
          {(period === 'peak' || period === 'off_peak') && (
            <Typography.Text type="secondary">
              本次预估采用{USAGE_PRICE_PERIOD_LABELS[period]}{timezone ? `（${timezone}）` : ''}。排队或执行跨时段时，实际费用按每次调用的发送时刻计价。
            </Typography.Text>
          )}
          {estimate.budgets.map((budget) => (
            <Typography.Text key={budget.scope} type={budget.state === 'normal' ? undefined : 'warning'}>
              {budget.scope === 'project' ? '全书预算' : '本月账号预算'}预计使用 {estimate.currency}{' '}
              {budget.projected_spent} / {budget.limit}
              {budget.state === 'exceeded' && '，预计达到或超过预算'}
              {budget.state === 'near' && '，预计达到提醒阈值'}
              {budget.unpriced_calls > 0 && `，另有 ${budget.unpriced_calls} 次费用未知`}
            </Typography.Text>
          ))}
        </Space>
      )}
    />
  )
}
