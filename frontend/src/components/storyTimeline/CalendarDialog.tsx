import { Alert, Button, Modal, Space } from 'antd'
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { storyTimelineApi, type CalendarImpact, type StoryCalendar, type StoryMonth, type TimelineItem } from '../../api/storyTimeline'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useSerializationAction } from '../serialization/useSerializationAction'
import CalendarGenerateDialog from './CalendarGenerateDialog'
import DateDialog from './DateDialog'
import { dateText, itemTitle } from './timelineModel'
import './storyTimeline.css'

type DraftMonth = { id: string; name: string; days: string }
function newMonth(): DraftMonth {
  const bytes = crypto.getRandomValues(new Uint32Array(3))
  return { id: 'm' + Array.from(bytes, (n) => n.toString(36)).join(''), name: '', days: '30' }
}

export default function CalendarDialog({ pid, initial, onClose, onChanged }: {
  pid: number; initial: StoryCalendar; onClose: () => void; onChanged: () => void
}) {
  const { canEditNow, canUseAI } = useProjectAccess(pid)
  const write = useSerializationAction(canEditNow)
  const [base, setBase] = useState(initial)
  const [era, setEra] = useState(initial.era_name ?? '')
  const [months, setMonths] = useState<DraftMonth[]>(() => initial.months.length
    ? initial.months.map((m) => ({ ...m, days: String(m.days) })) : [newMonth()])
  const [impact, setImpact] = useState<CalendarImpact>()
  const [changed, setChanged] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [editing, setEditing] = useState<TimelineItem>()

  function updateBase(calendar: StoryCalendar) {
    setBase(calendar)
    setChanged(true)
    setImpact(undefined)
  }
  function changeMonths(next: DraftMonth[]) {
    setMonths(next); setImpact(undefined)
  }
  function shift(index: number, step: number) {
    const next = [...months]
    const destination = index + step
    next[index] = months[destination]
    next[destination] = months[index]
    changeMonths(next)
  }
  function submit() {
    if (impact?.blocked.length || !months.length) return
    const monthRows: StoryMonth[] = months.map((m) => ({ ...m, name: m.name.trim(), days: Number(m.days) }))
    const payload = { expected_revision: base.revision, era_name: era.trim(), months: monthRows }
    void write.run(async (opts) => {
      try {
        if (!impact) return { impact: await storyTimelineApi.preview(pid, payload, opts) }
        await storyTimelineApi.saveCalendar(pid, { ...payload, confirm_reorder: impact.reordered.length > 0 }, opts)
        return { saved: true }
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 409) throw err
        return { fresh: await storyTimelineApi.calendar(pid, opts) }
      }
    }, (result) => {
      if (result.fresh) updateBase(result.fresh)
      else if (result.impact) setImpact(result.impact)
      else { onChanged(); onClose() }
    })
  }
  const affected = impact?.blocked.length ? impact.blocked : impact?.reordered ?? []
  return <Modal open title="作品历法" footer={null} onCancel={onClose}>
    <form className="story-date-form" aria-label="作品历法" onSubmit={(e) => { e.preventDefault(); submit() }}>
      {changed && <Alert type="warning" showIcon message="时间线已更新，请核对当前历法和保留的输入，然后重新预览。"
        description={`${base.era_name ?? '尚未设置'} · ${base.months.map((m) => `${m.name}（${m.days} 天）`).join('、')}`} />}
      <fieldset disabled={write.busy}><legend>纪年与月份</legend>
        <label>纪年名称<input name="era" maxLength={64} required value={era} onChange={(e) => { setEra(e.target.value); setImpact(undefined) }} /></label>
        <ol className="story-months">{months.map((month, index) => <li key={month.id}>
          <div className="story-form-fields">
            <label>第 {index + 1} 月名称<input name={`name-${month.id}`} maxLength={64} required value={month.name} onChange={(e) =>
              changeMonths(months.map((m) => m.id === month.id ? { ...m, name: e.target.value } : m))} /></label>
            <label>天数<input name={`days-${month.id}`} type="number" min="1" max="999999" step="1" required value={month.days} onChange={(e) =>
              changeMonths(months.map((m) => m.id === month.id ? { ...m, days: e.target.value } : m))} /></label>
          </div>
          <Space wrap>
            <Button disabled={index === 0 || write.busy} onClick={() => shift(index, -1)}>上移</Button>
            <Button disabled={index === months.length - 1 || write.busy} onClick={() => shift(index, 1)}>下移</Button>
            <Button disabled={months.length === 1 || write.busy} onClick={() => changeMonths(months.filter((m) => m.id !== month.id))}>移除</Button>
          </Space>
        </li>)}</ol>
        <Space wrap>
          <Button disabled={write.busy || months.length >= 128} onClick={() => changeMonths([...months, newMonth()])}>添加月份</Button>
          {canUseAI && <Button disabled={write.busy} onClick={() => setGenerating(true)}>AI 生成历法</Button>}
        </Space>
      </fieldset>
      {impact && <Alert showIcon type={affected.length ? 'warning' : 'info'}
        message={impact.blocked.length ? '以下标注在新历法中无效，请先修正日期。' :
          impact.reordered.length ? '以下日期的先后将改变，请核对后确认保存。' : '可以保存，已有日期均有效。'}
        description={affected.length > 0 && <ul>{affected.map((item) => <li key={`${item.kind}:${item.scene_id ?? item.chapter_id}`}>
          {itemTitle(item)} · {dateText(item)}
          {impact.blocked.length > 0 && <Button type="link" onClick={() => setEditing(item)}>修正日期</Button>}
        </li>)}</ul>} />}
      {write.error && <Alert type="error" showIcon message={write.error} />}
      <Button htmlType="submit" type="primary" disabled={Boolean(impact?.blocked.length)} loading={write.busy}>
        {impact ? '确认保存历法' : '预览历法修改'}</Button>
    </form>
    {generating && canUseAI && <CalendarGenerateDialog pid={pid} revision={base.revision} onClose={() => setGenerating(false)}
      onBaseChanged={updateBase} onAdopt={(draft) => {
        setEra(draft.era_name)
        changeMonths(draft.months.map((m) => ({ ...m, days: String(m.days) })))
        setGenerating(false)
      }} />}
    {editing && <DateDialog pid={pid} kind={editing.kind} id={editing.scene_id ?? editing.chapter_id}
      onClose={() => setEditing(undefined)} onSaved={() => { setEditing(undefined); setImpact(undefined); onChanged() }} />}
  </Modal>
}
