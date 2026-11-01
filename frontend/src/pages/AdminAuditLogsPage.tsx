import { useAuthorizationCleanup } from '../hooks/useAuthorizationCleanup'
import { can } from '../utils/permissions'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Empty, Form, Input, InputNumber, List, Pagination, Result, Select, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useState } from 'react'
import { AUDIT_ACTION, AUDIT_RESULT, AUDIT_TARGET, auditLogApi, auditResult } from '../api/auditLogs'
import { errorMessage } from '../api/client'
import type { AuditAction, AuditLogBrief, AuditLogListQuery, AuditResult, AuditTargetType } from '../api/types'
import AuditCleanupModal from '../components/auditLogs/AuditCleanupModal'
import AuditLogDetailDrawer from '../components/auditLogs/AuditLogDetailDrawer'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { auditActorLabel, auditFilterQuery, auditTargetLabel, auditValidPage } from '../utils/auditLogs'
import type { AuditFilterValues } from '../utils/auditLogs'
import { formatTime } from '../utils/time'

interface ResultsProps {
  items: AuditLogBrief[]
  loading: boolean
  mobile: boolean
  onDetail: (id: number) => void
}

/** 桌面密集表格、移动端可换行列表，均只有只读详情入口。 */
export function AuditLogResults({ items, loading, mobile, onDetail }: ResultsProps) {
  const resultTag = (log: AuditLogBrief) => <Space size={[4, 4]} wrap>
    <Tag color={auditResult(log).color}>{auditResult(log).label}</Tag>
    {log.action === 'audit.cleanup' && <Tag>受保护</Tag>}
  </Space>
  const columns: TableColumnsType<AuditLogBrief> = [
    { title: '时间 / ID', key: 'time', width: 190, render: (_, log) => <>{formatTime(log.occurred_at, 'YYYY-MM-DD HH:mm:ss')}<br /><Typography.Text type="secondary">#{log.id}</Typography.Text></> },
    { title: '真实操作人', key: 'actor', width: 150, render: (_, log) => <span style={{ overflowWrap: 'anywhere' }}>{auditActorLabel(log)}</span> },
    { title: '动作', dataIndex: 'action', width: 180, render: (action: AuditAction) => AUDIT_ACTION[action] },
    { title: '目标快照', key: 'target', width: 260, render: (_, log) => <span style={{ overflowWrap: 'anywhere' }}>{auditTargetLabel(log)}</span> },
    { title: '所属用户 / 作品', key: 'owner', width: 210, render: (_, log) => <span style={{ overflowWrap: 'anywhere' }}>{log.owner_user_id === null ? '不适用 / 未知' : `${log.owner_username ?? '历史用户'} · #${log.owner_user_id}`}<br /><Typography.Text type="secondary">{log.project_id === null ? '—' : `${log.project_title ?? '历史作品'} · #${log.project_id}`}</Typography.Text></span> },
    { title: '结果', key: 'result', width: 110, render: (_, log) => resultTag(log) },
    { title: '操作', key: 'detail', width: 90, render: (_, log) => <Button size="small" type="link" onClick={() => onDetail(log.id)}>详情</Button> },
  ]
  if (!mobile) return <Table<AuditLogBrief> rowKey="id" size="small" columns={columns} dataSource={items} loading={loading} pagination={false} scroll={{ x: 1190 }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的审计日志" /> }} />
  return <List loading={loading} dataSource={items} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的审计日志" /> }} renderItem={(log) => <List.Item style={{ display: 'block', overflowWrap: 'anywhere' }}>
    <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}><Typography.Text strong>{AUDIT_ACTION[log.action]}</Typography.Text>{resultTag(log)}</div>
    <Typography.Paragraph style={{ margin: '8px 0 4px' }}>{auditTargetLabel(log)}</Typography.Paragraph>
    <Typography.Text>操作人：{auditActorLabel(log)}</Typography.Text><br />
    <Typography.Text type="secondary">{formatTime(log.occurred_at, 'YYYY-MM-DD HH:mm:ss')} · 日志 #{log.id}</Typography.Text>
    <div style={{ textAlign: 'right' }}><Button size="small" onClick={() => onDetail(log.id)}>查看详情</Button></div>
  </List.Item>} />
}

