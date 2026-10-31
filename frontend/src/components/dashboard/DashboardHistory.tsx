import { Alert, Collapse, Empty, Select, Space, Table, Tag, Typography, theme } from 'antd'
import type { TableColumnsType } from 'antd'
import { useId, useState } from 'react'
import type { AdminDashboardHistory, AdminDashboardHistoryPoint, AdminDashboardTaskDay } from '../../api/types'
import { useElementWidth } from '../../hooks/useElementWidth'
import { usePalette } from '../../theme/usePalette'
import { DASHBOARD_METRICS, dashboardAxis, dashboardChartFrame, dashboardCount, dashboardDelta, dashboardLineSegments, dashboardTaskMax, dashboardTime } from '../../utils/adminDashboard'
import type { DashboardMetric } from '../../utils/adminDashboard'

const INITIAL_WIDTH = 320
const HEIGHT = 210

function Axes({ max, frame, first, last }: { max: number; frame: ReturnType<typeof dashboardChartFrame>; first?: string; last?: string }) {
  const palette = usePalette()
  const { left, right, top, bottom, height } = frame
  return <g fontSize={12} fill={palette.textSecondary}>
    {[0, 0.5, 1].map((ratio) => {
      const y = bottom - ratio * (bottom - top)
      return <g key={ratio}>
        <line x1={left} x2={right} y1={y} y2={y} stroke={palette.grid} />
        <text x={left - 10} y={y} textAnchor="end" dominantBaseline="middle">{dashboardAxis(max * ratio)}</text>
      </g>
    })}
    <text x={left} y={height - 8}>{first?.slice(5)}</text>
    <text x={right} y={height - 8} textAnchor="end">{last?.slice(5)}</text>
  </g>
}

/** 一项一个量纲；未观测日期切断折线，单点和有效零不被隐藏。 */
function ScaleChart({ points, metric }: { points: AdminDashboardHistoryPoint[]; metric: DashboardMetric }) {
  const id = useId()
  const palette = usePalette()
  const [ref, measuredWidth] = useElementWidth<HTMLElement>()
  const frame = dashboardLineSegments(points, metric, measuredWidth || INITIAL_WIDTH, HEIGHT)
  const { segments, max } = frame
  return <figure ref={ref} className="admin-dashboard-chart">
    {!segments.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该时段没有规模采样记录" /> : <>
    <svg viewBox={`0 0 ${frame.width} ${frame.height}`} height={HEIGHT} role="img" aria-labelledby={`${id}-title ${id}-desc`}>
      <title id={`${id}-title`}>{DASHBOARD_METRICS[metric]}总量趋势</title>
      <desc id={`${id}-desc`}>北京时间各日最后一次成功采样。缺失日期断线；具体值可在下方每日数据表中读取。</desc>
      <Axes max={max} frame={frame} first={points.at(0)?.day} last={points.at(-1)?.day} />
      {segments.map((segment) => <g key={segment[0].day}>
        {segment.length > 1 && <polyline fill="none" stroke={palette.primary} strokeWidth={2}
          points={segment.map((point) => `${point.x},${point.y}`).join(' ')} />}
        {segment.map((point) => <circle key={point.day} cx={point.x} cy={point.y} r={3}
          fill={point.partial ? palette.surface : palette.primary} stroke={palette.primary} strokeWidth={1.5}>
          <title>{point.day}：{dashboardCount(point.value)}{point.partial ? '（部分统计日）' : ''}</title>
        </circle>)}
      </g>)}
    </svg>
    <figcaption>空心点表示部分统计日；缺口不补零、不连线。</figcaption>
    </>}
  </figure>
}

const RESULT_KEYS: Array<Exclude<keyof AdminDashboardTaskDay, 'created'>> = ['succeeded', 'failed', 'cancelled', 'interrupted']
const RESULT_LABELS = { succeeded: '成功', failed: '失败', cancelled: '取消', interrupted: '中断' }

