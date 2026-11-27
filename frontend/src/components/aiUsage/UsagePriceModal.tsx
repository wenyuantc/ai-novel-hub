import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { Alert, Button, Col, Form, Input, Modal, Popconfirm, Row, Select, Skeleton, Space, Switch, Typography } from 'antd'
import { useCallback } from 'react'
import { aiUsageApi, type UsageModelPrice, type UsageOffPeakWindow, type UsagePriceRates, type UsagePriceRevision } from '../../api/aiUsage'
import type { AIConfig, UsagePricePeriod } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useAiUsageMutation, useAiUsageRequest, type UsageReadOptions, type UsageSession } from './useAiUsageRequest'
import { DEFAULT_USAGE_PRICE_TIMEZONE, offPeakWindowsError, USAGE_CURRENCIES, USAGE_PRICE_CLOCK_TIME, USAGE_PRICE_PERIOD_LABELS, validUsageDecimal } from './usagePresentation'
import './aiUsage.css'

interface Props { config: AIConfig; session: UsageSession; onClose: () => void }
type PriceField = { name: keyof UsagePriceRates; label: string; optional?: boolean }
type PriceFormValues = Omit<UsageModelPrice, 'off_peak'> & {
  scheduled: boolean
  off_peak: UsagePriceRates & { timezone: string; windows: UsageOffPeakWindow[] }
}

const priceFields: Record<AIConfig['model_type'], readonly PriceField[]> = {
  text: [
    { name: 'input_per_million', label: '每百万输入 tokens' },
    { name: 'cached_input_per_million', label: '每百万缓存命中输入 tokens', optional: true },
    { name: 'output_per_million', label: '每百万输出 tokens' },
  ],
  embedding: [{ name: 'input_per_million', label: '每百万输入 tokens' }],
  video: [{ name: 'per_video_second', label: '每秒生成视频' }],
  image: [{ name: 'per_image', label: '每张返回图片' }],
}

const PRICE_FORM_NAME = 'usage-price'
const OFF_PEAK_WINDOWS_PATH: ['off_peak', 'windows'] = ['off_peak', 'windows']
const emptyWindow = { start: '', end: '' }

function configuredRates(values: UsagePriceRates, fields: readonly PriceField[]): UsagePriceRates {
  const rates: UsagePriceRates = { input_per_million: null, output_per_million: null, per_image: null }
  for (const { name } of fields) rates[name] = values[name]?.trim() || null
  return rates
}

function PriceInputs({ fields, period }: { fields: readonly PriceField[]; period: UsagePricePeriod }) {
  return fields.map(({ name, label, optional }) => (
    <Form.Item
      key={name}
      name={period === 'off_peak' ? ['off_peak', name] : name}
      label={period === 'fixed' ? label : `${USAGE_PRICE_PERIOD_LABELS[period]} · ${label}`}
      extra={optional ? '留空时，缓存命中按普通输入价计算。' : undefined}
      rules={[
        { required: !optional, message: '请输入单价' },
        { validator: (_, value: string | null | undefined) => {
          const amount = value?.trim() ?? ''
          return (optional && amount === '') || validUsageDecimal(amount, 1_000_000)
            ? Promise.resolve() : Promise.reject(new Error('请输入 0 到 100 万之间的金额，最多 9 位小数'))
        } },
      ]}
    >
      <Input inputMode="decimal" maxLength={17} />
    </Form.Item>
  ))
}

function PriceWindows({ loading }: { loading: boolean }) {
  return <Form.List name={OFF_PEAK_WINDOWS_PATH} rules={[{
    validator: async (_, windows: UsageOffPeakWindow[] | undefined) => {
      const error = offPeakWindowsError(windows)
      if (error) throw new Error(error)
    },
  }]}>
    {(fields, { add, remove }, { errors }) => (
      <fieldset id={`${PRICE_FORM_NAME}_${OFF_PEAK_WINDOWS_PATH.join('_')}`} className="ai-usage-price-windows">
        <legend>每日空闲时段</legend>
        {fields.map(({ name, key }) => {
          const windowLabel = `第 ${name + 1} 个空闲时段`
          return <Row key={key} wrap={false} gutter={8} className="ai-usage-price-window">
            <Col flex={1} style={{ minWidth: 0 }}>
              <Form.Item name={[name, 'start']} label="开始时间" rules={[
                { required: true, message: '请输入开始时间' },
                { pattern: USAGE_PRICE_CLOCK_TIME, message: '请使用 HH:MM 格式' },
              ]}>
                <Input type="time" aria-label={`${windowLabel}的开始时间`} />
              </Form.Item>
            </Col>
            <Col flex={1} style={{ minWidth: 0 }}>
              <Form.Item name={[name, 'end']} label="结束时间" rules={[
                { required: true, message: '请输入结束时间' },
                { pattern: USAGE_PRICE_CLOCK_TIME, message: '请使用 HH:MM 格式' },
              ]}>
                <Input type="time" aria-label={`${windowLabel}的结束时间`} />
              </Form.Item>
            </Col>
            <Col flex="none" className="ai-usage-price-window-remove">
              <Button type="text" icon={<DeleteOutlined />} aria-label={`移除${windowLabel}`} title={`移除${windowLabel}`}
                disabled={loading || fields.length === 1} onClick={() => remove(name)} />
            </Col>
          </Row>
        })}
        <Form.ErrorList errors={errors} />
        <Button icon={<PlusOutlined />} onClick={() => add({ ...emptyWindow })}>添加空闲时段</Button>
        <Typography.Paragraph type="secondary" className="ai-usage-price-window-hint">
          所有时段每天重复，共用空闲价。结束早于开始时跨至次日，其余时间使用高峰价。
        </Typography.Paragraph>
      </fieldset>
    )}
  </Form.List>
}

