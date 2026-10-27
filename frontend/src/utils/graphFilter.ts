// 关系图谱筛选（纯函数，不依赖图谱库，主包与图谱 chunk 都可引用）

import type { GraphEdge, GraphNode, RelationshipGraph } from '../api/types'
import { descendantIds } from './orgTree'
import { chartPalette, type ChartPalette } from '../theme/palette'

export interface GraphFilter {
  /** 只看该组织（含下级组织）的成员；null = 全部 */
  organizationId: number | null
  /** 只看这些类别的关系；空 = 全部 */
  categories: string[]
  /** 隐藏筛选后没有任何关系的角色 */
  hideIsolated: boolean
}

export const DEFAULT_GRAPH_FILTER: GraphFilter = { organizationId: null, categories: [], hideIsolated: false }

export function filterGraph(graph: RelationshipGraph, f: GraphFilter): { nodes: GraphNode[]; edges: GraphEdge[] } {
  let nodes = graph.nodes
  if (f.organizationId !== null) {
    const orgIds = descendantIds(graph.organizations, f.organizationId)
    nodes = nodes.filter((n) => n.organization_ids.some((id) => orgIds.has(id)))
  }
  const visible = new Set(nodes.map((n) => n.id))
  const edges = graph.edges.filter(
    (e) =>
      visible.has(e.source) && visible.has(e.target) && (f.categories.length === 0 || f.categories.includes(e.category)),
  )
  if (f.hideIsolated) {
    const linked = new Set(edges.flatMap((e) => [e.source, e.target]))
    nodes = nodes.filter((n) => linked.has(n.id))
  }
  return { nodes, edges }
}

/** 节点颜色：主角金色、反派红色、重要配角蓝色、配角青色，其余灰；颜色取自主题调色板（默认浅色），非存活降低饱和度由组件处理 */
export function nodeColor(
  n: Pick<GraphNode, 'role_type' | 'is_protagonist'>,
  palette: Pick<ChartPalette, 'roles'> = chartPalette('light'),
): string {
  const r = palette.roles
  if (n.is_protagonist) return r.protagonist
  switch (n.role_type.trim()) {
    case '反派':
      return r.villain
    case '重要配角':
      return r.important
    case '配角':
      return r.supporting
    default:
      return r.other
  }
}
