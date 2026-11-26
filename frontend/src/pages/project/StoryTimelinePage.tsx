import { Alert, Button, Empty, Pagination, Radio } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { storyTimelineApi, type StoryCalendar, type TimelineItem, type TimelineOrder } from '../../api/storyTimeline'
import { StudioPage } from '../../components/StudioUI'
import { QueryFeedback } from '../../components/serialization/SerializationLists'
import CalendarDialog from '../../components/storyTimeline/CalendarDialog'
import DateDialog from '../../components/storyTimeline/DateDialog'
import { dateText, itemTitle, manuscriptHref } from '../../components/storyTimeline/timelineModel'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectStore } from '../../stores/project'
import '../../components/storyTimeline/storyTimeline.css'

const pageSize = 30

function Workbench({ pid }: { pid: number }) {
  const { canEdit, canReadNow, canRead } = useProjectAccess(pid)
  const [order, setOrder] = useState<TimelineOrder>('story')
  const [page, setPage] = useState(1)
  const [calendarBase, setCalendarBase] = useState<StoryCalendar>()
  const [target, setTarget] = useState<TimelineItem>()
  const loadCalendar = useCallback((signal: AbortSignal) => storyTimelineApi.calendar(pid, { signal, isCurrent: canReadNow }), [pid, canReadNow])
  const calendar = useMentionQuery(pid, 'story-calendar', loadCalendar, canRead)
  const loadPage = useCallback((signal: AbortSignal) =>
    storyTimelineApi.list(pid, order, (page - 1) * pageSize, pageSize, { signal, isCurrent: canReadNow }), [pid, order, page, canReadNow])
  const rows = useMentionQuery(pid, `story-timeline:${order}:${page}`, loadPage, canRead)
  const reload = () => { rows.reload(); calendar.reload() }
  const items = rows.data?.items ?? []
  function list(group: TimelineItem[]) {
    return <ul className="story-timeline-list">{group.map((item) => <li key={`${item.kind}:${item.scene_id ?? item.chapter_id}`}>
      <Link to={manuscriptHref(pid, item)}>{itemTitle(item)}</Link>
      <span>{dateText(item)}</span>
      {canEdit && <Button size="small" onClick={() => setTarget(item)}>编辑日期</Button>}
    </li>)}</ul>
  }
  const unmarked = items.filter((item) => !item.effective_date)
  return <StudioPage title="故事时间线" action={canEdit && <Button disabled={!calendar.data} onClick={() => setCalendarBase(calendar.data)}>设置作品历法</Button>}>
    <section className="story-timeline-content">
      <QueryFeedback {...calendar} onReload={calendar.reload} />
      {calendar.data && <p>{calendar.data.era_name ? `作品历法 · ${calendar.data.era_name}` : '尚未设置作品历法'}</p>}
      <Radio.Group aria-label="时间线顺序" value={order} onChange={(e) => { setOrder(e.target.value); setPage(1) }}>
        <Radio.Button value="story">故事顺序</Radio.Button><Radio.Button value="chapter">章节顺序</Radio.Button>
      </Radio.Group>
      <QueryFeedback {...rows} onReload={rows.reload} />
      {rows.data && <>
        {order === 'chapter' ? list(items) : <>
          {list(items.filter((item) => item.effective_date))}
          {unmarked.length > 0 && <><h2>未标时间</h2>{list(unmarked)}</>}
        </>}
        {!rows.data.total && <Empty description="还没有章节" />}
        <Pagination current={page} pageSize={pageSize} total={rows.data.total} hideOnSinglePage={page === 1}
          aria-label="时间线分页" showSizeChanger={false} onChange={setPage} />
      </>}
    </section>
    {calendarBase && canEdit && <CalendarDialog pid={pid} initial={calendarBase} onClose={() => setCalendarBase(undefined)} onChanged={reload} />}
    {target && canEdit && <DateDialog pid={pid} kind={target.kind} id={target.scene_id ?? target.chapter_id}
      onClose={() => setTarget(undefined)} onSaved={() => { setTarget(undefined); reload() }} />}
  </StudioPage>
}

export default function StoryTimelinePage() {
  const pid = useProjectId()
  const scope = useProjectStore((s) => s.scopeVersion)
  const { canRead } = useProjectAccess(pid)
  if (!canRead) return <Alert type="warning" message="没有读取此作品的权限" showIcon />
  return <Workbench key={`${pid}:${scope}`} pid={pid} />
}
