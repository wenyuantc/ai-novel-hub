import { Button, Empty, Space, Tag, Typography } from 'antd'
import type { PlanningRow, PlanningWeave, SubplotCrossing } from '../../api/types'
import PlanningWeaveDiagram from './PlanningWeaveDiagram'
import { planningRowLabel, planningSourceKey } from '../../utils/planning'

interface Props { data: PlanningWeave; canEdit: boolean; onNode: (row: PlanningRow, subplotId: number) => void; onCrossing: (crossing: SubplotCrossing) => void; onCreateCrossing: (row: PlanningRow, subplotIds: number[]) => void }
/** 同章分布与作者明确交汇分别标记；间隔直接采用完整轴的服务端结果。 */
export default function PlanningWeaveTable({ data, canEdit, onNode, onCrossing, onCreateCrossing }: Props) {
  if (!data.tracks.length) return <Empty description="还没有支线，请在支线管理中新建。" />
  const unitName = data.axis === 'chapter' ? '已建章' : data.axis === 'scene' ? '实际场景' : '未建章细纲'
  const visibleSubplots = new Set(data.tracks.map((track) => track.subplot.id))
  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <Typography.Text type="secondary">● 普通推进 ◆ 作者明确交汇 同位置有多条支线只表示并行推进。空档按完整{unitName}顺序计算，不受当前窗口影响。</Typography.Text>
    <PlanningWeaveDiagram data={data} />
    <div className="planning-grid-wrap" tabIndex={0} role="region" aria-label={`${unitName}支线交织分布，可横向滚动`}>
      <table className="planning-grid planning-weave"><caption>支线沿{unitName}排列；点击推进单元编辑，交汇按钮查看完整参与支线。</caption><thead><tr><th scope="col" className="planning-position">支线</th>{data.rows.map((row) => <th key={row.key} scope="col">{planningRowLabel(row)}{(data.same_position[row.key]?.length ?? 0) >= 2 && <div><Tag>{data.same_position[row.key].length} 条同位置推进</Tag>{canEdit && <Button size="small" type="link" onClick={() => onCreateCrossing(row, data.same_position[row.key])}>记录明确交汇</Button>}</div>}</th>)}</tr></thead>
        <tbody>{data.tracks.map((track) => <tr key={track.subplot.id}><th scope="row" className="planning-position"><Tag color={track.subplot.tone}>{track.subplot.name}</Tag>{track.unplanned && <div className="planning-meta">尚未安排</div>}<details className="planning-gap-details"><summary>{track.gaps.filter((gap) => gap.exceeds_threshold).length} 处间隔提醒 · 查看全部 {track.gaps.length} 处间隔</summary>{track.gaps.length ? track.gaps.map((gap, index) => <div key={`${gap.after_key}:${index}`}><div className="planning-meta">{gap.after_label} → {gap.before_label ?? '当前顺序轴末尾'}</div><Tag color={gap.exceeds_threshold ? 'warning' : 'default'}>{gap.is_tail ? '末次推进后' : '两次推进间'} {gap.gap_units} 个{unitName}</Tag></div>) : <span className="planning-meta">暂无间隔</span>}</details></th>
          {data.rows.map((row) => {
            const node = row.unit_id === null ? undefined : track.nodes.find((item) => item.unit_id === row.unit_id)
            const crossings = data.crossings.filter((item) => item.subplot_ids.includes(track.subplot.id) && planningSourceKey(item.source) === planningSourceKey(row.source))
            return <td key={row.key}><Space direction="vertical" size={6}>
              <Button type={node ? 'primary' : 'text'} size="small" ghost={!!node} disabled={!canEdit && !node} onClick={() => onNode(row, track.subplot.id)} aria-label={`${track.subplot.name}，${planningRowLabel(row)}${node ? '查看推进节点' : '添加推进节点'}`}>{node ? '● 推进节点' : canEdit ? '＋ 推进' : '—'}</Button>
              {crossings.map((event) => {
                const hidden = event.subplot_ids.filter((id) => !visibleSubplots.has(id)).length
                return <Button key={event.id} size="small" type="link" onClick={() => onCrossing(event)} aria-label={`交汇：${event.title}，${event.subplot_ids.length} 条参与支线${hidden ? `，另 ${hidden} 条未展示` : ''}`}>◆ {event.title}（{event.subplot_ids.length} 条{hidden ? `，另 ${hidden} 条未展示` : ''}）</Button>
              })}
            </Space></td>
          })}</tr>)}</tbody></table>
    </div>
    {!data.rows.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`还没有${unitName}位置`} />}
  </Space>
}
