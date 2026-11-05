import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  describeRelation,
  formatChangeValue,
  intimacyColor,
  parseTypeOptionValue,
  perspective,
  typeOptions,
  typeOptionValue,
  relationshipApi,
  RELATION_SOURCE_LABELS,
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

afterEach(() => { vi.unstubAllGlobals() })

it('生成接口携带所选 ID / 条数 / 配置与要求，返回新 kind 任务；AI 生成来源有中文标签', async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ id: 27, kind: 'relations.generate' }), { status: 202 }))
  vi.stubGlobal('fetch', fetchMock)
  const body = { character_ids: [3, 5], count: 1, instruction: '成长与信任', ai_config_id: 4 }
  expect(await relationshipApi.generate(8, body)).toMatchObject({ id: 27, kind: 'relations.generate' })
  expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/8/relationships/generate')
  expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', credentials: 'include', body: JSON.stringify(body) })
  expect(RELATION_SOURCE_LABELS.ai_generate).toBe('AI 生成')
})

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

it('按人物对读取已结束关系的旧审计，不依赖当前关系列表', async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([{ id: 6, relationship_id: 12, changes: {} }]), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  const items = await relationshipApi.historyForPair(8, 9, 2)
  expect(items[0].relationship_id).toBe(12)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/8/relationship-history?character_id=9&related_character_id=2')
})

it('删除关系时可携带时间线版本做 CAS，省略时保持无参数请求', async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
  vi.stubGlobal('fetch', fetchMock)
  await relationshipApi.remove(8, 12, 5)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/8/relationships/12?expected_revision=5')
  expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'DELETE' })
  await relationshipApi.remove(8, 12)
  expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/projects/8/relationships/12')
})
