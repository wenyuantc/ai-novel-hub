import { Alert, Button, Descriptions, Drawer, Space, Spin, Tag, Typography, theme } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { AUDIT_ACTION, AUDIT_RESULT, auditLogApi } from '../../api/auditLogs'
import { ApiError, errorMessage } from '../../api/client'
import { versionApi } from '../../api/chapterVersions'
import type { AuditLogDetail, ChapterVersion } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'
import { useAuthStore } from '../../stores/auth'
import { auditSummaryRows, auditTargetLabel, createAuditRequestGate } from '../../utils/auditLogs'
import { formatTime } from '../../utils/time'

interface Props {
  id: number
  actorId: number
  onClose: () => void
}

interface ContentProps {
  log: AuditLogDetail
  onLoadVersion: () => void
  versionLoading: boolean
  version: ChapterVersion | null
  versionError: string | null
}

/** 仅展示安全快照与摘要；绝不复用包含恢复、标星等写操作的版本抽屉。 */
export function AuditLogDetailContent({ log, onLoadVersion, versionLoading, version, versionError }: ContentProps) {
  const { token } = theme.useToken()
  const result = AUDIT_RESULT[log.result]
  const ref = log.version_ref
  return <Space direction="vertical" size={16} style={{ width: '100%', overflowWrap: 'anywhere' }}>
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label="日志 ID">#{log.id}</Descriptions.Item>
      <Descriptions.Item label="发生时间">{formatTime(log.occurred_at, 'YYYY-MM-DD HH:mm:ss.SSS [UTC]Z')}</Descriptions.Item>
      <Descriptions.Item label="真实操作人">{log.actor_username} · #{log.actor_user_id}</Descriptions.Item>
      <Descriptions.Item label="动作">{AUDIT_ACTION[log.action]}<br /><Typography.Text type="secondary">{log.action}</Typography.Text></Descriptions.Item>
      <Descriptions.Item label="结果"><Tag color={result.color}>{result.label}</Tag>{log.action === 'audit.cleanup' && <Tag>受保护，不可清除</Tag>}</Descriptions.Item>
      <Descriptions.Item label="目标快照">{auditTargetLabel(log)}</Descriptions.Item>
      <Descriptions.Item label="所属用户快照">{log.owner_user_id === null ? '不适用 / 未知' : `${log.owner_username ?? '历史用户'} · #${log.owner_user_id}`}</Descriptions.Item>
      <Descriptions.Item label="关联作品快照">{log.project_id === null ? '不适用 / 未知' : `${log.project_title ?? '历史作品'} · #${log.project_id}`}</Descriptions.Item>
      {log.chapter_id !== null && <Descriptions.Item label="章节 ID">#{log.chapter_id}</Descriptions.Item>}
      {log.error_code && <Descriptions.Item label="安全错误类别">{log.error_code}</Descriptions.Item>}
      {log.error_message && <Descriptions.Item label="失败说明">{log.error_message}</Descriptions.Item>}
      <Descriptions.Item label="操作标识">{log.operation_id}</Descriptions.Item>
    </Descriptions>
    <div>
      <Typography.Title level={5} style={{ marginTop: 0 }}>变更摘要</Typography.Title>
      <Typography.Paragraph type="secondary">仅保存字段、状态及数量，不保存正文全文、完整差异或版本备注内容。名称、操作人及归属均为操作时快照。</Typography.Paragraph>
      <Descriptions size="small" column={1} bordered items={auditSummaryRows(log.summary).map((row, i) => ({ key: i, label: row.label, children: row.value }))} />
    </div>
    <div>
      <Typography.Title level={5} style={{ marginTop: 0 }}>已有业务版本关联</Typography.Title>
      {!ref ? <Typography.Text type="secondary">本次操作没有关联业务版本；审计摘要独立保留。</Typography.Text> : <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text>作品 #{ref.project_id} · 章节 #{ref.chapter_id} · 版本 #{ref.version_id}（记录时第 {ref.seq} 版）</Typography.Text>
        <Alert showIcon type={log.version_status === 'available' ? 'info' : 'warning'}
          message={log.version_status === 'missing' ? '关联版本已不可用' : log.version_status === 'changed' ? '关联版本内容已变化' : '业务版本可变，不是审计全文快照'}
          description="业务版本可能合并、裁剪或删除。主动查看的是该版本当前正文，不能视为本次操作时刻的确定全文；关联失效不影响审计摘要。" />
        {log.version_status !== 'missing' && <Button loading={versionLoading} disabled={versionLoading} onClick={onLoadVersion}>查看关联版本当前正文</Button>}
        {versionError && <Alert showIcon type="warning" message={versionError} action={<Button size="small" disabled={versionLoading} onClick={onLoadVersion}>重试</Button>} />}
        {version && <div>
          <Typography.Paragraph type="secondary">独立读取的业务版本 · 当前第 {version.seq} 版 · {version.word_count} 字 · 更新于 {formatTime(version.updated_at)}。以下不是审计保存的正文。</Typography.Paragraph>
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 420, overflowY: 'auto', padding: 12, margin: 0, borderRadius: token.borderRadius, background: token.colorFillQuaternary, fontFamily: 'inherit' }}>{version.content || '（空正文）'}</pre>
        </div>}
      </Space>}
    </div>
  </Space>
}

