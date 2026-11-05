import type { PlanningWeave } from '../../api/types'
import { planningRowLabel, planningSourceKey } from '../../utils/planning'

/** 轨道概览只作视觉补充；下方原生表格提供全部名称、状态和键盘操作。 */
export default function PlanningWeaveDiagram({ data }: { data: PlanningWeave }) {
  if (!data.rows.length || !data.tracks.length) return null
  const left = 172, step = 136, top = 70, trackHeight = 52
  const width = left + data.rows.length * step + 32
  const height = top + data.tracks.length * trackHeight + 28
  return <figure className="planning-track-figure">
    <figcaption>当前窗口的支线轨道：圆点为普通推进，菱形为明确交汇。下方表格可查看和操作所有内容。</figcaption>
    <div className="planning-grid-wrap" tabIndex={0} role="region" aria-label="支线轨道概览，可横向滚动">
      <svg className="planning-track-svg" width={width} height={height} aria-hidden="true" focusable="false">
        {data.rows.map((row, index) => <g key={row.key}><text x={left + index * step} y={24} textAnchor="middle">{row.chapter_no === null ? `细纲 ${row.seq ?? '—'}` : `第 ${row.chapter_no} 章`}</text><text x={left + index * step} y={43} textAnchor="middle" className="planning-track-subtitle">{row.title.length > 10 ? `${row.title.slice(0, 10)}…` : row.title}</text></g>)}
        {data.tracks.map((track, index) => {
          const y = top + index * trackHeight
          const progress = data.rows.flatMap((row, position) => track.progress_keys.includes(row.key) ? [left + position * step] : [])
          return <g key={track.subplot.id}>
            <text x={12} y={y + 4}>{track.subplot.name.length > 10 ? `${track.subplot.name.slice(0, 10)}…` : track.subplot.name}</text>
            <line className="planning-track-baseline" x1={left} x2={left + (data.rows.length - 1) * step} y1={y} y2={y} />
            {progress.length > 1 && <line className="planning-track-progress" x1={progress[0]} x2={progress.at(-1)} y1={y} y2={y} />}
            {data.rows.map((row, position) => {
              const node = track.nodes.find((item) => item.unit_id === row.unit_id)
              return node ? <circle key={row.key} className="planning-track-node" cx={left + position * step} cy={y} r={6}><title>{track.subplot.name}，{planningRowLabel(row)}：{node.content}</title></circle> : null
            })}
          </g>
        })}
        {data.crossings.map((event) => {
          const column = data.rows.findIndex((row) => planningSourceKey(row.source) === planningSourceKey(event.source))
          const positions = data.tracks.flatMap((track, index) => event.subplot_ids.includes(track.subplot.id) ? [index] : [])
          if (column < 0 || !positions.length) return null
          const x = left + column * step + 15
          return <g key={event.id}><title>明确交汇：{event.title}，共 {event.subplot_ids.length} 条参与支线</title>
            {positions.length > 1 && <line className="planning-track-crossing" x1={x} x2={x} y1={top + positions[0] * trackHeight} y2={top + positions[positions.length - 1] * trackHeight} />}
            {positions.map((position) => { const y = top + position * trackHeight; return <path key={position} className="planning-track-diamond" d={`M ${x} ${y - 7} L ${x + 7} ${y} L ${x} ${y + 7} L ${x - 7} ${y} Z`} /> })}
          </g>
        })}
      </svg>
    </div>
  </figure>
}
