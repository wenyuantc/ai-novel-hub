import { Button, Collapse, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { Scene, ScenePlanningField } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { entityDetailHref } from '../../utils/entityLinks'
import { PLANNING_REFERENCE_LABELS, planningReferenceId } from '../../utils/planning'
import PlanningBatchModal from '../planning/PlanningBatchModal'
import PlanningPreviewDrawer from '../planning/PlanningPreviewDrawer'
import { scenePlanningRow } from './scenePresentation'

interface Props { projectId: number; scene: Scene; disabled: boolean; ensureSaved: () => Promise<boolean>; onChanged: () => void }
/** 显示已保存的逐项来源；覆盖编辑复用矩阵的同一批量合同。 */
export default function ScenePlanningPanel({ projectId, scene, disabled, ensureSaved, onChanged }: Props) {
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const [editing, setEditing] = useState(false)
  const [preview, setPreview] = useState(false)
  const row = scenePlanningRow(scene)
  const source = (field: ScenePlanningField) => <Tag>{scene.planning.effective.sources[field] === 'chapter' ? '继承本章' : '本场景覆盖'}</Tag>
  const edit = async () => { if (canEditNow() && await ensureSaved() && canEditNow()) setEditing(true) }
  return <>
    <Collapse size="small" items={[{ key: 'planning', label: '场景规划 · 继承与覆盖', children: <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <div><Typography.Text strong>视角人物（POV）</Typography.Text> {source('pov')} {scene.planning.effective.pov_character_name ?? (scene.planning.effective.pov_character_id ? `角色 #${scene.planning.effective.pov_character_id}` : '明确无／尚未指定')}</div>
      <div><Typography.Text strong>标签</Typography.Text> {source('tags')} {scene.planning.effective.tags.map((tag) => <Tag key={tag.id} color={tag.tone}>{tag.name}</Tag>)}{!scene.planning.effective.tags.length && '无标签'}</div>
      {(Object.keys(PLANNING_REFERENCE_LABELS) as (keyof typeof PLANNING_REFERENCE_LABELS)[]).map((kind) => {
        const refs = scene.planning.effective.references.filter((item) => item.kind === kind)
        return <div key={kind}><Typography.Text strong>{PLANNING_REFERENCE_LABELS[kind]}</Typography.Text> {source(kind)} {refs.map((ref) => <Tag key={planningReferenceId(ref)}>{ref.kind === 'relationship' ? <Link to={`/projects/${projectId}/relationships?timeline_id=${ref.timeline_id}`}>人物关系 #{ref.timeline_id}</Link> : <Link to={entityDetailHref(projectId, ref)}>{PLANNING_REFERENCE_LABELS[kind]} #{ref.id}</Link>}</Tag>)}{!refs.length && '无手动关联'}</div>
      })}
      <Typography.Text type="secondary">以上为已保存的有效值。明确无只清空手动项，不禁止自动候选或改变设定 AI 可见策略；POV 不自动指定人称。</Typography.Text>
      <Space wrap><Button disabled={!canEdit || disabled} onClick={() => void edit()}>设置继承／本场景自定义</Button><Button onClick={() => setPreview(true)}>预览实际 AI 规划</Button><Link to={`/projects/${projectId}/planning`}>打开规划矩阵</Link></Space>
    </Space> }]} />
    {editing && <PlanningBatchModal projectId={projectId} rows={[row]} revision={scene.planning_revision} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); onChanged() }} />}
    {preview && <PlanningPreviewDrawer projectId={projectId} row={row} onClose={() => setPreview(false)} />}
  </>
}