export default function AuditLogDetailDrawer({ id, actorId, onClose }: Props) {
  const width = useModalWidth(740)
  const [gate] = useState(createAuditRequestGate)
  const pending = useRef(false)
  const [version, setVersion] = useState<ChapterVersion | null>(null)
  const [versionLoading, setVersionLoading] = useState(false)
  const [versionError, setVersionError] = useState<string | null>(null)
  const loader = useCallback(async () => {
    try { return await auditLogApi.get(id) }
    catch (err) {
      if (err instanceof ApiError && err.status === 404) throw new Error('这条日志已不存在，可能已被其他管理员清理。可关闭后刷新列表。', { cause: err })
      throw err
    }
  }, [id])
  const { data, error, loading, reload } = useLoader(loader)
  useEffect(() => () => gate.invalidate(), [gate])
  const isCurrent = (ticket: number) => gate.isCurrent(ticket) && useAuthStore.getState().user?.id === actorId && useAuthStore.getState().user?.is_admin === true
  const close = () => { gate.invalidate(); onClose() }
  const loadVersion = async () => {
    const ref = data?.version_ref
    if (!ref || pending.current) return
    pending.current = true
    const ticket = gate.begin()
    setVersionLoading(true)
    setVersionError(null)
    setVersion(null)
    try {
      const next = await versionApi.get(ref.project_id, ref.chapter_id, ref.version_id)
      if (!isCurrent(ticket)) return
      if (next.id !== ref.version_id || next.chapter_id !== ref.chapter_id) throw new Error('关联版本不匹配，无法展示正文')
      setVersion(next)
    } catch (err) {
      if (isCurrent(ticket)) setVersionError(err instanceof ApiError && err.status === 404 ? '关联版本已被删除或不可访问，审计摘要仍然保留。' : errorMessage(err))
    } finally {
      if (isCurrent(ticket)) { pending.current = false; setVersionLoading(false) }
    }
  }
  return <Drawer open title="审计详情（只读）" width={width} onClose={close}>
    {error && <Alert showIcon type="error" message={error} action={<Button onClick={reload} size="small">重试</Button>} style={{ marginBottom: 16 }} />}
    {loading && !data && <div style={{ padding: 32, textAlign: 'center' }}><Spin tip="正在加载审计详情…"><div style={{ minHeight: 40 }} /></Spin></div>}
    {data && <AuditLogDetailContent log={data} onLoadVersion={() => void loadVersion()} version={version} versionLoading={versionLoading} versionError={versionError} />}
  </Drawer>
}
