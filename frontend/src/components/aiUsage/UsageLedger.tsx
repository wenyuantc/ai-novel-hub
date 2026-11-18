import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Form, Input, InputNumber, Pagination, Select, Skeleton, Space } from 'antd'
import { useCallback, useState } from 'react'
import { MODEL_TYPE_LABELS, PROVIDER_LABELS } from '../../api/aiConfigs'
import { aiUsageApi, type UsageFilters, type UsageScope } from '../../api/aiUsage'
import { errorMessage } from '../../api/client'
import { useIsMobile } from '../../hooks/useIsMobile'
import { UsageLedgerRows, UsageLedgerSummary, UsageRowDetail } from './UsageLedgerViews'
import { useAiUsageRequest, type UsageReadOptions, type UsageSession } from './useAiUsageRequest'
import { USAGE_KIND_LABELS, USAGE_STATUSES, usageFilterQuery, type UsageFilterFields } from './usagePresentation'

const PAGE_SIZE = 20
const MAX_ENTITY_ID = 2_147_483_647
const selectOptions = (labels: Record<string, string>) => Object.entries(labels).map(([value, label]) => ({ value, label }))
const providerOptions = selectOptions(PROVIDER_LABELS)
const modelTypeOptions = selectOptions(MODEL_TYPE_LABELS)
const statusOptions = selectOptions(USAGE_STATUSES)
const kindOptions = selectOptions(USAGE_KIND_LABELS)

interface Props {
  scope: UsageScope
  session: UsageSession
  canAccess?: () => boolean
  initialChapter?: number
  projects?: { id: number; title: string }[]
  chapters?: { id: number; title: string; chapter_no: number }[]
}

export default function UsageLedger({ scope, session, canAccess, initialChapter, projects, chapters }: Props) {
  const [form] = Form.useForm<UsageFilterFields>()
  const [query, setQuery] = useState<UsageFilters>(() => ({ chapter_id: initialChapter, page: 1, page_size: PAGE_SIZE }))
  const [detailId, setDetailId] = useState<string | null>(null)
  const [filterError, setFilterError] = useState<string | null>(null)
  const load = useCallback((options: UsageReadOptions) => aiUsageApi.list(scope, query, options), [scope, query])
  const { data, error, loading, refresh } = useAiUsageRequest(load, session, canAccess)
  const mobile = useIsMobile()
  const detail = detailId === null ? undefined : data?.items.find((row) => row.id === detailId)

  function applyFilters(values: UsageFilterFields) {
    try {
      setQuery({ ...usageFilterQuery(values), page_size: query.page_size })
      setFilterError(null)
      setDetailId(null)
    } catch (error) {
      setFilterError(errorMessage(error))
    }
  }

  function clearFilters() {
    form.resetFields()
    form.setFieldValue('chapter_id', undefined)
    setFilterError(null)
    setDetailId(null)
    setQuery({ page: 1, page_size: query.page_size })
  }

  return <Card size="small" title="调用记录" className="ai-usage-ledger"
    extra={<Button icon={<ReloadOutlined />} onClick={() => { setDetailId(null); refresh() }} loading={loading}>刷新</Button>}>
    <Form form={form} initialValues={{ chapter_id: initialChapter }} layout="vertical" onFinish={applyFilters}>
      <div className="ai-usage-filter-grid">
        {scope.kind === 'admin' && <>
          <Form.Item name="actor_id" label="发起账号 ID"><InputNumber min={1} precision={0} max={MAX_ENTITY_ID} /></Form.Item>
          <Form.Item name="payer_id" label="承担账号 ID"><InputNumber min={1} precision={0} max={MAX_ENTITY_ID} /></Form.Item>
          <Form.Item name="project_id" label="作品 ID"><InputNumber min={1} precision={0} max={MAX_ENTITY_ID} /></Form.Item>
        </>}
        {scope.kind === 'account' && <Form.Item name="project_id" label="作品">
          {projects ? <Select<number> placeholder="全部作品" allowClear showSearch optionFilterProp="label"
            options={projects.map(({ id, title }) => ({ value: id, label: title }))} />
            : <InputNumber min={1} precision={0} max={MAX_ENTITY_ID} placeholder="输入作品 ID" />}
        </Form.Item>}
        {scope.kind === 'project' && <Form.Item name="chapter_id" label="章节">
          {chapters ? <Select<number> placeholder="全书" allowClear showSearch optionFilterProp="label"
            options={chapters.map(({ id, title, chapter_no }) => ({ value: id, label: `第 ${chapter_no} 章 · ${title}` }))} />
            : <InputNumber min={1} precision={0} max={MAX_ENTITY_ID} placeholder="输入章节 ID" />}
        </Form.Item>}
        <Form.Item name="provider" label="提供方"><Select allowClear placeholder="全部" options={providerOptions} /></Form.Item>
        <Form.Item name="model" label="模型"><Input allowClear placeholder="输入完整模型名" maxLength={200} /></Form.Item>
        <Form.Item name="model_type" label="模型类型"><Select allowClear placeholder="全部" options={modelTypeOptions} /></Form.Item>
        <Form.Item name="kind" label="任务类型" rules={[{ max: 100, message: '任务类型最多 100 个字符' }]}
          getValueProps={(value: string | undefined) => ({ value: value ? [value] : [] })}
          getValueFromEvent={(values: string[]) => values[0]}>
          <Select mode="tags" maxCount={1} allowClear showSearch optionFilterProp="label"
            placeholder="选择或输入任务类型" options={kindOptions} />
        </Form.Item>
        <Form.Item name="status" label="调用状态"><Select allowClear placeholder="全部" options={statusOptions} /></Form.Item>
        <Form.Item name="fromDay" label="开始日期（北京时间）"><Input type="date" /></Form.Item>
        <Form.Item name="toDay" label="结束日期（含当天）"><Input type="date" /></Form.Item>
      </div>
      {filterError && <Alert type="error" showIcon message={filterError} className="ai-usage-inline-alert" />}
      <Space wrap><Button type="primary" htmlType="submit">查询</Button><Button onClick={clearFilters}>重置筛选</Button></Space>
    </Form>
    <div className="ai-usage-results" aria-busy={loading}>
      {error ? <Alert type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} />
        : data ? <>
          <UsageLedgerSummary data={data} />
          <UsageLedgerRows data={data} scope={scope} onDetail={setDetailId} />
          {data.total > 0 && <Pagination aria-label="调用记录分页" current={data.page} pageSize={data.page_size} total={data.total}
            simple={mobile} showSizeChanger={!mobile} pageSizeOptions={[20, 50, 100]}
            onChange={(page, size) => { setDetailId(null); setQuery((previous) => ({ ...previous, page, page_size: size })) }} />}
        </> : <Skeleton active paragraph={{ rows: 4 }} />}
    </div>
    <UsageRowDetail row={detail} scope={scope} onClose={() => setDetailId(null)} />
  </Card>
}
