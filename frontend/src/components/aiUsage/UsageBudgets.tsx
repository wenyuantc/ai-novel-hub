import { Alert, Button, Card, Form, Input, InputNumber, Popconfirm, Progress, Skeleton, Space, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { aiUsageApi, type BudgetScope, type UsageBudget, type UsageBudgetInput, type UsageCurrency } from '../../api/aiUsage'
import { useAiUsageMutation, useAiUsageRequest, type UsageReadOptions, type UsageSession } from './useAiUsageRequest'
import { usageMoney, USAGE_CURRENCIES, validUsageDecimal } from './usagePresentation'

const budgetState = {
  normal: { label: '预算内', color: 'success' },
  near: { label: '已达提醒阈值', color: 'warning' },
  exceeded: { label: '已达到或超出预算', color: 'error' },
} as const

interface BudgetProps { scope: BudgetScope; session: UsageSession; canAccess?: () => boolean }

function BudgetCard({ scope, session, canAccess, currency, label, initial }: BudgetProps & {
  currency: UsageCurrency
  label: string
  initial: UsageBudget | null
}) {
  const [budget, setBudget] = useState(initial)
  const [form] = Form.useForm<UsageBudgetInput>()
  const { commit, loading } = useAiUsageMutation(session, canAccess)
  const status = budget === null ? null : budgetState[budget.state]
  const save = (values: UsageBudgetInput | null) => {
    const body = values ? { ...values, limit: values.limit.trim() } : null
    void commit((options) => aiUsageApi.setBudget(scope, currency, body, options), (updated) => {
      setBudget(updated)
      form.setFieldsValue({ limit: updated?.limit ?? '', threshold_percent: updated?.threshold_percent ?? 80 })
    }, body ? '预算已保存' : '预算已移除')
  }

  return <Card size="small" title={label} extra={status ? <Tag color={status.color}>{status.label}</Tag> : <Typography.Text type="secondary">未设置预算</Typography.Text>}>
    {budget && <>
      <Typography.Text strong>{usageMoney(budget.spent, currency)} / {usageMoney(budget.limit, currency)}</Typography.Text>
      <Progress percent={Math.min(100, Math.round(Number(budget.spent) * 100 / Number(budget.limit)))} showInfo={false}
        status={budget.state === 'exceeded' ? 'exception' : undefined} aria-label="已记录费用占预算比例" />
      {budget.unpriced_calls > 0 && <Alert type="warning" showIcon className="ai-usage-inline-alert" message={`${budget.unpriced_calls} 次调用的费用未知，已用金额可能不完整`} />}
    </>}
    <Form form={form} layout="vertical" disabled={loading} initialValues={{ limit: budget?.limit ?? '', threshold_percent: budget?.threshold_percent ?? 80 }} onFinish={save}>
      <Form.Item label="预算额度" name="limit" rules={[
        { required: true, message: '请输入预算额度' },
        { validator: (_, value: string) => validUsageDecimal(value?.trim() ?? '', 1_000_000_000, false)
          ? Promise.resolve() : Promise.reject(new Error('请输入大于 0 且不超过 10 亿的金额，最多 9 位小数')) },
      ]}><Input inputMode="decimal" maxLength={20} placeholder={`金额（${currency}）`} /></Form.Item>
      <Form.Item label="提醒阈值（%）" name="threshold_percent" rules={[
        { required: true, message: '请输入提醒阈值' },
        { type: 'integer', min: 1, max: 100, message: '请输入 1 到 100 的整数' },
      ]}><InputNumber className="ai-usage-threshold" min={1} max={100} precision={0} /></Form.Item>
      <Space wrap>
        <Button type="primary" htmlType="submit" loading={loading}>保存预算</Button>
        {budget && <Popconfirm title={`移除 ${currency} 预算？`} description="调用明细和已有费用仍可查询。" okText="移除" cancelText="取消" onConfirm={() => save(null)}>
          <Button danger disabled={loading}>移除预算</Button>
        </Popconfirm>}
      </Space>
    </Form>
  </Card>
}

export default function UsageBudgets({ scope, session, canAccess }: BudgetProps) {
  const load = useCallback((options: UsageReadOptions) => aiUsageApi.getBudgets(scope, options), [scope])
  const { data, error, refresh, loading } = useAiUsageRequest(load, session, canAccess)
  return <Card size="small" title={scope.kind === 'project' ? '作品预算' : '账号预算'}>
    <Typography.Paragraph type="secondary">
      {scope.kind === 'project' ? '按全书累计作者承担的费用，跨月继续累加。' : '按北京时间自然月统计本人承担的费用，每月 1 日 00:00 开始新周期。'}
    </Typography.Paragraph>
    {error ? <Alert type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} />
      : loading || !data ? <Skeleton active paragraph={{ rows: 2 }} />
        : <div className="ai-usage-budget-grid">{USAGE_CURRENCIES.map(({ value, label }) => (
          <BudgetCard key={value} label={label} currency={value} initial={data.find((item) => item.currency === value) ?? null}
            scope={scope} session={session} canAccess={canAccess} />
        ))}</div>}
  </Card>
}
