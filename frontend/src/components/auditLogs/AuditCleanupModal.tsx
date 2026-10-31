import { Alert, Button, Checkbox, Descriptions, Form, Input, Modal, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { auditLogApi } from '../../api/auditLogs'
import { errorMessage } from '../../api/client'
import type { AuditCleanupPreview, AuditCleanupResult, AuditDateRange } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useAuthStore } from '../../stores/auth'
import { auditDateRange, auditRangeLabel, cleanupPreviewUsable, createAuditRequestGate } from '../../utils/auditLogs'
import { formatTime, parseServerTime } from '../../utils/time'

interface Props {
  actorId: number
  onClose: () => void
  onCleaned: (result: AuditCleanupResult) => void
}

/** 单独导出可读预览供 node 渲染测试；令牌绝不渲染进 DOM。 */
export function AuditCleanupPreviewContent({ preview }: { preview: AuditCleanupPreview }) {
  return <Space direction="vertical" style={{ width: '100%', overflowWrap: 'anywhere' }}>
    <Descriptions column={1} bordered size="small">
      <Descriptions.Item label="本地时间范围">{auditRangeLabel(preview)}</Descriptions.Item>
      <Descriptions.Item label="UTC 范围">[{preview.from_at}, {preview.to_at})</Descriptions.Item>
      <Descriptions.Item label="预计可删除">{preview.count.toLocaleString('zh-CN')} 条</Descriptions.Item>
      <Descriptions.Item label="受保护">{preview.protected_count.toLocaleString('zh-CN')} 条（保留）</Descriptions.Item>
      <Descriptions.Item label="预览有效至">{formatTime(preview.expires_at, 'YYYY-MM-DD HH:mm:ss [UTC]Z')}</Descriptions.Item>
    </Descriptions>
    <Alert showIcon type="warning" message="删除不可恢复，请再次核对日期"
      description="将清理该日期范围内全部可清理记录，不继承列表中的操作人、动作、结果或目标筛选。成功和失败的 audit.cleanup 清理事件均受保护，永不通过此入口删除。并发操作可能使实际删除数与预计不同；本次最多清理预览高水位以内的记录，不包含之后新分配 ID 的记录。" />
  </Space>
}

