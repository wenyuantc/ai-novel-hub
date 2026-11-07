import { Alert, App, Button, Segmented, Space, Spin } from 'antd'
import { lazy, Suspense, useState } from 'react'
import { Link } from 'react-router-dom'
import type { SeriesKind } from '../../api/series'
import type { Project, SettingOrigin } from '../../api/types'
import { useProjectStore } from '../../stores/project'
import { invalidateProject } from '../../utils/projectRefresh'
import SeriesTransitionDialog from './SeriesTransitionDialog'
import { SERIES_SCOPE_OPTIONS, seriesSettingResources, type SettingScope } from './seriesState'

const SeriesLibrary = lazy(() => import('./SeriesLibrary'))

export function BookSeriesScope({ value, onChange, series }: {
  value: SettingScope; onChange: (value: SettingScope) => void; series: NonNullable<Project['series']>
}) {
  return <Space wrap style={{ marginBlock: 8 }}>
    <Segmented<SettingScope> aria-label="按设定归属筛选" value={value} onChange={onChange} options={SERIES_SCOPE_OPTIONS} />
    <Link to={`/series/${series.id}`}>系列：{series.name}</Link>
  </Space>
}

export function BookSeriesLibrary({ series, projectId, kind }: {
  series: NonNullable<Project['series']>; projectId: number; kind: SeriesKind
}) {
  const title = useProjectStore((state) => state.project?.id === projectId ? state.project.title : '本书')
  return <Suspense fallback={<Spin aria-label="正在读取系列设定" />}>
    <SeriesLibrary seriesId={series.id} seriesRevision={series.revision} kind={kind} book={{ id: projectId, title }} />
  </Suspense>
}

export function BookSeriesPromotion({ seriesId, projectId, kind, id, title, onClose }: {
  seriesId: number; projectId: number; kind: SeriesKind; id: number; title: string; onClose: () => void
}) {
  const { message } = App.useApp()
  const [request] = useState(() => ({ action: 'promote' as const, project_id: projectId, kind, entry_id: id }))
  return <SeriesTransitionDialog seriesId={seriesId} request={request} label={title} onClose={onClose}
    onDone={(ids) => {
      for (const pid of ids) invalidateProject(pid, seriesSettingResources(kind))
      onClose()
      message.success('已提升为系列共享')
    }} />
}

export function SharedSettingNotice({ origin, onUnuse }: { origin?: SettingOrigin; onUnuse?: () => void }) {
  if (origin?.scope !== 'series') return null
  const canOpen = origin.series_id != null && origin.series_entry_id != null
  return <Alert type="info" showIcon message="系列基础 · 本书演变"
    description="名称、档案基础和条目级 AI 规则在系列库维护。此处可维护本书自己的状态、字段、关联与剧情变化。"
    action={canOpen && <Space direction="vertical">
      <Link to={`/series/${origin.series_id}?entry=${origin.series_entry_id}`}>打开共享档案</Link>
      {onUnuse && <Button type="text" size="small" onClick={onUnuse}>取消在本书使用</Button>}
    </Space>} style={{ marginBottom: 12 }} />
}

export function BookSeriesUnuse({ seriesId, seriesEntryId, projectId, title, kind, onClose, onDone }: {
  seriesId: number; seriesEntryId: number; projectId: number; title: string; kind: SeriesKind; onClose: () => void; onDone: () => void
}) {
  const { message } = App.useApp()
  const [request] = useState(() => ({ action: 'unuse' as const, project_id: projectId, entry_id: seriesEntryId }))
  return <SeriesTransitionDialog seriesId={seriesId} request={request} label={title} onClose={onClose}
    onDone={(ids) => {
      onDone()
      for (const pid of ids) invalidateProject(pid, seriesSettingResources(kind))
      message.success('已取消在本书使用')
    }} />
}