/** 新建与四种结束结果并列，避免把跨日任务误当作同日转化率。 */
function TaskChart({ points }: { points: AdminDashboardHistoryPoint[] }) {
  const id = useId()
  const palette = usePalette()
  const [ref, measuredWidth] = useElementWidth<HTMLElement>()
  const frame = dashboardChartFrame(measuredWidth || INITIAL_WIDTH, HEIGHT)
  const max = dashboardTaskMax(points)
  const plotHeight = frame.bottom - frame.top
  const step = (frame.right - frame.left) / Math.max(points.length, 1)
  const barWidth = Math.min(10, step * 0.32)
  const gap = Math.min(1, step * 0.1)
  const colors = { succeeded: palette.success, failed: palette.danger, cancelled: palette.muted, interrupted: palette.warning }
  return <figure ref={ref} className="admin-dashboard-chart">
    {!points.some((point) => point.tasks !== null) ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该时段没有任务统计记录" /> : <>
    <Space wrap size={[12, 4]} className="admin-dashboard-legend">
      <span><i style={{ background: palette.primary }} />新建</span>
      {RESULT_KEYS.map((key) => <span key={key}><i style={{ background: colors[key] }} />{RESULT_LABELS[key]}</span>)}
    </Space>
    <svg viewBox={`0 0 ${frame.width} ${frame.height}`} height={HEIGHT} role="img" aria-labelledby={`${id}-title ${id}-desc`}>
      <title id={`${id}-title`}>每日任务新建与结束结果</title>
      <desc id={`${id}-desc`}>每组左柱为新建，右侧堆叠柱为成功、失败、取消、中断。无记录的日期没有柱，数据表包含精确数量。</desc>
      <Axes max={max} frame={frame} first={points.at(0)?.day} last={points.at(-1)?.day} />
      {points.map((point, index) => {
        const tasks = point.tasks
        if (!tasks) return null
        const x = frame.left + step * (index + 0.5)
        let stacked = 0
        const createdHeight = tasks.created / max * plotHeight
        return <g key={point.day}>
          <rect x={x - barWidth - gap / 2} y={frame.bottom - createdHeight} width={barWidth} height={createdHeight} fill={palette.primary}>
            <title>{point.day} 新建：{dashboardCount(tasks.created)}</title>
          </rect>
          {RESULT_KEYS.map((key) => {
            const height = tasks[key] / max * plotHeight
            stacked += height
            return <rect key={key} x={x + gap / 2} y={frame.bottom - stacked} width={barWidth} height={height} fill={colors[key]}>
              <title>{point.day} {RESULT_LABELS[key]}：{dashboardCount(tasks[key])}</title>
            </rect>
          })}
        </g>
      })}
    </svg>
    <figcaption>创建和结束可能跨日；图中数量仅表示已记录事件，不要求同日相等。</figcaption>
    </>}
  </figure>
}

const historyColumns: TableColumnsType<AdminDashboardHistoryPoint> = [
  { title: '统计日', dataIndex: 'day', width: 126, render: (day: string, point) => <Space direction="vertical" size={2}>
    <span>{day}</span>{point.partial && <Tag>部分统计日</Tag>}
  </Space> },
  { title: '最后采样（北京时间）', width: 220, render: (_: unknown, point) => dashboardTime(point.captured_at) },
  { title: '用户', width: 100, render: (_: unknown, point) => dashboardCount(point.totals?.users) },
  { title: '作品', width: 100, render: (_: unknown, point) => dashboardCount(point.totals?.projects) },
  { title: '章节', width: 100, render: (_: unknown, point) => dashboardCount(point.totals?.chapters) },
  { title: '正文总字数', width: 130, render: (_: unknown, point) => dashboardCount(point.totals?.total_words) },
  { title: '工坊待审核', width: 110, render: (_: unknown, point) => dashboardCount(point.totals?.pending_submissions) },
  { title: '新建任务', width: 110, render: (_: unknown, point) => dashboardCount(point.tasks?.created) },
  ...RESULT_KEYS.map((key) => ({
    title: RESULT_LABELS[key], width: 100, key,
    render: (_: unknown, point: AdminDashboardHistoryPoint) => dashboardCount(point.tasks?.[key]),
  })),
]

/** 规模、每日任务和可访问的历史数据表共同组成统计分析，不在客户端推测缺失值。 */
export default function DashboardHistory({ history }: { history: AdminDashboardHistory }) {
  const [metric, setMetric] = useState<DashboardMetric>('users')
  const { token } = theme.useToken()
  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Alert type="info" showIcon message="统计日按北京时间划分；规模取当日最后一次成功采样。"
      description="历史从功能上线后积累；无记录不等于零。首日、当日或覆盖不完整的日期会标记为部分统计日。" />
    <div className="admin-dashboard-chart-heading">
      <Typography.Title level={5} style={{ margin: 0 }}>平台规模趋势</Typography.Title>
      <Select<DashboardMetric> aria-label="规模趋势指标" value={metric} onChange={setMetric}
        options={Object.entries(DASHBOARD_METRICS).map(([value, label]) => ({ value, label }))} style={{ minWidth: 142 }} />
    </div>
    <ScaleChart points={history.points} metric={metric} />
    <div className="admin-dashboard-comparison" style={{ background: token.colorFillQuaternary }}>
      <Typography.Text>{DASHBOARD_METRICS[metric]}净变化：<strong>{dashboardDelta(history.comparison[metric])}</strong></Typography.Text>
      <Typography.Text type="secondary">比较 {history.comparison.from_day} 与 {history.comparison.to_day} 的有效采样；缺少首尾基准时不可计算。</Typography.Text>
    </div>
    <Typography.Title level={5} style={{ margin: '4px 0 0' }}>每日任务</Typography.Title>
    <TaskChart points={history.points} />
    <Collapse items={[{ key: 'daily', label: '查看每日数据表（含精确数量、缺失与采样时间）', children:
      <Table<AdminDashboardHistoryPoint> aria-label="每日统计数据表" size="small" columns={historyColumns} rowKey="day"
        dataSource={history.points} scroll={{ x: 1396 }} pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 30, 90], responsive: true }} />,
    }]} />
    <Typography.Text type="secondary">开始跟踪：{dashboardTime(history.tracking_since)}。作品、章节和字数包含回收站；净变化不是新建量或写作产出。</Typography.Text>
  </Space>
}
