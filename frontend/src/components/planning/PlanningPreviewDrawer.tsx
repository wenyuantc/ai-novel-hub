import { Alert, Button, Drawer, Segmented, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { planningApi } from '../../api/planning'
import type { PlanningRow } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useMentionQuery as usePlanningQuery } from '../../hooks/useMentionQuery'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { PLANNING_REFERENCE_LABELS, planningRowLabel } from '../../utils/planning'

interface Props { projectId: number; row: PlanningRow; onClose: () => void }
export default function PlanningPreviewDrawer({ projectId, row, onClose }: Props) {
  const [scope, setScope] = useState<'chapter' | 'selection'>('chapter')
  const load = useCallback((signal: AbortSignal) => planningApi.preview(projectId, row.source, scope, signal), [projectId, row.source, scope])
  const result = usePlanningQuery(projectId, `preview:${row.key}:${scope}`, load)
  useProjectRefresh(projectId, ['planning', 'characters', 'world', 'relationships', 'organizations', 'careers', 'chapters', 'outlines'], result.reload)
  const width = useModalWidth(760)
  return <Drawer open title={`AI 规划内容 · ${planningRowLabel(row)}`} width={width} onClose={onClose}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Segmented aria-label="AI规划用途" value={scope} options={[{ value: 'chapter', label: '生成／整章重写' }, { value: 'selection', label: '局部改写／续写' }]} onChange={setScope} />
      <Typography.Text type="secondary">这里只展示将传入 AI 的规划部分。正式生成还会组合正文、风格与其他上下文。</Typography.Text>
      {row.chapter_id === null && <Alert type="info" showIcon message="尚未建章：这里只预览规划指令" description="建章后才按真实章号解析历史有效设定，并使用现有正文生成入口。" />}
      {result.loading && <Spin />}
      {result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
      {result.data && <>
        <Space wrap>{Object.entries(PLANNING_REFERENCE_LABELS).filter(([kind]) => result.data!.references.some((reference) => reference.kind === kind)).map(([kind, label]) => <Tag key={kind}>{label}资料</Tag>)}<Tag>{result.data.node_ids.length} 个推进目标</Tag><Tag>{result.data.crossing_ids.length} 个明确交汇</Tag></Space>
        {result.data.warnings.map((warning) => <Alert key={warning} type="warning" showIcon message={warning} />)}
        {result.data.truncated && <Alert type="warning" message="规划内容较长，预览已按预算截断。正式调用会按模型预算重新分配。" />}
        <pre className="planning-preview">{result.data.text || '本位置暂无需要额外传入的规划内容。'}</pre>
      </>}
    </Space>
  </Drawer>
}
