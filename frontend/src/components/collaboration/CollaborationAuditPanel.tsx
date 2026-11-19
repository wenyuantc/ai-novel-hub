import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Form, Input, List, Pagination, Select, Space, Tag, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { currentRequestGeneration, errorMessage, watchRequestGeneration } from '../../api/client'
import type { CollaborationScope } from '../../api/collaboration'
import {
  COLLAB_AUDIT_ACTION,
  COLLAB_AUDIT_ERROR,
  COLLAB_AUDIT_RESULT,
  collaborationAuditApi,
  type AuthorCollaborationAuditQuery,
  type CollaborationAuditAction,
  type CollaborationAuditEvent,
  type CollaborationAuditPage,
  type CollaborationAuditResult,
} from '../../api/collaborationAudit'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useAuthStore } from '../../stores/auth'
import { auditValidPage } from '../../utils/auditLogs'
import {
  authorCollaborationAuditQuery,
  collaborationAuditSummaryRows,
  collaborationTargetLabel,
  collaborationUserLabel,
  type AuthorCollaborationAuditFilterValues,
} from '../../utils/collaborationAudit'
import { formatTime } from '../../utils/time'
import CollaborationAuditDetailDrawer from '../auditLogs/CollaborationAuditDetailDrawer'
import './collaboration.css'

export interface CollaborationAuditPanelProps {
  scope: CollaborationScope
  resourceId: number
  canManage: boolean
}

interface ItemsProps {
  items: CollaborationAuditEvent[]
  loading: boolean
  onDetail: (event: CollaborationAuditEvent) => void
}

