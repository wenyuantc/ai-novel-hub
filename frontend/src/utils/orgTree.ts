// 组织树：按 parent_id 构建（上级缺失 / 成环时挂到顶级），并计算不可选为上级的组织集合

export interface OrgLike {
  id: number
  name: string
  parent_id: number | null
  sort?: number
}

export interface OrgTreeNode<T extends OrgLike> {
  org: T
  children: OrgTreeNode<T>[]
}

function bySort<T extends OrgLike>(a: OrgTreeNode<T>, b: OrgTreeNode<T>): number {
  return (a.org.sort ?? 0) - (b.org.sort ?? 0) || a.org.id - b.org.id
}

/** 构建组织树。上级不存在、或沿上级链回到自身（脏数据）的组织作为顶级节点，保证每个组织恰好出现一次。 */
export function buildOrgTree<T extends OrgLike>(orgs: T[]): OrgTreeNode<T>[] {
  const nodes = new Map<number, OrgTreeNode<T>>(orgs.map((o) => [o.id, { org: o, children: [] }]))
  const byId = new Map(orgs.map((o) => [o.id, o]))
  const inCycle = (o: T): boolean => {
    const seen = new Set<number>([o.id])
    let cur = o.parent_id
    while (cur !== null) {
      if (seen.has(cur)) return true
      seen.add(cur)
      cur = byId.get(cur)?.parent_id ?? null
    }
    return false
  }
  const roots: OrgTreeNode<T>[] = []
  for (const o of orgs) {
    const node = nodes.get(o.id) as OrgTreeNode<T>
    const parent = o.parent_id !== null ? nodes.get(o.parent_id) : undefined
    if (parent && !inCycle(o)) parent.children.push(node)
    else roots.push(node)
  }
  const sortRec = (list: OrgTreeNode<T>[]) => {
    list.sort(bySort)
    for (const n of list) sortRec(n.children)
  }
  sortRec(roots)
  return roots
}

/** 组织自身及其全部下级的 id（编辑上级时这些都不能选，否则成环） */
export function descendantIds(orgs: OrgLike[], id: number): Set<number> {
  const children = new Map<number, number[]>()
  for (const o of orgs) {
    if (o.parent_id === null) continue
    const list = children.get(o.parent_id) ?? []
    list.push(o.id)
    children.set(o.parent_id, list)
  }
  const out = new Set<number>([id])
  const stack = [id]
  while (stack.length > 0) {
    const cur = stack.pop() as number
    for (const c of children.get(cur) ?? []) {
      if (!out.has(c)) {
        out.add(c)
        stack.push(c)
      }
    }
  }
  return out
}

/** 上级链名称（从顶级到自身），用于面包屑展示 */
export function orgPath(orgs: OrgLike[], id: number): string[] {
  const byId = new Map(orgs.map((o) => [o.id, o]))
  const path: string[] = []
  const seen = new Set<number>()
  let cur: number | null = id
  while (cur !== null && !seen.has(cur)) {
    seen.add(cur)
    const o = byId.get(cur)
    if (!o) break
    path.unshift(o.name)
    cur = o.parent_id
  }
  return path
}
