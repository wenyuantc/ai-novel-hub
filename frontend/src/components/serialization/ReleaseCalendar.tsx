import { LeftOutlined, RightOutlined } from '@ant-design/icons'
import { Button, Input, Radio } from 'antd'
import dayjs from 'dayjs'
import { useCallback, useState } from 'react'
import { serializationApi, type SerializationOverview } from '../../api/serialization'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { monthCells, SERIALIZATION_PAGE_SIZE } from './calendarModel'
import { ChapterList, PageNav, PublicationList, QueryFeedback } from './SerializationLists'
import { beijingMinute } from './serializationForm'

interface Props {
  projectId: number
  date: string
  onDate: (date: string) => void
  overview?: SerializationOverview
  version: number
  onChapter: (id: number) => void
  onPublication: (id: number) => void
}

function DayAgenda({ projectId, date, version, overview, onChapter, onPublication }: Omit<Props, 'onDate'>) {
  const { canReadNow } = useProjectAccess(projectId)
  const [mode, setMode] = useState<'planned' | 'first_published'>('planned')
  const [page, setPage] = useState(1)
  const load = useCallback((signal: AbortSignal) => serializationApi.day(projectId, date, {
    offset: (page - 1) * SERIALIZATION_PAGE_SIZE, limit: SERIALIZATION_PAGE_SIZE,
  }, { signal, isCurrent: canReadNow }), [projectId, date, page, canReadNow])
  const query = useMentionQuery(projectId, `agenda:${date}:${page}:${version}`, load)
  const data = query.data
  return <section className="serialization-agenda" aria-label="当天清单">
    <div className="serialization-section-header">
      <h3><time dateTime={date}>{dayjs(date).format('M 月 D 日')}</time></h3>
      <Radio.Group size="small" value={mode} optionType="button" aria-label="当天清单类型"
        options={[{ value: 'planned', label: `计划${data ? ' ' + data.planned.total : ''}` },
          { value: 'first_published', label: `首发${data ? ' ' + data.first_published.total : ''}` }]}
        onChange={(e) => { setMode(e.target.value); setPage(1) }} />
    </div>
    {data && overview?.daily_goal != null && <p className="serialization-muted">当日首发 {data.first_published.total} 章 · 当前日更目标 {overview.daily_goal} 章</p>}
    <QueryFeedback {...query} onReload={query.reload} />
    {data && <>
      {mode === 'planned' ? <ChapterList items={data.planned.items} empty="当天没有安排发布的章节。" onOpen={onChapter} />
        : <PublicationList items={data.first_published.items} empty="当天没有首次发布的章节。" onOpen={onPublication} />}
      <PageNav total={data[mode].total} page={page} onChange={setPage} />
    </>}
  </section>
}

export default function ReleaseCalendar({ date, onDate, overview, ...rest }: Props) {
  const month = date.slice(0, 7)
  const today = beijingMinute().slice(0, 10)
  const counts = new Map(overview?.days.map((value) => [value.date, value]))
  const selectMonth = (month: string) => {
    if (/^[1-9]\d{3}-(0[1-9]|1[0-2])$/.test(month)) onDate(month === today.slice(0, 7) ? today : `${month}-01`)
  }
  return <section className="serialization-panel" aria-label="发布日历">
    <div className="serialization-section-header"><div className="serialization-heading"><h2>发布日历</h2><small>北京时间</small></div>
      <div className="serialization-controls">
        <Button size="small" icon={<LeftOutlined />} aria-label="上个月" onClick={() => selectMonth(dayjs(date).subtract(1, 'month').format('YYYY-MM'))} />
        <Input size="small" type="month" aria-label="选择月份" value={month} onChange={(e) => selectMonth(e.target.value)} />
        <Button size="small" icon={<RightOutlined />} aria-label="下个月" onClick={() => selectMonth(dayjs(date).add(1, 'month').format('YYYY-MM'))} />
        <Button size="small" onClick={() => onDate(today)}>今天</Button>
      </div>
    </div>
    <div className="serialization-calendar-layout">
      <div className="serialization-calendar" aria-label={`${month} 计划与首发`}>
        <div className="serialization-week">{['一', '二', '三', '四', '五', '六', '日'].map((week) => <span key={week}>周{week}</span>)}</div>
        <div className="serialization-calendar-grid">{monthCells(month).map((day, index) => {
          if (!day) return <span key={index} aria-hidden="true" />
          const count = counts.get(day)
          const planned = count?.planned_count ?? 0
          const first = count?.first_published_count ?? 0
          const label = overview ? `，计划 ${planned} 章，首发 ${first} 章` : ''
          return <button type="button" key={day} aria-label={`${day}${label}`} aria-pressed={day === date}
            aria-current={day === today ? 'date' : undefined} onClick={() => onDate(day)}>
            <span className="serialization-day-number">{Number(day.slice(-2))}</span>
            {(planned > 0 || first > 0) && <span className="serialization-counts" aria-hidden>
              {planned > 0 && <span className="serialization-planned">{planned}</span>}
              {first > 0 && <span className="serialization-first">{first}</span>}
            </span>}
          </button>
        })}</div>
        <div className="serialization-legend"><span className="serialization-planned">计划</span><span className="serialization-first">首发</span></div>
      </div>
      <DayAgenda {...rest} key={date} date={date} overview={overview} />
    </div>
  </section>
}