/** 作者侧协作审计记录列表；在抽屉、卡片及 375px 移动端均采用可换行布局。 */
export function CollaborationAuditItems({ items, loading, onDetail }: ItemsProps) {
  return <List<CollaborationAuditEvent>
    loading={loading}
    dataSource={items}
    rowKey="id"
    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的协作审计记录" /> }}
    renderItem={(item) => {
      const result = COLLAB_AUDIT_RESULT[item.result]
      const summaryRows = collaborationAuditSummaryRows(item)
      return <List.Item className="collaboration-audit-item">
        <div className="collaboration-audit-item-head">
          <Space size={[6, 4]} wrap>
            <Typography.Text strong>{COLLAB_AUDIT_ACTION[item.action]}</Typography.Text>
            <Tag color={result.color}>{result.label}</Tag>
            {item.error_code && <Tag>{COLLAB_AUDIT_ERROR[item.error_code]}</Tag>}
          </Space>
          <Button size="small" type="link" onClick={() => onDetail(item)}>详情</Button>
        </div>
        <div className="collaboration-audit-meta">
          <span>操作人：{collaborationUserLabel(item.actor_user_id, item.actor_username)}</span>
          <span>目标成员：{collaborationTargetLabel(item, { ownerView: true })}</span>
          <span>时间：{formatTime(item.occurred_at, 'YYYY-MM-DD HH:mm:ss')} · #{item.id}</span>
        </div>
        {summaryRows.length > 0 && <div className="collaboration-audit-summary">
          {summaryRows.map((row) => <span key={row.label}>{row.label}：{row.value}</span>)}
        </div>}
      </List.Item>
    }}
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

export default function CollaborationAuditPanel({ scope, resourceId, canManage }: CollaborationAuditPanelProps) {
  const actorId = useAuthStore((state) => (state.status === 'authenticated' ? state.user?.id : undefined))
  const mobile = useIsMobile()
  const [generation, setGeneration] = useState(currentRequestGeneration)
  const [form] = Form.useForm<AuthorCollaborationAuditFilterValues>()
  const [query, setQuery] = useState<AuthorCollaborationAuditQuery>({})
  const [filterError, setFilterError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [reloadNonce, setReloadNonce] = useState(0)
  const [loaded, setLoaded] = useState<LoadedState>(EMPTY_LOADED)
  const [selected, setSelected] = useState<{ key: string; event: CollaborationAuditEvent } | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const ticketRef = useRef(0)

  const identityKey = `${scope}:${resourceId}:${actorId ?? ''}:${generation}:${canManage}`

  useEffect(() => {
    const watcher = watchRequestGeneration(() => {
      ticketRef.current += 1
      controllerRef.current?.abort()
      controllerRef.current = null
      setGeneration(currentRequestGeneration())
    })
    return watcher.dispose
  }, [])

  useEffect(() => {
    if (!canManage || actorId === undefined) return
    const ticket = ++ticketRef.current
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const isCurrent = () => {
      const auth = useAuthStore.getState()
      return ticketRef.current === ticket
        && !controller.signal.aborted
        && auth.status === 'authenticated'
        && auth.user?.id === actorId
        && currentRequestGeneration() === generation
    }
    setLoaded((prev) => (prev.key === identityKey
      ? { ...prev, loading: true, error: null }
      : { key: identityKey, data: null, page: 1, loading: true, error: null }))
    void (async () => {
      try {
        const first = await collaborationAuditApi.listForOwner(
          scope, resourceId, { ...query, page, page_size: pageSize }, controller.signal,
        )
        if (!isCurrent()) return
        const validPage = auditValidPage(page, first.total, pageSize)
        const resolved = validPage !== page
          ? await collaborationAuditApi.listForOwner(
            scope, resourceId, { ...query, page: validPage, page_size: pageSize }, controller.signal,
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
  }, [actorId, canManage, generation, identityKey, page, pageSize, query, reloadNonce, resourceId, scope])

  const reload = useCallback(() => setReloadNonce((n) => n + 1), [])

  const applyFilters = (values: AuthorCollaborationAuditFilterValues) => {
    try {
      setQuery(authorCollaborationAuditQuery(values))
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

  if (!canManage) return null

  const state = loaded.key === identityKey ? loaded : EMPTY_LOADED
  const detailEvent = selected?.key === identityKey ? selected.event : null
  const fieldStyle = { flex: '1 1 140px', minWidth: 0, marginBottom: 0 }

  return <section className="collaboration-audit-panel" aria-label={scope === 'project' ? '作品协作审计' : '系列协作审计'}>
    <div className="collaboration-heading">
      <h3>{scope === 'project' ? '作品协作审计' : '系列协作审计'}</h3>
      <Button size="small" icon={<ReloadOutlined />} onClick={reload} disabled={state.loading}>刷新</Button>
    </div>
    <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
      展示本{scope === 'project' ? '作品' : '系列'}的协作变更及合法参与方失败记录；不包含其他用户的越权尝试，邀请不可用账号时不区分账号不存在或已禁用。所有时间按本地时区显示。
    </Typography.Paragraph>
    <Form form={form} layout="vertical" onFinish={applyFilters}>
      <div className="collaboration-audit-filters">
        <Form.Item name="member_q" label="成员用户名快照" style={fieldStyle}>
          <Input allowClear placeholder="支持部分匹配" maxLength={64} />
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
      <Space wrap style={{ marginBottom: 12 }}>
        <Button type="primary" htmlType="submit" disabled={state.loading}>查询</Button>
        <Button onClick={resetFilters} disabled={state.loading}>重置筛选</Button>
      </Space>
    </Form>
    {filterError && <Alert type="error" showIcon message={filterError} style={{ marginBottom: 12 }} />}
    {state.error && <Alert type="error" showIcon message={state.error}
      action={<Button size="small" onClick={reload}>重试</Button>} style={{ marginBottom: 12 }} />}
    <CollaborationAuditItems
      items={state.data?.items ?? []}
      loading={state.loading}
      onDetail={(event) => setSelected({ key: identityKey, event })}
    />
    <div className="collaboration-audit-footer">
      <Typography.Text type="secondary">共 {state.data?.total ?? 0} 条 · 时间与 ID 倒序</Typography.Text>
      <Pagination
        size="small"
        current={state.page}
        pageSize={pageSize}
        total={state.data?.total ?? 0}
        simple={mobile}
        showSizeChanger={!mobile}
        pageSizeOptions={[20, 50, 100]}
        onChange={(next, size) => { setPage(size !== pageSize ? 1 : next); setPageSize(size); reload() }}
      />
    </div>
    <CollaborationAuditDetailDrawer
      event={detailEvent}
      ownerView
      onClose={() => setSelected(null)}
    />
  </section>
}
