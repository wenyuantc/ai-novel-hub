import { describe, expect, it } from 'vitest'
import {
  describeRelation,
  formatChangeValue,
  intimacyColor,
  parseTypeOptionValue,
  perspective,
  typeOptions,
  typeOptionValue,
} from './relationships'
import type { RelationshipType } from './types'

const t = (id: number, role_a: string, role_b: string, category = 'social'): RelationshipType => ({
  id,
  project_id: null,
  category,
  role_a,
  role_b,
  sort: 0,
  builtin: true,
})

const rel = { a_id: 1, b_id: 2, a_name: 'A', b_name: 'B', role_a: '师父', role_b: '徒弟' }

describe('关系视角', () => {
  it('a 视角对方是 role_b，b 视角对方是 role_a', () => {
    expect(perspective(rel, 1)).toEqual({ otherId: 2, otherName: 'B', otherRole: '徒弟', selfRole: '师父' })
    expect(perspective(rel, 2)).toEqual({ otherId: 1, otherName: 'A', otherRole: '师父', selfRole: '徒弟' })
    expect(perspective(rel, 3)).toBeNull()
  })

  it('一句话描述：非对称 / 对称', () => {
    expect(describeRelation(rel)).toBe('A 是 B 的师父')
    expect(describeRelation({ ...rel, role_a: '朋友', role_b: '朋友' })).toBe('A 与 B 是朋友')
  })

  it('类型选项：非对称两个方向，对称只有一个', () => {
    const opts = typeOptions([t(1, '师父', '徒弟'), t(2, '朋友', '朋友')])
    expect(opts.map((o) => o.label)).toEqual([
      '对方是我的徒弟（我是师父）',
      '对方是我的师父（我是徒弟）',
      '对方与我是朋友',
    ])
    expect(opts[0]).toMatchObject({ value: '1:a', typeId: 1, viewerIsA: true })
    expect(opts[1]).toMatchObject({ value: '1:b', typeId: 1, viewerIsA: false })
    expect(typeOptions([t(1, '师父', '徒弟')], '林远', '张三')[0].label).toBe('张三是林远的徒弟（林远是师父）')
  })

  it('选项值往返', () => {
    expect(parseTypeOptionValue(typeOptionValue(12, false))).toEqual({ typeId: 12, viewerIsA: false })
    expect(parseTypeOptionValue('x')).toBeNull()
  })
})

describe('亲密度颜色', () => {
  it('-100 红、0 灰、100 绿，越界截断', () => {
    expect(intimacyColor(0)).toBe('#8c8c8c')
    expect(intimacyColor(-100)).toBe('#f5222d')
    expect(intimacyColor(100)).toBe('#52c41a')
    expect(intimacyColor(500)).toBe('#52c41a')
    expect(intimacyColor(Number.NaN)).toBe('#8c8c8c')
    expect(intimacyColor(50)).not.toBe(intimacyColor(-50))
  })
})

it('历史值展示', () => {
  expect(formatChangeValue(null)).toBe('（空）')
  expect(formatChangeValue('')).toBe('（空）')
  expect(formatChangeValue(80)).toBe('80')
  expect(formatChangeValue('师父/徒弟')).toBe('师父/徒弟')
})
