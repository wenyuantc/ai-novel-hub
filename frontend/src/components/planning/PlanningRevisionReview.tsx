import { Alert, Button, Checkbox, Space, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { planningApi, readPlanningReviewResource } from '../../api/planning'
import type { PlanningReviewResource } from '../../api/planning'
import type { PlanningMatrix, PlanningRow, PlanningSource } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { PLANNING_REFERENCE_LABELS, planningReferenceId, planningRowLabel } from '../../utils/planning'

interface Props {
  projectId: number; failure: unknown; targets?: PlanningSource[]; originalRows?: PlanningRow[]
  resource?: PlanningReviewResource; subplotId?: number; onAccept: (revision: number) => void
}
interface Review { matrix: PlanningMatrix; revision: number; details: string[]; unavailable: boolean }
/** 冲突后只重读、不重放；作者核对最新目标及待修改内容后才更新表单版本。 */
export default function PlanningRevisionReview({ projectId, failure, targets = [], originalRows = [], resource, subplotId, onAccept }: Props) {
  const { canReadNow, canEditNow } = useProjectAccess(projectId)
  const [latest, setLatest] = useState<Review>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])
  if (!(failure instanceof ApiError) || ![0, 409].includes(failure.status)) return null
  if (failure.body?.code === 'planning_merge_conflict') return null
  const load = async () => {
    if (!canReadNow()) return
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true); setError(''); setConfirmed(false); setLatest(undefined)
    try {
      const matrix = await planningApi.matrix(projectId, { column_mode: 'custom', column_refs: subplotId ? [{ kind: 'subplot', subplot_id: subplotId }] : [], source_refs: targets.length ? targets : undefined, page_size: 100 }, controller.signal)
      const catalog = resource ? await readPlanningReviewResource(projectId, resource, controller.signal) : undefined
      if (catalog && catalog.revision !== matrix.revision) throw new Error('读取期间规划再次变化，请重新加载最新内容')
      const details: string[] = []
      if (subplotId) for (const cell of matrix.cells) if (cell.kind === 'subplot') details.push(cell.node ? `最新推进节点：${cell.node.content}` : '最新状态：本位置没有普通推进节点')
      if (catalog) {
        for (const item of catalog.items) details.push('title' in item ? `交汇「${item.title}」；${item.subplot_ids.length} 条支线；${item.source.kind === 'chapter' ? '章节' : item.source.kind === 'plan' ? '细纲' : '待关联规划'}记录 #${item.source.id}\n${item.description}` : `${item.name}${'description' in item ? `\n${item.description}` : ''}`)
        if (resource?.id === undefined) details.unshift(`当前匹配 ${catalog.total} 项，以下为前 ${catalog.items.length} 项；确认是否已经保存过本次内容。`)
      }
      if (!controller.signal.aborted && canReadNow()) setLatest({ matrix, revision: matrix.revision, details, unavailable: !!resource?.id && !catalog?.items.length })
    } catch (err) { if (!controller.signal.aborted && canReadNow()) setError(errorMessage(err)) }
    finally { if (!controller.signal.aborted && canReadNow()) setLoading(false) }
  }
  const currentRow = (source: PlanningSource) => latest?.matrix.rows.find((row) => source.kind === 'chapter' ? row.chapter_id === source.id : source.kind === 'plan' ? row.plan_id === source.id : row.unit_id === source.id && row.source.kind === 'detached')
  const unavailable = latest?.unavailable || targets.some((source) => !currentRow(source))
  const changedBinding = originalRows.some((old) => {
    const current = currentRow(old.source)
    return old.unit_id !== null && current && current.unit_id !== old.unit_id
  })
  return <Alert type="warning" showIcon message="当前草稿已保留，请核对最新数据后重试" description={<Space direction="vertical" size={8} style={{ width: '100%' }}>
    <Typography.Text>{failure.status === 0 ? '网络失败可能只是响应丢失；重新读取实际结果后再决定是否提交。' : '其他操作已更新规划。重新读取并核对本次操作涉及的数据。'}</Typography.Text>
    <Button onClick={() => void load()} loading={loading}>加载最新版本和目标</Button>
    {error && <Typography.Text type="danger">{error}</Typography.Text>}
    {latest && <>
      {targets.length > 0 && latest.matrix.rows.map((row) => <div key={row.key}><strong>{planningRowLabel(row)}</strong><div><Typography.Text type="secondary">POV：{row.pov_character_name ?? (row.pov_character_id ? `角色 #${row.pov_character_id}` : '未设定')}</Typography.Text>{row.tags.map((tag) => <Tag key={tag.id} color={tag.tone}>{tag.name}</Tag>)}</div><Typography.Text type="secondary">手动关联：{row.references.map((reference) => `${PLANNING_REFERENCE_LABELS[reference.kind]} #${planningReferenceId(reference)}`).join('、') || '无'}</Typography.Text></div>)}
      {latest.details.map((detail, index) => <Typography.Paragraph key={index} style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{detail}</Typography.Paragraph>)}
      {(unavailable || changedBinding) ? <Typography.Text type="danger">部分目标已删除或规划身份改变，请重新选择目标；当前填写内容仍在此处供你核对。</Typography.Text> : <>
        <Checkbox checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}>已核对最新目标，保留当前填写内容重新提交</Checkbox>
        <Button disabled={!confirmed} onClick={() => { if (canEditNow()) onAccept(latest.revision) }}>使用最新版本</Button>
      </>}
    </>}
  </Space>} />
}
