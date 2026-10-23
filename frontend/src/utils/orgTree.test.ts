import { describe, expect, it } from 'vitest'
import { buildOrgTree, descendantIds, orgPath } from './orgTree'

const orgs = [
  { id: 1, name: '朝廷', parent_id: null, sort: 0 },
  { id: 2, name: '六部', parent_id: 1, sort: 1 },
  { id: 3, name: '刑部', parent_id: 2, sort: 0 },
  { id: 4, name: '户部', parent_id: 2, sort: 0 },
  { id: 5, name: '江湖', parent_id: 99, sort: 0 },
]

describe('组织树', () => {
  it('按上级嵌套，上级缺失的作为顶级，组内按 sort / id 排序', () => {
    const tree = buildOrgTree(orgs)
    expect(tree.map((n) => n.org.name)).toEqual(['朝廷', '江湖'])
    expect(tree[0].children[0].children.map((n) => n.org.name)).toEqual(['刑部', '户部'])
  })

  it('成环的脏数据也不会丢节点', () => {
    const tree = buildOrgTree([
      { id: 1, name: 'A', parent_id: 2 },
      { id: 2, name: 'B', parent_id: 1 },
    ])
    const count = (ns: typeof tree): number => ns.reduce((s, n) => s + 1 + count(n.children), 0)
    expect(count(tree)).toBe(2)
  })

  it('自身与下级不可作为上级；路径', () => {
    expect([...descendantIds(orgs, 2)].sort()).toEqual([2, 3, 4])
    expect([...descendantIds(orgs, 3)]).toEqual([3])
    expect(orgPath(orgs, 3)).toEqual(['朝廷', '六部', '刑部'])
  })
})
