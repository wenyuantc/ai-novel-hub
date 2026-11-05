import { Button, Checkbox, Empty, Space, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { MatrixCell, MatrixColumn, PlanningMatrix, PlanningRow } from '../../api/types'
import { planningRowLabel, planningRowLink, settingCellDescription } from '../../utils/planning'

interface Props {
  projectId: number; data: PlanningMatrix; selected: string[]; canEdit: boolean; onSelection: (keys: string[]) => void
  onCell: (row: PlanningRow, column: MatrixColumn, cell: MatrixCell) => void
  onEdit: (row: PlanningRow) => void; onNode: (row: PlanningRow) => void; onCrossing: (row: PlanningRow) => void; onPreview: (row: PlanningRow) => void
}
/** 表格按窗口呈现两维数据，格子按钮保留完整可访问名称。 */
export default function PlanningMatrixTable({ projectId, data, selected, canEdit, onSelection, onCell, onEdit, onNode, onCrossing, onPreview }: Props) {
  const cells = new Map(data.cells.map((cell) => [`${cell.row_key}|${cell.column_key}`, cell]))
  const all = data.rows.length > 0 && data.rows.every((row) => selected.includes(row.key))
  if (!data.rows.length) return <Empty description="没有符合条件的章节或细纲；可先创建大纲细纲，再回来规划。" />
  return <div className="planning-grid-wrap" tabIndex={0} role="region" aria-label="章节设定与支线规划矩阵，可横向滚动">
    <table className="planning-grid"><caption>选择章节／细纲，可批量调整 POV、标签和手动关联；点击格子查看来源或维护推进。</caption><thead><tr>
      <th scope="col" className="planning-position"><Checkbox aria-label="选择当前页全部规划位置" checked={all} indeterminate={!all && selected.length > 0} disabled={!canEdit} onChange={(event) => onSelection(event.target.checked ? data.rows.map((row) => row.key) : [])} /> 章节／细纲</th>
      <th scope="col">POV 与标签</th>
      {data.columns.map((column) => <th key={column.key} scope="col"><Tag color={column.tone}>{column.selection.kind === 'subplot' ? '支线' : '设定'}</Tag><div>{column.name}</div></th>)}
      <th scope="col">规划操作</th>
    </tr></thead><tbody>{data.rows.map((row) => {
      const link = planningRowLink(projectId, row)
      return <tr key={row.key}><th scope="row" className="planning-position"><div className="planning-row-heading"><Checkbox aria-label={`选择${planningRowLabel(row)}`} checked={selected.includes(row.key)} disabled={!canEdit} onChange={(event) => onSelection(event.target.checked ? [...selected, row.key] : selected.filter((key) => key !== row.key))} />
        <div>{link ? <Link to={link}>{planningRowLabel(row)}</Link> : planningRowLabel(row)}<div className="planning-meta">{row.volume_title || (row.volume_no !== null ? `第 ${row.volume_no} 卷` : '未分卷')} · {row.chapter_id === null ? '未建章' : row.has_content ? '已有正文' : '未写正文'}</div></div></div></th>
        <td><Space direction="vertical" size={4}><Typography.Text>{row.pov_character_id ? `POV · ${row.pov_character_name ?? `角色 #${row.pov_character_id}`}` : '未指定视角'}</Typography.Text><Space size={[0, 4]} wrap>{row.tags.map((tag) => <Tag color={tag.tone} key={tag.id}>{tag.name}</Tag>)}</Space></Space></td>
        {data.columns.map((column) => {
          const cell = cells.get(`${row.key}|${column.key}`)
          if (!cell) return <td key={column.key}>—</td>
          const description = cell.kind === 'setting' ? settingCellDescription(cell) : [cell.node ? '推进节点' : '', cell.crossings.length ? `明确交汇 ${cell.crossings.length}` : ''].filter(Boolean).join(' · ') || '未安排'
          return <td key={column.key}><button type="button" className={`planning-cell ${cell.kind === 'subplot' && cell.has_progress ? 'planning-cell-progress' : ''}`} onClick={() => onCell(row, column, cell)} aria-label={`${planningRowLabel(row)}，${column.name}：${description}`}>
            {cell.kind === 'setting' ? <><span className="planning-cell-markers">{cell.manual && <Tag color="blue">手动</Tag>}{cell.pov && <Tag color="gold">POV</Tag>}{cell.mention_count > 0 && <Tag>{cell.mention_count} 次</Tag>}{cell.ambiguous_count > 0 && <Tag color="warning">? {cell.ambiguous_count}</Tag>}</span><span className="planning-meta">{!cell.tracking_supported ? '无自动追踪' : !cell.has_content ? '未写' : !cell.mention_count && !cell.ambiguous_count ? '无正文提及' : '正文提及'}</span></> : <><span>{cell.node ? '● 推进' : cell.crossings.length ? '◆ 交汇' : '＋ 安排'}</span>{cell.crossings.length > 0 && <span className="planning-meta">{cell.crossings.length} 个明确交汇</span>}</>}
          </button></td>
        })}
        <td><Space direction="vertical" size={0}><Button size="small" type="link" disabled={!canEdit} onClick={() => onEdit(row)}>编辑规划</Button><Button size="small" type="link" disabled={!canEdit} onClick={() => onNode(row)}>支线推进</Button><Button size="small" type="link" disabled={!canEdit} onClick={() => onCrossing(row)}>新建交汇</Button><Button size="small" type="link" onClick={() => onPreview(row)}>AI 规划内容</Button></Space></td>
      </tr>
    })}</tbody></table>
  </div>
}
