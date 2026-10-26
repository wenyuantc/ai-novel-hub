import { Empty, Space, Typography } from 'antd'
import { useMemo } from 'react'
import { chapterX, timelineBars, timelineRange, timelineTicks, type SegmentKind } from '../../api/foreshadows'
import type { Foreshadow } from '../../api/types'

interface Props {
  items: Foreshadow[]
  /** 项目当前章（已有正文的最大章节号），长线 / 超期线段延伸到这里 */
  currentChapter: number
  onSelect?: (f: Foreshadow) => void
}

const LABEL_W = 180
const ROW_H = 28
const AXIS_H = 28
const PAD_R = 24
const RED = '#cf1322'
const BLUE = '#1677ff'
const GREY = '#bfbfbf'

const STROKE: Record<SegmentKind, { color?: string; dash?: string }> = {
  solid: {},
  dashed: { dash: '5 4' },
  overdue: { color: RED },
}

/** 伏笔时间线（纯 SVG）：横轴章节；实际章节实线 / 实心点，计划章节虚线 / 空心点；超期红色；长线箭头延伸到当前章。 */
export default function ForeshadowTimeline({ items, currentChapter, onSelect }: Props) {
  const bars = useMemo(() => timelineBars(items, currentChapter), [items, currentChapter])
  const range = useMemo(() => timelineRange(bars, currentChapter), [bars, currentChapter])
  const ticks = useMemo(() => timelineTicks(range), [range])
  const byId = useMemo(() => new Map(items.map((f) => [f.id, f])), [items])

  if (items.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的伏笔" />

  const span = range.max - range.min
  const unit = Math.max(16, Math.min(48, 900 / Math.max(span, 1)))
  const width = LABEL_W + span * unit + PAD_R
  const height = AXIS_H + bars.length * ROW_H + 8
  const x = (ch: number) => chapterX(ch, range, unit, LABEL_W)

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        实线 / 实心点 = 实际章节，虚线 / 空心点 = 计划章节；红色 = 超期未回收；箭头 = 长线伏笔（没有计划回收章）。
        {currentChapter > 0 ? `竖线为当前第 ${currentChapter} 章。` : ''}点击标题可编辑。
      </Typography.Text>
      <div style={{ overflowX: 'auto', border: '1px solid #f0f0f0', borderRadius: 6 }}>
        <svg width={width} height={height} role="img" aria-label="伏笔时间线" style={{ display: 'block', fontSize: 12 }}>
          <defs>
            <marker id="fs-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0,0 L10,5 L0,10 z" fill={BLUE} />
            </marker>
          </defs>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={x(t)} x2={x(t)} y1={AXIS_H - 6} y2={height} stroke="#f5f5f5" />
              <text x={x(t)} y={AXIS_H - 10} textAnchor="middle" fill="#8c8c8c">
                {t}
              </text>
            </g>
          ))}
          {currentChapter >= range.min && currentChapter <= range.max && (
            <line x1={x(currentChapter)} x2={x(currentChapter)} y1={AXIS_H - 6} y2={height} stroke="#faad14" strokeDasharray="2 3" />
          )}
          {bars.map((b, i) => {
            const y = AXIS_H + i * ROW_H + ROW_H / 2
            const f = byId.get(b.id)
            const muted = b.status === 'abandoned'
            const base = muted ? GREY : b.overdue > 0 ? RED : b.status === 'resolved' ? '#389e0d' : BLUE
            const title = b.title.length > 12 ? `${b.title.slice(0, 12)}…` : b.title
            return (
              <g key={b.id}>
                <text
                  x={8}
                  y={y + 4}
                  fill={b.overdue > 0 ? RED : muted ? GREY : '#262626'}
                  style={{ cursor: onSelect ? 'pointer' : undefined }}
                  onClick={() => f && onSelect?.(f)}
                >
                  <title>{`#${b.id} ${b.title}${b.overdue > 0 ? `（超期 ${b.overdue} 章）` : ''}`}</title>
                  {`#${b.id} ${title}`}
                </text>
                {b.segments.map((s, j) => {
                  const st = STROKE[s.kind]
                  const last = j === b.segments.length - 1
                  return (
                    <line
                      key={j}
                      x1={x(s.from)}
                      x2={x(s.to)}
                      y1={y}
                      y2={y}
                      stroke={st.color ?? base}
                      strokeWidth={3}
                      strokeDasharray={st.dash}
                      markerEnd={b.arrow && last ? 'url(#fs-arrow)' : undefined}
                    />
                  )
                })}
                {b.markers.map((m, j) => (
                  <circle
                    key={j}
                    cx={x(m.chapter)}
                    cy={y}
                    r={m.type === 'plant' ? 5 : 6}
                    fill={m.actual ? base : '#fff'}
                    stroke={base}
                    strokeWidth={2}
                  >
                    <title>{`${m.type === 'plant' ? '埋设' : '回收'}：${m.actual ? '' : '计划'}第 ${m.chapter} 章`}</title>
                  </circle>
                ))}
              </g>
            )
          })}
        </svg>
      </div>
    </Space>
  )
}
