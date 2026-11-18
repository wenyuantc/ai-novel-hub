import { SettingOutlined } from '@ant-design/icons'
import { Alert, Button, Skeleton } from 'antd'
import { useCallback, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { serializationApi, type SerializationOverview } from '../../api/serialization'
import { StudioPage } from '../../components/StudioUI'
import DailyGoalModal from '../../components/serialization/DailyGoalModal'
import ReleaseCalendar from '../../components/serialization/ReleaseCalendar'
import SerializationChapterDrawer from '../../components/serialization/SerializationChapterDrawer'
import SerializationPublicationDrawer from '../../components/serialization/SerializationPublicationDrawer'
import WorkbenchLibrary from '../../components/serialization/WorkbenchLibrary'
import { deepLinkId } from '../../components/serialization/calendarModel'
import { QueryFeedback } from '../../components/serialization/SerializationLists'
import { beijingMinute } from '../../components/serialization/serializationForm'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import './SerializationPage.css'

function StockSummary({ data }: { data: SerializationOverview }) {
  return <dl className="serialization-stats" aria-label="全书连载统计">
    <div><dt>可发布存稿</dt><dd><strong>{data.stock_count}</strong> 章</dd></div>
    <div><dt>存稿可支撑</dt><dd>{data.support_days === null ? <span className="serialization-unset">未设置日更目标</span> : <>
      <strong>{data.support_days}</strong> 天{(data.remaining_chapters ?? 0) > 0 && <small>余 {data.remaining_chapters} 章</small>}</>}</dd></div>
    <div><dt>日更目标</dt><dd>{data.daily_goal === null ? <span className="serialization-unset">未设置</span>
      : <><strong>{data.daily_goal}</strong> 章 / 天</>}</dd></div>
  </dl>
}

function Workbench({ projectId }: { projectId: number }) {
  const { canReadNow, canEditNow, canEdit, canExport } = useProjectAccess(projectId)
  const [params, setParams] = useSearchParams()
  const chapterId = deepLinkId(params.get('chapter'))
  const publicationId = deepLinkId(params.get('publication'))
  const [goalBase, setGoalBase] = useState<SerializationOverview>()
  const [date, setDate] = useState(() => beijingMinute().slice(0, 10))
  const month = date.slice(0, 7)
  const [version, setVersion] = useState(0)
  const reload = useCallback(() => setVersion((n) => n + 1), [])
  const load = useCallback((signal: AbortSignal) => serializationApi.overview(projectId, month, { signal, isCurrent: canReadNow }), [projectId, month, canReadNow])
  const summary = useMentionQuery(projectId, `overview:${month}:${version}`, load)
  useProjectRefresh(projectId, ['chapters'], reload)
  function select(key: 'chapter' | 'publication', id: number | null) {
    if (!canReadNow()) return
    setParams((previous) => {
      const next = new URLSearchParams(previous)
      if (key === 'chapter') next.delete('publication')
      if (id === null) next.delete(key)
      else next.set(key, String(id))
      return next
    }, { replace: true })
  }
  const onChapter = (id: number) => select('chapter', id)
  const onPublication = (id: number) => select('publication', id)
  return <div className="serialization-workbench"><StudioPage title="连载与存稿"
    action={canEdit && <Button icon={<SettingOutlined />} disabled={!summary.data} onClick={() => {
      if (canEditNow()) setGoalBase(summary.data)
    }}>设置日更目标</Button>}>
    <QueryFeedback {...summary} onReload={summary.reload} />
    {summary.data && <StockSummary data={summary.data} />}
    <ReleaseCalendar projectId={projectId} date={date} onDate={setDate} overview={summary.data} version={version}
      onChapter={onChapter} onPublication={onPublication} />
    <WorkbenchLibrary projectId={projectId} version={version} onChapter={onChapter} onPublication={onPublication} />
    {goalBase && canEdit && <DailyGoalModal projectId={projectId} initial={goalBase} month={month}
      onClose={() => setGoalBase(undefined)} onSaved={() => { setGoalBase(undefined); reload() }} />}
    {chapterId !== null && publicationId === null && <SerializationChapterDrawer key={chapterId} projectId={projectId} chapterId={chapterId}
      canEdit={canEdit} canExport={canExport} onPublication={onPublication} onChanged={reload} onClose={() => select('chapter', null)} />}
    {publicationId !== null && <SerializationPublicationDrawer key={publicationId} projectId={projectId} publicationId={publicationId}
      canEdit={canEdit} canExport={canExport} onChanged={reload} onClose={() => select('publication', null)} />}
  </StudioPage></div>
}

export default function SerializationPage() {
  const projectId = useProjectId()
  const { ready, canRead } = useProjectAccess(projectId)
  const actor = useAuthStore((s) => s.user)
  const scope = useProjectStore((s) => s.scopeVersion)
  if (!ready || !actor) return <Skeleton active />
  if (!canRead) return <Alert type="warning" message="没有读取此作品的权限" showIcon />
  return <Workbench key={`${projectId}:${actor.id}:${scope}`} projectId={projectId} />
}
