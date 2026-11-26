import { Alert, Button, Modal, Space } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { loadStoryDate, storyTimelineApi, type StoryCalendar, type StoryDate, type TargetKind, type TimelineItem } from '../../api/storyTimeline'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { QueryFeedback } from '../serialization/SerializationLists'
import { useSerializationAction } from '../serialization/useSerializationAction'
import { dateText, itemTitle } from './timelineModel'
import './storyTimeline.css'

interface Props {
  pid: number; kind: TargetKind; id: number; onClose: () => void; onSaved: () => void
}
interface Snapshot { calendar: StoryCalendar; item: TimelineItem }

function DateForm({ pid, kind, id, initial, onSaved }: Props & { initial: Snapshot }) {
  const { canEdit, canEditNow } = useProjectAccess(pid)
  const write = useSerializationAction(canEditNow)
  const [base, setBase] = useState(initial)
  const [conflict, setConflict] = useState(false)
  const first = initial.item.effective_date
  const [year, setYear] = useState(String(first?.year ?? 1))
  const [month, setMonth] = useState(first?.month_id ?? initial.calendar.months[0]?.id ?? '')
  const [day, setDay] = useState(String(first?.day ?? 1))
  const selected = base.calendar.months.find((row) => row.id === month)
  const save = (date: StoryDate | null) => void write.run(async (opts) => {
    try {
      await storyTimelineApi.saveDate(pid, kind, id, base.item.revision, date, opts)
      return null
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 409) throw err
      return loadStoryDate(pid, kind, id, opts)
    }
  }, (fresh) => {
    if (!fresh) onSaved()
    else { setBase(fresh); setConflict(true) }
  })
  return <div className="story-date-form">
    <p>{itemTitle(base.item)}</p>
    <p>当前日期：{dateText(base.item)}</p>
    {conflict && <Alert type="warning" showIcon message="时间线已更新。请核对当前日期与历法，再保存保留的输入。" />}
    {write.error && <Alert type="error" showIcon message={write.error} />}
    {canEdit && (base.calendar.months.length ? <form aria-label="登记故事日期" onSubmit={(event) => {
      event.preventDefault()
      if (selected) save({ year: Number(year), month_id: month, day: Number(day) })
    }}>
      <fieldset disabled={write.busy}><legend>{base.calendar.era_name}</legend>
        <div className="story-form-fields">
          <label>年份<input name="year" type="number" min="1" max="999999" step="1" required value={year} onChange={(e) => setYear(e.target.value)} /></label>
          <label>月份<select name="month" required value={selected?.id ?? ''} onChange={(e) => setMonth(e.target.value)}>
            {!selected && <option value="">请选择月份</option>}
            {base.calendar.months.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select></label>
          <label>日<input name="day" type="number" min="1" max={selected?.days} step="1" required value={day} onChange={(e) => setDay(e.target.value)} /></label>
        </div>
        <Space wrap>
          <Button type="primary" htmlType="submit" loading={write.busy}>{kind === 'chapter' ? '保存章节默认日期' : '保存场景日期'}</Button>
          {base.item.explicit_date && <Button disabled={write.busy} onClick={() => save(null)}>
            {kind === 'chapter' ? '清除默认日期' : '恢复沿用本章'}</Button>}
        </Space>
      </fieldset>
    </form> : <p>请先到<Link to={`/projects/${pid}/story-timeline`}>故事时间线</Link>设置作品历法。</p>)}
  </div>
}

export default function DateDialog(props: Props) {
  const { pid, kind, id, onClose } = props
  const { canRead, canReadNow } = useProjectAccess(pid)
  const load = useCallback((signal: AbortSignal) => loadStoryDate(pid, kind, id, { signal, isCurrent: canReadNow }),
    [pid, kind, id, canReadNow])
  const query = useMentionQuery(pid, `date:${kind}:${id}`, load, canRead)
  return <Modal open title={kind === 'chapter' ? '章节默认日期' : '场景故事日期'} footer={null} onCancel={onClose}>
    <QueryFeedback {...query} onReload={query.reload} />
    {query.data && <DateForm {...props} initial={query.data} />}
  </Modal>
}