function AuditLogs({ actorId }: { actorId: number }) {
  const actor = useAuthStore((state) => state.user)
  const mobile = useIsMobile()
  const [form] = Form.useForm<AuditFilterValues>()
  const [query, setQuery] = useState<AuditLogListQuery>({})
  const [filterError, setFilterError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [cleanupOpen, setCleanupOpen] = useState(false)
  useAuthorizationCleanup(() => { setDetailId(null); setCleanupOpen(false) })
  const loader = useCallback(async () => {
    const result = await auditLogApi.list({ ...query, page, page_size: pageSize })
    const validPage = auditValidPage(page, result.total, pageSize)
    // 清理/并发删除使页码失效时，在同一加载流程读取最后有效页，不显示永久空白页。
    if (validPage !== page) return { ...await auditLogApi.list({ ...query, page: validPage, page_size: pageSize }), page: validPage }
    return { ...result, page }
  }, [query, page, pageSize])
  const { data, loading, error, reload } = useLoader(loader)
  const currentPage = data?.page ?? page
  const applyFilters = (values: AuditFilterValues) => {
    try {
      setQuery(auditFilterQuery(values))
      setPage(1)
      setFilterError(null)
      reload()
    } catch (err) { setFilterError(errorMessage(err)) }
  }
  const resetFilters = () => {
    form.resetFields()
    setQuery({})
    setPage(1)
    setFilterError(null)
    reload()
  }
  const fieldStyle = { flex: '1 1 150px', minWidth: 0, marginBottom: 0 }
  return <div style={{ maxWidth: 1500, minWidth: 0, margin: '0 auto', padding: mobile ? 12 : 24 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', alignItems: 'flex-start', gap: 12, marginBottom: 16 }}>
      <div><Typography.Title level={3} style={{ margin: '0 0 8px' }}>操作日志</Typography.Title><Typography.Text type="secondary">追溯真实管理员或系统清理、操作对象与结果。长期保留；仅含安全摘要，无正文全文差异。所有时间按浏览器本地时区显示。</Typography.Text></div>
      <Space wrap><Button icon={<ReloadOutlined />} onClick={reload} disabled={loading}>刷新</Button><Button danger icon={<DeleteOutlined />} disabled={!can(actor, 'audit.cleanup')} onClick={() => setCleanupOpen(true)}>按日期清理</Button></Space>
    </div>
    <Card styles={{ body: { padding: mobile ? 12 : 16, minWidth: 0 } }}>
      <Form form={form} layout="vertical" onFinish={applyFilters}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginBottom: 12 }}>
          <Form.Item name="actor_q" label="历史操作人用户名" style={fieldStyle}><Input allowClear placeholder="用户名快照，支持部分匹配" maxLength={64} /></Form.Item>
          <Form.Item name="actor_user_id" label="操作人 ID" style={fieldStyle}><InputNumber min={1} max={Number.MAX_SAFE_INTEGER} precision={0} placeholder="用户 ID" style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="target_type" label="目标类型" style={fieldStyle}><Select<AuditTargetType> allowClear placeholder="全部类型" options={(Object.keys(AUDIT_TARGET) as AuditTargetType[]).map((value) => ({ value, label: AUDIT_TARGET[value] }))} /></Form.Item>
          <Form.Item name="target_id" label="目标 ID" style={fieldStyle}><InputNumber min={1} max={Number.MAX_SAFE_INTEGER} precision={0} placeholder="目标 ID" style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="action" label="动作" style={{ ...fieldStyle, flexBasis: 200 }}><Select<AuditAction> allowClear showSearch optionFilterProp="label" placeholder="全部动作" options={(Object.keys(AUDIT_ACTION) as AuditAction[]).map((value) => ({ value, label: AUDIT_ACTION[value] }))} /></Form.Item>
          <Form.Item name="result" label="结果" style={fieldStyle}><Select<AuditResult> allowClear placeholder="全部结果" options={(Object.keys(AUDIT_RESULT) as AuditResult[]).map((value) => ({ value, label: AUDIT_RESULT[value].label }))} /></Form.Item>
          <Form.Item name="fromDate" label="开始日期（本地，含）" style={fieldStyle}><Input type="date" style={{ width: '100%', minWidth: 0 }} /></Form.Item>
          <Form.Item name="toDate" label="结束日期（本地，含）" style={fieldStyle}><Input type="date" style={{ width: '100%', minWidth: 0 }} /></Form.Item>
        </div>
        <Space wrap style={{ marginBottom: 16 }}><Button type="primary" htmlType="submit">查询</Button><Button onClick={resetFilters}>重置筛选</Button></Space>
      </Form>
      {filterError && <Alert type="error" showIcon message={filterError} style={{ marginBottom: 16 }} />}
      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={reload}>重试</Button>} style={{ marginBottom: 16 }} />}
      <AuditLogResults items={data?.items ?? []} loading={loading} mobile={mobile} onDetail={setDetailId} />
      <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 12, alignItems: 'center', marginTop: 16 }}>
        <Typography.Text type="secondary">共 {data?.total ?? 0} 条 · 时间与 ID 倒序</Typography.Text>
        <Pagination current={currentPage} pageSize={pageSize} total={data?.total ?? 0} simple={mobile} showSizeChanger={!mobile} pageSizeOptions={[20, 50, 100]} onChange={(next, size) => { setPage(size !== pageSize ? 1 : next); setPageSize(size); reload() }} />
      </div>
    </Card>
    {detailId !== null && <AuditLogDetailDrawer key={`${actorId}:${detailId}`} id={detailId} actorId={actorId} onClose={() => setDetailId(null)} />}
    {cleanupOpen && can(actor, 'audit.cleanup') && <AuditCleanupModal key={actorId} actorId={actorId} onClose={() => setCleanupOpen(false)} onCleaned={() => { setDetailId(null); setPage(currentPage); reload() }} />}
  </div>
}

/** 路由仍由 RequireAdmin 保护；按真实账号重建所有本地数据和待确认令牌。 */
export default function AdminAuditLogsPage() {
  const user = useAuthStore((s) => s.user)
  return user && can(user, 'audit.read') ? <AuditLogs key={user.id} actorId={user.id} /> : <Result status="403" title="需要管理员权限" />
}
