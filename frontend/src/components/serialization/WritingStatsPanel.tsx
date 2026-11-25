import { Button, Input } from 'antd'
import { useCallback, useState } from 'react'
import { writingStatsApi, type WritingGoal, type WritingStatsDay, type WritingStatsRange } from '../../api/writingStats'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { QueryFeedback } from './SerializationLists'
import WritingGoalModal from './WritingGoalModal'
import { beijingMinute } from './serializationForm'

function TrendRange({ today, selected, onApply }: { today: string; selected: WritingStatsRange; onApply: (range: WritingStatsRange) => void }) {
  const [start, setStart] = useState(selected.start_date)
  const [end, setEnd] = useState(selected.end_date)
  const tooLong = Date.parse(end) - Date.parse(start) > 365 * 86_400_000
  const valid = start && end && start <= end && end <= today && !tooLong
  return <form className="serialization-controls writing-trend-range" onSubmit={(event) => {
    event.preventDefault()
    if (valid) onApply({ start_date: start, end_date: end })
  }}>
    <label>开始日期<Input type="date" required max={end || today} value={start} onChange={(e) => setStart(e.target.value)} /></label>
    <label>结束日期<Input type="date" required min={start} max={today} value={end} onChange={(e) => setEnd(e.target.value)} /></label>
    <Button htmlType="submit" disabled={!valid}>查看趋势</Button>
    {tooLong && <span role="status">最多可查看 366 天</span>}
  </form>
}

function TrendTable({ days }: { days: WritingStatsDay[] }) {
  const peak = Math.max(1, ...days.map((day) => Math.max(day.manual_words ?? 0, day.ai_words ?? 0)))
  const cell = (value: number) => <>
    <span aria-hidden="true" className="writing-trend-bar" style={{ width: `${100 * value / peak}%` }} />
    <span className="writing-trend-value">{value.toLocaleString('zh-CN')}</span>
  </>
  return <div className="writing-trend-scroll" tabIndex={0} role="region" aria-label="每日写作量明细">
    <table className="writing-trend-table" aria-label="每日写作量（字）">
      <thead><tr><th scope="col">北京日期</th><th scope="col">手写（字）</th><th scope="col">AI（字）</th></tr></thead>
      <tbody>{days.slice().reverse().map((day) => <tr key={day.date}>
        <th scope="row">{day.date}{day.partial && <small>部分日</small>}</th>
        {day.manual_words === null || day.ai_words === null
          ? <td colSpan={2} className="serialization-muted">尚未开始统计</td>
          : <><td>{cell(day.manual_words)}</td><td>{cell(day.ai_words)}</td></>}
      </tr>)}</tbody>
    </table>
  </div>
}

export default function WritingStatsPanel({ projectId, version }: { projectId: number; version: number }) {
  const { canReadNow, canEdit, canEditNow } = useProjectAccess(projectId)
  const [range, setRange] = useState<WritingStatsRange>()
  const [editing, setEditing] = useState<WritingGoal>()
  const load = useCallback((signal: AbortSignal) => writingStatsApi.overview(projectId, undefined, {
    signal, isCurrent: canReadNow,
  }), [projectId, canReadNow])
  const query = useMentionQuery(projectId, `writing-stats:${version}`, load)
  const { data } = query
  const loadRange = useCallback((signal: AbortSignal) => writingStatsApi.overview(projectId, range, {
    signal, isCurrent: canReadNow,
  }), [projectId, range, canReadNow])
  const filtered = useMentionQuery(projectId, `writing-trend:${range?.start_date}:${range?.end_date}:${version}`,
    loadRange, Boolean(range && data))
  const trend = range ? filtered : query
  const target = data?.goal.target_words
  const period = data?.goal.period === 'week' ? '本周' : '今日'
  return <section className="serialization-panel writing-stats-panel" aria-label="我的码字目标">
    <div className="serialization-section-header"><h2>我的码字目标</h2>
      {data && canEdit && <Button size="small" onClick={() => { if (canEditNow()) setEditing(data.goal) }}>
        {target ? '修改码字目标' : '设置码字目标'}
      </Button>}
    </div>
    <QueryFeedback {...query} onReload={query.reload} />
    {data && <>
      <dl className="writing-stats-numbers">
        <div><dt>今日手写</dt><dd><strong>{data.today_manual_words.toLocaleString('zh-CN')}</strong> 字</dd></div>
        <div><dt>今日 AI 落稿</dt><dd><strong>{data.today_ai_words.toLocaleString('zh-CN')}</strong> 字</dd></div>
        <div><dt>连续手写</dt><dd><strong>{data.streak_days}</strong> 天</dd></div>
      </dl>
      {target ? <div className="writing-goal-progress">
        <span>{period}手写 {data.period_manual_words.toLocaleString('zh-CN')} / {target.toLocaleString('zh-CN')} 字</span>
        <progress aria-label={`${period}手写目标进度`} max={target} value={Math.min(data.period_manual_words, target)} />
        <span>{data.period_manual_words >= target ? '已达标' : `还差 ${(target - data.period_manual_words).toLocaleString('zh-CN')} 字`}</span>
      </div> : <p className="serialization-muted">尚未设置个人码字目标</p>}
      <div className="serialization-section-header"><h3>每日写作趋势</h3>
        <small className="serialization-muted">自 {beijingMinute(data.tracking_started_at).replace('T', ' ')} 起统计（北京时间）</small>
      </div>
      <TrendRange selected={range ?? data} today={data.today} onApply={(next) => setRange(next.start_date === data.start_date && next.end_date === data.end_date ? undefined : next)} />
      {range && <QueryFeedback {...filtered} onReload={filtered.reload} />}
      {trend.data && <TrendTable days={trend.data.days} />}
    </>}
    {editing && canEdit && <WritingGoalModal projectId={projectId} initial={editing}
      onClose={() => setEditing(undefined)} onSaved={() => { setEditing(undefined); query.reload() }} />}
  </section>
}
