import { Alert, Button, Modal, Spin, Typography } from 'antd'
import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { progressionApi } from '../../api/progressions'
import type { Character } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { RelationPairHistory } from './RelationHistoryModal'

/** 按稳定时间线打开历史，关系当前已隐藏或没有载体时仍可查看。 */
export default function RelationTimelineDeepLink({ projectId, characters }: { projectId: number; characters: Character[] }) {
  const [params, setParams] = useSearchParams()
  const rawId = params.get('timeline_id')
  const timelineId = Number(rawId)
  const valid = rawId !== null && Number.isSafeInteger(timelineId) && timelineId > 0
  const access = useProjectAccess(projectId)
  const width = useModalWidth(680)
  const load = useCallback((signal: AbortSignal) => progressionApi.listTimelines(projectId, { subject_kind: 'relationship' }, signal), [projectId])
  const result = useMentionQuery(projectId, `relationship-link:${timelineId}`, load, valid && access.canRead)
  const timeline = result.data?.items.find((item) => item.id === timelineId && item.project_id === projectId && item.subject_kind === 'relationship')
  const close = () => setParams((old) => { const next = new URLSearchParams(old); next.delete('timeline_id'); return next }, { replace: true })
  if (rawId === null || !access.canRead) return null
  const name = (id: number) => characters.find((character) => character.id === id)?.name ?? `角色 #${id}`
  return <Modal open title="人物关系时间线" width={width} footer={null} onCancel={close}>
    {result.loading && <Spin />}
    {result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
    {(!valid || (!result.loading && !result.error && !timeline)) && <Alert type="warning" message="关系时间线不存在或已被删除" />}
    {timeline?.character_id && timeline.related_character_id && <>
      <Typography.Paragraph>{name(timeline.character_id)} ↔ {name(timeline.related_character_id)}</Typography.Paragraph>
      <RelationPairHistory key={`${projectId}:${timeline.id}`} projectId={projectId} pair={[timeline.character_id, timeline.related_character_id]} />
    </>}
  </Modal>
}
