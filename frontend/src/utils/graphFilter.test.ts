import { describe, expect, it } from 'vitest'
import type { RelationshipGraph } from '../api/types'
import { DEFAULT_GRAPH_FILTER, filterGraph, nodeColor } from './graphFilter'

const node = (id: number, orgs: number[] = []) => ({
  id,
  name: `c${id}`,
  aliases: [],
  role_type: '',
  is_protagonist: false,
  life_status: 'alive',
  organization_ids: orgs,
})
const edge = (id: number, source: number, target: number, category: string) => ({
  id,
  source,
  target,
  type_id: 1,
  category,
  role_a: '朋友',
  role_b: '朋友',
  label: '朋友',
  intimacy: 0,
  status: 'normal',
  description: '',
})
const graph: RelationshipGraph = {
  nodes: [node(1, [10]), node(2, [11]), node(3), node(4)],
  edges: [edge(1, 1, 2, 'social'), edge(2, 2, 3, 'hostile'), edge(3, 1, 3, 'family')],
  organizations: [
    { id: 10, name: '宗门', parent_id: null, org_type: '' },
    { id: 11, name: '外门', parent_id: 10, org_type: '' },
  ],
}

describe('图谱筛选', () => {
  it('默认全部', () => {
    const v = filterGraph(graph, DEFAULT_GRAPH_FILTER)
    expect(v.nodes).toHaveLength(4)
    expect(v.edges).toHaveLength(3)
  })

  it('按组织（含下级）：只留两端都在组织内的边', () => {
    const v = filterGraph(graph, { ...DEFAULT_GRAPH_FILTER, organizationId: 10 })
    expect(v.nodes.map((n) => n.id)).toEqual([1, 2])
    expect(v.edges.map((e) => e.id)).toEqual([1])
  })

  it('按类别 + 隐藏孤立角色', () => {
    const v = filterGraph(graph, { organizationId: null, categories: ['hostile'], hideIsolated: true })
    expect(v.edges.map((e) => e.id)).toEqual([2])
    expect(v.nodes.map((n) => n.id)).toEqual([2, 3])
  })

  it('节点颜色：主角优先', () => {
    expect(nodeColor({ role_type: '反派', is_protagonist: true })).toBe('#faad14')
    expect(nodeColor({ role_type: '反派', is_protagonist: false })).toBe('#f5222d')
  })
})
