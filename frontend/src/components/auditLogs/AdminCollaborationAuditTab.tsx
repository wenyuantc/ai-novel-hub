import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Empty, Form, Input, InputNumber, List, Pagination, Select, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { currentRequestGeneration, errorMessage, watchRequestGeneration } from '../../api/client'
import type { CollaborationScope } from '../../api/collaboration'
import {
  COLLAB_AUDIT_ACTION,
  COLLAB_AUDIT_ERROR,
  COLLAB_AUDIT_RESULT,
  COLLAB_AUDIT_SCOPE,
  collaborationAuditApi,
  type AdminCollaborationAuditQuery,
  type CollaborationAuditAction,
  type CollaborationAuditEvent,
  type CollaborationAuditPage,
  type CollaborationAuditResult,
} from '../../api/collaborationAudit'
import { useAuthorizationCleanup } from '../../hooks/useAuthorizationCleanup'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useAuthStore } from '../../stores/auth'
import { auditValidPage } from '../../utils/auditLogs'
import {
  adminCollaborationAuditQuery,
  collaborationResourceLabel,
  collaborationTargetLabel,
  collaborationUserLabel,
  type AdminCollaborationAuditFilterValues,
} from '../../utils/collaborationAudit'
import { authorizationKey, can } from '../../utils/permissions'
import { formatTime } from '../../utils/time'
import CollaborationAuditDetailDrawer from './CollaborationAuditDetailDrawer'

const MAX_DB_INT = 2_147_483_647

interface ResultsProps {
  items: CollaborationAuditEvent[]
  loading: boolean
  mobile: boolean
  onDetail: (event: CollaborationAuditEvent) => void
}

/** 管理员全站协作审计结果：桌面端密集表格，375px 移动端可换行卡片，均为只读入口。 */
export function AdminCollaborationAuditResults({ items, loading, mobile, onDetail }: ResultsProps) {
  const resultTag = (item: CollaborationAuditEvent) => {
    const meta = COLLAB_AUDIT_RESULT[item.result]
    return <Space size={[4, 4]} wrap>
      <Tag color={meta.color}>{meta.label}</Tag>
      {item.error_code && <Tag>{COLLAB_AUDIT_ERROR[item.error_code]}</Tag>}
    </Space>
  }
  const columns: TableColumnsType<CollaborationAuditEvent> = [
    {
      title: '时间 / ID', key: 'time', width: 180,
      render: (_, item) => <>{formatTime(item.occurred_at, 'YYYY-MM-DD HH:mm:ss')}<br /><Typography.Text type="secondary">#{item.id}</Typography.Text></>,
    },
    {
      title: '范围与资源快照', key: 'resource', width: 240,
      render: (_, item) => <span style={{ overflowWrap: 'anywhere' }}>{collaborationResourceLabel(item)}</span>,
    },
    {
      title: '操作人快照', key: 'actor', width: 160,
      render: (_, item) => <span style={{ overflowWrap: 'anywhere' }}>{collaborationUserLabel(item.actor_user_id, item.actor_username)}</span>,
    },
    {
      title: '目标成员快照', key: 'target', width: 160,
      render: (_, item) => <span style={{ overflowWrap: 'anywhere' }}>{collaborationTargetLabel(item)}</span>,
    },
    {
      title: '动作', dataIndex: 'action', width: 110,
      render: (action: CollaborationAuditAction) => COLLAB_AUDIT_ACTION[action],
    },
    {
      title: '结果', key: 'result', width: 190,
      render: (_, item) => resultTag(item),
    },
    {
      title: '操作', key: 'detail', width: 80,
      render: (_, item) => <Button size="small" type="link" onClick={() => onDetail(item)}>详情</Button>,
    },
  ]
  if (!mobile) {
    return <Table<CollaborationAuditEvent>
      rowKey="id"
      size="small"
      columns={columns}
      dataSource={items}
      loading={loading}
      pagination={false}
      scroll={{ x: 1120 }}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的协作审计记录" /> }}
    />
  }
  return <List<CollaborationAuditEvent>
    loading={loading}
    dataSource={items}
    rowKey="id"
    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的协作审计记录" /> }}
    renderItem={(item) => <List.Item style={{ display: 'block', overflowWrap: 'anywhere' }}>
      <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}>
        <Typography.Text strong>{COLLAB_AUDIT_ACTION[item.action]}</Typography.Text>
        {resultTag(item)}
      </div>
      <Typography.Paragraph style={{ margin: '8px 0 4px' }}>{collaborationResourceLabel(item)}</Typography.Paragraph>
      <Typography.Text>操作人：{collaborationUserLabel(item.actor_user_id, item.actor_username)}</Typography.Text><br />
      <Typography.Text>目标成员：{collaborationTargetLabel(item)}</Typography.Text><br />
      <Typography.Text type="secondary">{formatTime(item.occurred_at, 'YYYY-MM-DD HH:mm:ss')} · 记录 #{item.id}</Typography.Text>
      <div style={{ textAlign: 'right' }}>
        <Button size="small" onClick={() => onDetail(item)}>查看详情</Button>
      </div>
    </List.Item>}
  />
}

