import { describe, expect, expectTypeOf, it } from 'vitest'
import { entityDetailHref, readEntityFieldPosition } from './entityLinks'
import type { EntityRef } from './entityLinks'

describe('设定条目详情链接', () => {
  it.each([
    ['character', 'characters'], ['world', 'world'], ['organization', 'organizations'], ['career', 'careers'],
  ] as const)('%s 跳转到本作品的对应详情页', (kind, page) => {
    expect(entityDetailHref(17, { kind, id: 8 })).toBe(`/projects/17/${page}?id=8`)
  })

  it('引用合同仅包含四类条目，不把关系时间线当作实体 id', () => {
    expectTypeOf<EntityRef['kind']>().toEqualTypeOf<'character' | 'world' | 'organization' | 'career'>()
  })
  it('已有章节和计划章节深链可往返，保留章初章末且不混淆锚点', () => {
    for (const position of [
      { anchor_kind: 'chapter' as const, chapter_id: 5, chapter_no: null, boundary: 'start' as const },
      { anchor_kind: 'planned' as const, chapter_id: null, chapter_no: 20, boundary: 'end' as const },
    ]) {
      const url = new URL(entityDetailHref(17, { kind: 'organization', id: 8 }, position), 'https://example.test')
      expect(url.pathname).toBe('/projects/17/organizations')
      expect(readEntityFieldPosition(url.searchParams)).toEqual(position)
    }
  })
  it('无查看意图、混合锚和非法位置不会被当作有效值请求', () => {
    for (const query of ['chapter_no=5&boundary=start', 'view=effective&chapter_id=5&chapter_no=5&boundary=start', 'view=effective&chapter_no=1e2&boundary=start', 'view=effective&chapter_id=2147483648&boundary=end', 'view=effective&chapter_no=5&boundary=bad']) expect(readEntityFieldPosition(new URLSearchParams(query))).toBeNull()
  })
})
