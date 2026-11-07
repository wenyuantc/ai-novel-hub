import { Alert, Button, Card, Empty, Pagination, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { sceneApi } from '../../api/scenes'
import type { SceneBrief } from '../../api/types'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import PlanningBatchModal from '../planning/PlanningBatchModal'
import PlanningPreviewDrawer from '../planning/PlanningPreviewDrawer'
import { sceneLabel, scenePlanningRow } from './scenePresentation'

interface Props { projectId: number }
/** 项目级轻量分页网格，不为卡片加载整书正文。 */
export default function SceneGrid({ projectId }: Props) {
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<SceneBrief>()
  const [preview, setPreview] = useState<SceneBrief>()
  const access = useProjectAccess(projectId)
  const load = useCallback((signal: AbortSignal) => sceneApi.directory(projectId, { offset: (page - 1) * 24, limit: 24 }, signal), [projectId, page])
  const result = useMentionQuery(projectId, `scene-grid:${page}`, load)
  useProjectRefresh(projectId, ['planning', 'chapters', 'outlines'], result.reload)
  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <Typography.Paragraph type="secondary">按章与场景顺序展示已保存的作者摘要、有效 POV 与标签。规划覆盖和 AI 预览与编辑器、矩阵共用同一来源；手动引用不代表正文已出场。</Typography.Paragraph>
    {result.loading && <Spin />}
    {result.error && <Alert type="error" showIcon message={result.error} action={<Button onClick={result.reload}>重新加载</Button>} />}
    {result.data && <>
      {!result.data.items.length && <Empty description="暂无场景，请先创建章节" />}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 280px), 1fr))', gap: 12 }}>
        {result.data.items.map((scene) => <Card key={scene.id} size="small" title={<Link to={`/projects/${projectId}/chapters/${scene.chapter_id}?scene_id=${scene.id}`}>{sceneLabel(scene)}</Link>} styles={{ title: { whiteSpace: 'normal' } }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Text type="secondary">第 {scene.chapter_no} 章 · {scene.chapter_title || '未命名'} · {scene.word_count ? `${scene.word_count} 字` : '未写'}</Typography.Text>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{scene.summary || '尚无作者摘要'}</Typography.Paragraph>
            <div>POV：{scene.planning.effective.pov_character_name ?? '未指定'} <Tag>{scene.planning.effective.sources.pov === 'chapter' ? '继承本章' : '本场景覆盖'}</Tag></div>
            <Space wrap size={4}>{scene.planning.effective.tags.map((tag) => <Tag color={tag.tone} key={tag.id}>{tag.name}</Tag>)}<Typography.Text type="secondary">标签：{scene.planning.effective.sources.tags === 'chapter' ? '继承' : '覆盖'} · 手动引用 {scene.planning.effective.references.length} 项</Typography.Text></Space>
            <Space wrap><Button size="small" disabled={!access.canEdit} onClick={() => setEditing(scene)}>编辑规划覆盖</Button><Button size="small" onClick={() => setPreview(scene)}>AI 规划预览</Button></Space>
          </Space>
        </Card>)}
      </div>
      <Pagination current={page} pageSize={24} total={result.data.total} onChange={setPage} showSizeChanger={false} showTotal={(total) => `共 ${total} 个实际场景`} />
    </>}
    {editing && <PlanningBatchModal projectId={projectId} rows={[scenePlanningRow(editing)]} revision={editing.planning_revision} onClose={() => setEditing(undefined)} onSaved={() => { setEditing(undefined); result.reload() }} />}
    {preview && <PlanningPreviewDrawer projectId={projectId} row={scenePlanningRow(preview)} onClose={() => setPreview(undefined)} />}
  </Space>
}