interface LoadedState {
  key: string
  data: CollaborationAuditPage | null
  page: number
  loading: boolean
  error: string | null
}

const EMPTY_LOADED: LoadedState = { key: '', data: null, page: 1, loading: false, error: null }

export default function AdminCollaborationAuditTab({ actorId }: { actorId: number }) {
  const actor = useAuthStore((state) => state.user)
  const mobile = useIsMobile()
  const [generation, setGeneration] = useState(currentRequestGeneration)
  const [form] = Form.useForm<AdminCollaborationAuditFilterValues>()
  const [query, setQuery] = useState<AdminCollaborationAuditQuery>({})
  const [filterError, setFilterError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [reloadNonce, setReloadNonce] = useState(0)
  const [loaded, setLoaded] = useState<LoadedState>(EMPTY_LOADED)
  const [selected, setSelected] = useState<CollaborationAuditEvent | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const ticketRef = useRef(0)

  const authKey = authorizationKey(actor)
  const canRead = can(actor, 'audit.read')
  const identityKey = `${actorId}:${authKey}:${generation}:${canRead}`

  useAuthorizationCleanup(() => setSelected(null))

  useEffect(() => {
    const watcher = watchRequestGeneration(() => {
      ticketRef.current += 1
      controllerRef.current?.abort()
      controllerRef.current = null
      setSelected(null)
      setGeneration(currentRequestGeneration())
    })
    return watcher.dispose
  }, [])

  useEffect(() => {
    if (!canRead) return
    const ticket = ++ticketRef.current
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const isCurrent = () => {
      const currentUser = useAuthStore.getState().user
      return ticketRef.current === ticket
        && !controller.signal.aborted
        && currentUser?.id === actorId
        && authorizationKey(currentUser) === authKey
        && can(currentUser, 'audit.read')
        && currentRequestGeneration() === generation
    }
    setLoaded((prev) => (prev.key === identityKey
      ? { ...prev, loading: true, error: null }
      : { key: identityKey, data: null, page: 1, loading: true, error: null }))
    void (async () => {
      try {
        const first = await collaborationAuditApi.listForAdmin(
          { ...query, page, page_size: pageSize }, controller.signal,
        )
        if (!isCurrent()) return
        const validPage = auditValidPage(page, first.total, pageSize)
        const resolved = validPage !== page
          ? await collaborationAuditApi.listForAdmin(
            { ...query, page: validPage, page_size: pageSize }, controller.signal,
          )
          : first
        if (!isCurrent()) return
        setLoaded({ key: identityKey, data: resolved, page: validPage, loading: false, error: null })
      } catch (err) {
        if (!isCurrent()) return
        setLoaded((prev) => ({
          key: identityKey,
          data: prev.key === identityKey ? prev.data : null,
          page: prev.key === identityKey ? prev.page : 1,
          loading: false,
          error: errorMessage(err),
        }))
      }
    })()
    return () => {
      ticketRef.current += 1
      controller.abort()
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }, [actorId, authKey, canRead, generation, identityKey, page, pageSize, query, reloadNonce])

  const reload = useCallback(() => setReloadNonce((n) => n + 1), [])

  const applyFilters = (values: AdminCollaborationAuditFilterValues) => {
    try {
      setQuery(adminCollaborationAuditQuery(values))
      setPage(1)
      setFilterError(null)
      reload()
    } catch (err) {
      setFilterError(errorMessage(err))
    }
  }

  const resetFilters = () => {
    form.resetFields()
    setQuery({})
    setPage(1)
    setFilterError(null)
    reload()
  }

  const state = loaded.key === identityKey ? loaded : EMPTY_LOADED
  const fieldStyle = { flex: '1 1 150px', minWidth: 0, marginBottom: 0 }

  return <>
    <Alert
      type="info"
      showIcon
      message="全站协作审计（只读，不受清理影响）"
      description="协作审计独立只追加保存，不受「管理操作日志」按日期清理影响；包含已删除作品或系列的快照、非作者越权尝试，以及无法归属资源的无效邀请失败记录（资源 #0）。"
      style={{ marginBottom: 16 }}
    />
    <Card styles={{ body: { padding: mobile ? 12 : 16, minWidth: 0 } }}>
      <Form form={form} layout="vertical" onFinish={applyFilters}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginBottom: 12 }}>
          <Form.Item name="scope" label="作用范围" style={fieldStyle}>
            <Select<CollaborationScope> allowClear placeholder="全部范围"
              options={(Object.keys(COLLAB_AUDIT_SCOPE) as CollaborationScope[]).map((value) => ({
                value, label: COLLAB_AUDIT_SCOPE[value],
              }))} />
          </Form.Item>
          <Form.Item name="resource_id" label="资源 ID" style={fieldStyle}>
            <InputNumber min={0} max={MAX_DB_INT} precision={0} placeholder="作品/系列 ID（含 0）" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="resource_q" label="资源名称快照" style={fieldStyle}>
            <Input allowClear placeholder="作品标题或系列名" maxLength={200} />
          </Form.Item>
          <Form.Item name="actor_q" label="操作人用户名" style={fieldStyle}>
            <Input allowClear placeholder="操作人快照" maxLength={64} />
          </Form.Item>
          <Form.Item name="actor_user_id" label="操作人 ID" style={fieldStyle}>
            <InputNumber min={1} max={MAX_DB_INT} precision={0} placeholder="操作人 ID" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="target_q" label="目标成员用户名" style={fieldStyle}>
            <Input allowClear placeholder="目标成员快照" maxLength={64} />
          </Form.Item>
          <Form.Item name="target_user_id" label="目标成员 ID" style={fieldStyle}>
            <InputNumber min={1} max={MAX_DB_INT} precision={0} placeholder="目标成员 ID" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="action" label="动作" style={fieldStyle}>
            <Select<CollaborationAuditAction> allowClear placeholder="全部动作"
              options={(Object.keys(COLLAB_AUDIT_ACTION) as CollaborationAuditAction[]).map((value) => ({
                value, label: COLLAB_AUDIT_ACTION[value],
              }))} />
          </Form.Item>
          <Form.Item name="result" label="结果" style={fieldStyle}>
            <Select<CollaborationAuditResult> allowClear placeholder="全部结果"
              options={(Object.keys(COLLAB_AUDIT_RESULT) as CollaborationAuditResult[]).map((value) => ({
                value, label: COLLAB_AUDIT_RESULT[value].label,
              }))} />
          </Form.Item>
          <Form.Item name="fromDate" label="开始日期（本地，含）" style={fieldStyle}>
            <Input type="date" style={{ width: '100%', minWidth: 0 }} />
          </Form.Item>
          <Form.Item name="toDate" label="结束日期（本地，含）" style={fieldStyle}>
            <Input type="date" style={{ width: '100%', minWidth: 0 }} />
          </Form.Item>
        </div>
        <Space wrap style={{ marginBottom: 16 }}>
          <Button type="primary" htmlType="submit" disabled={state.loading}>查询</Button>
          <Button onClick={resetFilters} disabled={state.loading}>重置筛选</Button>
          <Button icon={<ReloadOutlined />} onClick={reload} disabled={state.loading}>刷新</Button>
        </Space>
      </Form>
      {filterError && <Alert type="error" showIcon message={filterError} style={{ marginBottom: 16 }} />}
      {state.error && <Alert type="error" showIcon message={state.error}
        action={<Button size="small" onClick={reload}>重试</Button>} style={{ marginBottom: 16 }} />}
      <AdminCollaborationAuditResults items={state.data?.items ?? []} loading={state.loading} mobile={mobile} onDetail={setSelected} />
      <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 12, alignItems: 'center', marginTop: 16 }}>
        <Typography.Text type="secondary">共 {state.data?.total ?? 0} 条 · 时间与 ID 倒序</Typography.Text>
        <Pagination
          current={state.page}
          pageSize={pageSize}
          total={state.data?.total ?? 0}
          simple={mobile}
          showSizeChanger={!mobile}
          pageSizeOptions={[20, 50, 100]}
          onChange={(next, size) => { setPage(size !== pageSize ? 1 : next); setPageSize(size); reload() }}
        />
      </div>
    </Card>
    <CollaborationAuditDetailDrawer event={selected} onClose={() => setSelected(null)} />
  </>
}
