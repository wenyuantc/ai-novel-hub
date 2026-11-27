import { Button, Descriptions, Drawer, Empty, List, Space, Table, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { MODEL_TYPE_LABELS, PROVIDER_LABELS } from '../../api/aiConfigs'
import type { UsagePage, UsageRow, UsageScope } from '../../api/aiUsage'
import type { ProviderName } from '../../api/types'
import { useIsMobile, useModalWidth } from '../../hooks/useIsMobile'
import { usageCount, usageKindLabel, usageMoney, usageTime, USAGE_PRICE_PERIOD_LABELS, USAGE_PRICING, USAGE_STATUSES } from './usagePresentation'

const BILLING_LABELS: Record<UsageRow['billing_source'], string> = {
  own: '本人承担', author: '作者承担', other: '用量归属其他账号', unknown: '承担人未知',
}

const PRICE_LABELS = [
  ['input_per_million', '输入 / 百万 tokens'],
  ['cached_input_per_million', '缓存命中输入 / 百万 tokens'],
  ['output_per_million', '输出 / 百万 tokens'],
  ['per_image', '每张图片'],
  ['per_video_second', '每秒视频'],
] as const

function modelLabel(row: UsageRow): string {
  if (row.model) return row.model
  return row.billing_source === 'author' ? '作者授权模型' : '模型未知'
}

function projectLabel(row: UsageRow): string {
  if (row.project_state === 'deleted') return '作品已删除'
  if (row.project_state === 'none') return '无作品归属'
  if (row.project_state === 'private') return '作品不可见'
  return row.project_title ?? (row.project_id ? `作品 #${row.project_id}` : '作品不可见')
}

function costLabel(row: UsageRow): string {
  return row.estimated_cost !== null && row.currency !== null
    ? usageMoney(row.estimated_cost, row.currency)
    : USAGE_PRICING[row.pricing_status] ?? '费用未知'
}

function accountLabel(name: string | null, id: number | null): string {
  return id === null ? name ?? '未知或不可见' : name ? `${name}（#${id}）` : `#${id}`
}

function RowUsage({ row }: { row: UsageRow }) {
  if (row.model_type === 'video') return <span>视频 {row.video_seconds == null ? '时长未知' : row.video_seconds.toLocaleString('zh-CN') + ' 秒'}</span>
  if (row.model_type === 'image') return <span>图片 {usageCount(row.image_count, row.image_source)}</span>
  return <Space direction="vertical" size={0}>
    <span>输入 {usageCount(row.input_tokens, row.input_source)}</span>
    {row.model_type === 'text' && <Typography.Text type="secondary">其中缓存命中 {usageCount(row.cached_input_tokens ?? null, 'provider')}</Typography.Text>}
    {row.model_type === 'text' && <span>输出 {usageCount(row.output_tokens, row.output_source)}</span>}
  </Space>
}

export function UsageLedgerSummary({ data }: { data: UsagePage }) {
  const summary = data.summary
  const cachedTokens = summary.cached_input_tokens ?? 0
  return <section className="ai-usage-summary" aria-label="筛选结果合计">
    <Space wrap size={[20, 8]}>
      <Typography.Text strong>筛选合计</Typography.Text>
      <span>{data.total.toLocaleString('zh-CN')} 次调用</span>
      <span>已记录输入 {summary.input_tokens.toLocaleString('zh-CN')} tokens{cachedTokens > 0 && `（其中缓存命中 ${cachedTokens.toLocaleString('zh-CN')} tokens）`}</span>
      <span>已记录输出 {summary.output_tokens.toLocaleString('zh-CN')} tokens</span>
      {summary.video_seconds > 0 && <span>已记录视频 {summary.video_seconds.toLocaleString('zh-CN')} 秒</span>}
      {summary.image_count > 0 && <span>已记录图片 {summary.image_count.toLocaleString('zh-CN')} 张</span>}
    </Space>
    <Space wrap size={[16, 8]}>
      {summary.costs.map(({ currency, amount, calls }) => <Typography.Text strong key={currency}>估算 {usageMoney(amount, currency)}（{calls} 次）</Typography.Text>)}
      {summary.costs.length === 0 && <Typography.Text type="secondary">暂无可计算的费用</Typography.Text>}
      {summary.unknown_usage_calls > 0 && <Tag color="warning">{summary.unknown_usage_calls} 次用量不完整</Tag>}
      {summary.unpriced_calls > 0 && <Tag color="warning">{summary.unpriced_calls} 次费用无法计算</Tag>}
    </Space>
    <Typography.Text type="secondary">{data.tracking_since ? `计量开始于 ${usageTime(data.tracking_since)}。` : '计量起点尚未记录。'}时间按北京时间显示。费用为估算值，按币种分别汇总。</Typography.Text>
  </section>
}

export function UsageLedgerRows({ data, scope, onDetail }: { data: UsagePage; scope: UsageScope; onDetail: (id: string) => void }) {
  const mobile = useIsMobile()
  const admin = scope.kind === 'admin'
  if (!data.items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的调用记录" />
  if (mobile) {
    return <List<UsageRow> dataSource={data.items} rowKey="id" renderItem={(row) => (
      <List.Item className="ai-usage-mobile-row">
        <div className="ai-usage-row-heading"><Typography.Text strong>{usageKindLabel(row.kind)}</Typography.Text><Tag>{USAGE_STATUSES[row.status] ?? row.status}</Tag></div>
        <div>{usageTime(row.started_at)} · {projectLabel(row)}{row.chapter_id ? ` · 章节 #${row.chapter_id}` : ''}</div>
        {admin && <div>发起 {accountLabel(row.actor_name, row.actor_id)} · 承担 {accountLabel(row.payer_name, row.payer_id)}</div>}
        <div>{modelLabel(row)}</div>
        <RowUsage row={row} />
        <div className="ai-usage-row-heading"><Typography.Text strong>{costLabel(row)}</Typography.Text><Button size="small" onClick={() => onDetail(row.id)}>明细</Button></div>
      </List.Item>
    )} />
  }
  return <Table<UsageRow> size="small" rowKey="id" dataSource={data.items} pagination={false} scroll={{ x: admin ? 1080 : 800 }} columns={[
    { title: '调用时间', width: 152, render: (_, row) => usageTime(row.started_at) },
    { title: '任务 / 作品', width: 214, render: (_, row) => <Space direction="vertical" size={2}>
      <span>{usageKindLabel(row.kind)} <Tag>{USAGE_STATUSES[row.status] ?? row.status}</Tag></span>
      <Typography.Text type="secondary">{projectLabel(row)}{row.chapter_id ? ` · 章节 #${row.chapter_id}` : ''}</Typography.Text>
    </Space> },
    ...(admin ? [{ title: '账号', width: 185, key: 'accounts', render: (_: unknown, row: UsageRow) => <Space direction="vertical" size={0}>
      <span>发起 {accountLabel(row.actor_name, row.actor_id)}</span><span>承担 {accountLabel(row.payer_name, row.payer_id)}</span>
    </Space> }] : []),
    { title: '模型', width: 160, render: (_, row) => modelLabel(row) },
    { title: '用量', width: 152, render: (_, row) => <RowUsage row={row} /> },
    { title: '费用估算', width: 140, render: (_, row) => costLabel(row) },
    { title: '', key: 'detail', width: 68, render: (_, row) => <Button size="small" onClick={() => onDetail(row.id)} aria-label={`查看${usageKindLabel(row.kind)}的调用明细`}>明细</Button> },
  ]} />
}

export function UsageRowDetail({ row, scope, onClose }: { row: UsageRow | undefined; scope: UsageScope; onClose: () => void }) {
  const width = useModalWidth(560)
  const visibleCurrency = row?.pricing_status === 'private' ? null : row?.currency
  const cachedInputNotice = row?.model_type === 'text' && row.pricing_status === 'priced' && row.input_per_million !== null
    && ((row.cached_input_tokens == null && row.cached_input_per_million != null && '缓存命中量未知，按普通输入单价估算。')
      || (row.cached_input_tokens != null && row.cached_input_per_million == null && '未设置缓存命中价，按普通输入单价计算。'))
  const detail = row ? [
    { key: 'id', label: '调用 ID', children: row.id },
    { key: 'time', label: '调用时间（北京时间）', children: usageTime(row.started_at) },
    { key: 'kind', label: '任务类型', children: `${usageKindLabel(row.kind)} · ${USAGE_STATUSES[row.status] ?? row.status}` },
    { key: 'project', label: '作品', children: projectLabel(row) },
    { key: 'chapter', label: '章节', children: row.chapter_id && scope.kind === 'project' ? <Link to={`/projects/${scope.projectId}/chapters/${row.chapter_id}`}>章节 #{row.chapter_id}</Link> : row.chapter_id ?? '无' },
    { key: 'task', label: '任务 ID', children: row.task_id ?? '无' },
    { key: 'actor', label: '发起账号', children: accountLabel(row.actor_name, row.actor_id) },
    { key: 'payer', label: '费用承担', children: row.payer_id !== null ? accountLabel(row.payer_name, row.payer_id) : BILLING_LABELS[row.billing_source] },
    { key: 'model', label: MODEL_TYPE_LABELS[row.model_type], children: [row.provider && (PROVIDER_LABELS[row.provider as ProviderName] ?? row.provider), modelLabel(row)].filter(Boolean).join(' / ') },
    { key: 'usage', label: '用量', children: <RowUsage row={row} /> },
    { key: 'cost', label: '费用估算', children: costLabel(row) },
    ...(row.price_period && row.pricing_status !== 'private'
      ? [{ key: 'pricePeriod', label: '计价档位', children: USAGE_PRICE_PERIOD_LABELS[row.price_period] }] : []),
    ...PRICE_LABELS.flatMap(([key, label]) => visibleCurrency && row[key] != null
      ? [{ key, label, children: usageMoney(row[key], visibleCurrency) }] : []),
    ...(visibleCurrency && cachedInputNotice ? [{ key: 'cachedInputNotice', label: '缓存计价', children: cachedInputNotice }] : []),
  ] : []
  return <Drawer open={!!row} title="调用明细" onClose={onClose} width={width} destroyOnHidden>
    {row && <Descriptions className="ai-usage-description" column={1} items={detail} size="small" />}
  </Drawer>
}