function UsagePriceForm({ config, session, onClose, current, reload }: Props & { current: UsagePriceRevision; reload: () => void }) {
  const { loading, commit } = useAiUsageMutation(session)
  const [form] = Form.useForm<PriceFormValues>()
  const savedOffPeak = current.price?.off_peak
  const scheduled = Form.useWatch('scheduled', form) ?? (savedOffPeak != null)
  const fields = priceFields[config.model_type]
  const save = (body: UsageModelPrice | null) => {
    void commit((options) => aiUsageApi.setPrice(config.id, current.version, body, options), onClose,
      body ? '模型单价已保存' : '模型单价已清除', reload)
  }
  const initialValues = {
    ...(current.price ?? { currency: 'CNY' }),
    scheduled: savedOffPeak != null,
    off_peak: {
      ...savedOffPeak,
      timezone: savedOffPeak?.timezone ?? DEFAULT_USAGE_PRICE_TIMEZONE,
      windows: savedOffPeak
        ? [{ start: savedOffPeak.start, end: savedOffPeak.end }, ...(savedOffPeak.additional_windows ?? [])]
        : [{ ...emptyWindow }],
    },
  }
  return <Form<PriceFormValues> name={PRICE_FORM_NAME} form={form} layout="vertical" initialValues={initialValues} disabled={loading}
    scrollToFirstError={{ block: 'nearest' }}
    onValuesChange={(changed: Partial<PriceFormValues>) => {
      if (changed.off_peak?.windows && form.getFieldError(OFF_PEAK_WINDOWS_PATH).length) {
        void form.validateFields([OFF_PEAK_WINDOWS_PATH]).catch(() => undefined)
      }
    }}
    onFinish={(values) => {
      const offPeak = values.off_peak
      const [first, ...additionalWindows] = offPeak?.windows ?? []
      save({
        currency: values.currency,
        ...configuredRates(values, fields),
        off_peak: values.scheduled && first ? {
          timezone: offPeak.timezone.trim(), ...first, additional_windows: additionalWindows,
          ...configuredRates(offPeak, fields),
        } : null,
      })
    }}>
    <Typography.Paragraph type="secondary">新单价用于之后的调用，历史估算保留原价。</Typography.Paragraph>
    <Form.Item name="currency" label="定价币种" rules={[{ required: true, message: '请选择币种' }]}>
      <Select options={[...USAGE_CURRENCIES]} />
    </Form.Item>
    <Form.Item name="scheduled" label="分时定价" valuePropName="checked">
      <Switch aria-label="分时定价" />
    </Form.Item>
    {scheduled && <>
      <Form.Item name={['off_peak', 'timezone']} label="定价时区"
        extra="默认北京时间（Asia/Shanghai），可填写供应商使用的 IANA 时区。"
        rules={[{ required: true, whitespace: true, message: '请输入定价时区' }]}>
        <Input placeholder={DEFAULT_USAGE_PRICE_TIMEZONE} maxLength={64} />
      </Form.Item>
      <PriceWindows loading={loading} />
    </>}
    <PriceInputs fields={fields} period={scheduled ? 'peak' : 'fixed'} />
    {scheduled && <PriceInputs fields={fields} period="off_peak" />}
    <Space wrap className="ai-usage-price-actions">
      <Button type="primary" htmlType="submit" loading={loading}>保存单价</Button>
      {current.price && <Popconfirm title="清除当前模型单价？" okText="清除" cancelText="取消" onConfirm={() => save(null)}>
        <Button danger disabled={loading}>清除单价</Button>
      </Popconfirm>}
    </Space>
  </Form>
}

export default function UsagePriceModal({ config, session, onClose }: Props) {
  const width = useModalWidth(480)
  const load = useCallback((options: UsageReadOptions) => aiUsageApi.getPrice(config.id, options), [config.id])
  const { data, error, refresh } = useAiUsageRequest(load, session)
  const moved = data && (data.model !== config.model || data.provider !== config.provider || data.model_type !== config.model_type)
  return <Modal open centered title={`模型单价 · ${config.name}`} onCancel={onClose} width={width} footer={null} destroyOnHidden
    classNames={{ body: 'ai-usage-price-body' }}>
    <Typography.Paragraph type="secondary">{config.model}</Typography.Paragraph>
    {error ? <Alert type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} />
      : moved ? <Alert type="warning" showIcon message="该配置的模型已变化，请刷新配置后重新设置单价。" />
        : data ? <UsagePriceForm key={data.version} config={config} session={session} onClose={onClose} current={data} reload={refresh} />
        : <Skeleton active paragraph={{ rows: 3 }} />}
  </Modal>
}
