import { Button, Empty, Space, Table, Tag, theme, Typography } from 'antd'
import { formatScore, SCORE_KEYS, SCORE_LABELS, trendSegments } from '../../api/analysis'
import type { AnalysisBrief, ScoreKey } from '../../api/types'
import { usePalette } from '../../theme/usePalette'
import { formatTime } from '../../utils/time'

const WIDTH = 480
const HEIGHT = 140

function scoreValue(a: AnalysisBrief, key: ScoreKey): number | null {
  const v = a.scores?.[key]
  return typeof v === 'number' ? v : null
}

/** 评分走势（纯 SVG 折线，不引入图表库）：横轴为分析次序，纵轴 1–10。 */
function ScoreTrend({ items }: { items: AnalysisBrief[] }) {
  // items 新在前 → 按时间正序绘制
  const ordered = [...items].reverse()
  const COLORS: Record<ScoreKey, string> = usePalette().series
  const { token } = theme.useToken()
  return (
    <div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        width="100%"
        height={HEIGHT}
        role="img"
        aria-label="评分走势"
        style={{ background: token.colorFillQuaternary, borderRadius: 6 }}
      >
        {[2, 4, 6, 8, 10].map((v) => {
          const y = 6 + ((HEIGHT - 12) * (10 - v)) / 9
          return (
            <g key={v}>
              <line x1={0} x2={WIDTH} y1={y} y2={y} stroke={token.colorBorderSecondary} />
              <text x={2} y={y - 2} fontSize={9} fill={token.colorTextQuaternary}>
                {v}
              </text>
            </g>
          )
        })}
        {SCORE_KEYS.map((key) =>
          trendSegments(
            ordered.map((a) => scoreValue(a, key)),
            WIDTH,
            HEIGHT,
          ).map((points, i) =>
            points.includes(' ') ? (
              <polyline
                key={`${key}-${i}`}
                points={points}
                fill="none"
                stroke={COLORS[key]}
                strokeWidth={key === 'overall' ? 2.5 : 1.2}
                opacity={key === 'overall' ? 1 : 0.7}
              />
            ) : (
              <circle
                key={`${key}-${i}`}
                cx={Number(points.split(',')[0])}
                cy={Number(points.split(',')[1])}
                r={key === 'overall' ? 3 : 2}
                fill={COLORS[key]}
              />
            ),
          ),
        )}
      </svg>
      <Space size="middle" wrap style={{ fontSize: 12 }}>
        {SCORE_KEYS.map((key) => (
          <span key={key}>
            <span
              style={{ display: 'inline-block', width: 10, height: 3, background: COLORS[key], marginRight: 4, verticalAlign: 'middle' }}
            />
            {SCORE_LABELS[key]}
          </span>
        ))}
      </Space>
    </div>
  )
}

interface Props {
  items: AnalysisBrief[]
  loading: boolean
  selectedId: number | null
  onSelect: (id: number) => void
}

/** 分析历史：评分走势 + 各次分析列表（可切换查看任意一次）。 */
export default function AnalysisHistory({ items, loading, selectedId, onSelect }: Props) {
  if (!loading && items.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无分析记录" />
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {items.length > 1 && <ScoreTrend items={items} />}
      <Table<AnalysisBrief>
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={items}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        columns={[
          {
            title: '时间',
            dataIndex: 'created_at',
            render: (t: string, a) => (
              <Space size={4} wrap>
                <Typography.Text style={{ fontSize: 12 }}>{formatTime(t)}</Typography.Text>
                {a.id === items[0]?.id && <Tag color="blue">最新</Tag>}
                {a.stale && <Tag>旧正文</Tag>}
              </Space>
            ),
          },
          ...SCORE_KEYS.map((key) => ({
            title: SCORE_LABELS[key],
            key,
            width: 58,
            render: (_: unknown, a: AnalysisBrief) => formatScore(a.scores, key),
          })),
          {
            title: '',
            key: 'view',
            width: 56,
            render: (_: unknown, a: AnalysisBrief) =>
              a.id === selectedId ? (
                <Typography.Text type="secondary">查看中</Typography.Text>
              ) : (
                <Button type="link" size="small" onClick={() => onSelect(a.id)}>
                  查看
                </Button>
              ),
          },
        ]}
      />
    </Space>
  )
}