/** 打开时挂载、关闭时卸载；范围变化及账号变化也会使所有旧响应失效。 */
export default function AuditCleanupModal({ actorId, onClose, onCleaned }: Props) {
  const width = useModalWidth(640)
  const [gate] = useState(createAuditRequestGate)
  const pending = useRef(false)
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [preview, setPreview] = useState<AuditCleanupPreview | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AuditCleanupResult | null>(null)
  let range: AuditDateRange | null = null
  try { range = auditDateRange(fromDate, toDate) } catch { /* 未完成选择时只禁用预览，提交时显示明确错误。 */ }
  const usable = cleanupPreviewUsable(preview, range)
  const isCurrent = (ticket: number) => gate.isCurrent(ticket) && useAuthStore.getState().user?.id === actorId && useAuthStore.getState().user?.is_admin === true

  useEffect(() => () => gate.invalidate(), [gate])
  useEffect(() => {
    if (!preview) return
    const timer = setTimeout(() => {
      gate.invalidate()
      pending.current = false
      setPreview(null)
      setConfirmed(false)
      setError('清理预览已过期，请重新预览后确认。')
    }, Math.max(0, parseServerTime(preview.expires_at).valueOf() - Date.now()))
    return () => clearTimeout(timer)
  }, [preview, gate])

  const resetPreview = () => {
    gate.invalidate()
    pending.current = false
    setPreview(null)
    setConfirmed(false)
    setPreviewing(false)
    setError(null)
    setResult(null)
  }
  const close = () => { gate.invalidate(); onClose() }
  const handlePreview = async () => {
    if (pending.current) return
    let requested: AuditDateRange
    try { requested = auditDateRange(fromDate, toDate) }
    catch (err) { setError(errorMessage(err)); return }
    pending.current = true
    const ticket = gate.begin()
    setPreview(null)
    setConfirmed(false)
    setResult(null)
    setError(null)
    setPreviewing(true)
    try {
      const next = await auditLogApi.previewCleanup(requested)
      if (isCurrent(ticket)) setPreview(next)
    } catch (err) {
      if (isCurrent(ticket)) setError(errorMessage(err))
    } finally {
      if (isCurrent(ticket)) { pending.current = false; setPreviewing(false) }
    }
  }
  const handleCleanup = async () => {
    if (pending.current || !confirmed || !preview) return
    if (!cleanupPreviewUsable(preview, range)) {
      resetPreview()
      setError('清理预览已过期或日期范围变化，请重新预览后确认。')
      return
    }
    pending.current = true
    const ticket = gate.begin()
    const token = preview.preview_token
    // 执行开始即消费本地预览：网络失败、过期或 409 后均必须重新预览。
    setPreview(null)
    setConfirmed(false)
    setExecuting(true)
    setError(null)
    try {
      const next = await auditLogApi.cleanup({ preview_token: token, confirm: true })
      if (!isCurrent(ticket)) return
      setResult(next)
      onCleaned(next)
    } catch (err) {
      if (isCurrent(ticket)) setError(`${errorMessage(err)}。请重新预览；若请求结果不确定，请先刷新列表核对清理留痕。`)
    } finally {
      if (isCurrent(ticket)) { pending.current = false; setExecuting(false) }
    }
  }
  return <Modal open title="按日期清理审计日志" width={width} onCancel={close}
    closable={!executing} keyboard={!executing} maskClosable={!executing} destroyOnHidden
    footer={<Space wrap style={{ justifyContent: 'flex-end', width: '100%' }}>
      <Button onClick={close} disabled={executing}>{result ? '完成' : '取消'}</Button>
      {!result && <Button onClick={() => void handlePreview()} loading={previewing} disabled={!range || executing || previewing}>预览清理范围</Button>}
      {!result && <Button danger type="primary" loading={executing} disabled={!usable || !confirmed || previewing || executing} onClick={() => void handleCleanup()}>确认清理{preview ? ` ${preview.count.toLocaleString('zh-CN')} 条` : ''}</Button>}
    </Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%', overflowWrap: 'anywhere' }}>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>审计日志长期保留，不自动过期。以下日期独立于列表筛选，按浏览器本地时区选择，包含结束日期全天；后端以 UTC 半开区间执行。</Typography.Paragraph>
      <Form layout="vertical" disabled={executing}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
          <Form.Item label="开始日期（含）" style={{ flex: '1 1 180px', minWidth: 0, marginBottom: 0 }}>
            <Input aria-label="清理开始日期" type="date" value={fromDate} onChange={(e) => { resetPreview(); setFromDate(e.target.value) }} style={{ width: '100%', minWidth: 0 }} />
          </Form.Item>
          <Form.Item label="结束日期（含）" style={{ flex: '1 1 180px', minWidth: 0, marginBottom: 0 }}>
            <Input aria-label="清理结束日期" type="date" value={toDate} onChange={(e) => { resetPreview(); setToDate(e.target.value) }} style={{ width: '100%', minWidth: 0 }} />
          </Form.Item>
        </div>
      </Form>
      {error && <Alert type="error" showIcon message={error} />}
      {preview && <>
        <AuditCleanupPreviewContent preview={preview} />
        {preview.count === 0 ? <Alert type="info" showIcon message="该范围没有可清理记录；受保护的清理事件将继续保留。" />
          : <Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} disabled={!usable || executing}>我已核对日期范围，了解将清理范围内全部可清理记录，且不可恢复。</Checkbox>}
      </>}
      {executing && <Alert type="info" showIcon message="正在执行清理，请勿重复提交。" />}
      {result && <Alert type="success" showIcon message={`清理完成，实际删除 ${result.deleted_count.toLocaleString('zh-CN')} 条`}
        description={`清理留痕 #${result.audit_log_id} 已保留且受保护。当前列表已刷新；实际数量可能与预览不同。`} />}
    </Space>
  </Modal>
}
