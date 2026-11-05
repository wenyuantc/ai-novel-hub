import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  assignBlockReason,
  assignmentApi,
  careerApi,
  careerBadges,
  DRAFT_NAME_CONFLICT,
  draftBlockReason,
  draftsToBatch,
  formatAssignment,
  formatCareerChange,
  ladderText,
  moveItem,
  newStageRow,
  parseCareerGenerateResult,
  rowsToStages,
  stageLabel,
  stagesToRows,
  toEditableDrafts,
  validateStages,
} from './careers'
import type { Assignment } from './types'

afterEach(() => vi.unstubAllGlobals())

describe('阶梯显示', () => {
  it('阶段 / 挂接 / 阶梯格式', () => {
    expect(stageLabel('筑基', 2, 9)).toBe('筑基（2/9）')
    const a = { career_name: '剑修', stage_name: '筑基', stage_position: 2, stage_total: 9, note: ' 瓶颈期 ' }
    expect(formatAssignment(a)).toBe('剑修·筑基（2/9，瓶颈期）')
    expect(formatAssignment({ ...a, note: '' })).toBe('剑修·筑基（2/9）')
    expect(ladderText([{ name: '练气' }, { name: '筑基' }, { name: '金丹' }])).toBe('练气 → 筑基 → 金丹')
    expect(ladderText([{ name: 'a' }, { name: 'b' }, { name: 'c' }], 2)).toBe('a → b → …')
  })

  it('历史值：role 显示主 / 副职业，空值显示（空）', () => {
    expect(formatCareerChange('role', 'main')).toBe('主职业')
    expect(formatCareerChange('role', null)).toBe('（空）')
    expect(formatCareerChange('stage', '第2阶·筑基')).toBe('第2阶·筑基')
    expect(formatCareerChange('note', '')).toBe('（空）')
  })
})

describe('阶段编辑与提交', () => {
  it('moveItem 上下移动，越界原样返回', () => {
    expect(moveItem([1, 2, 3], 0, 1)).toEqual([2, 1, 3])
    expect(moveItem([1, 2, 3], 2, -2)).toEqual([3, 1, 2])
    const list = [1, 2]
    expect(moveItem(list, 0, -1)).toBe(list)
    expect(moveItem(list, 1, 1)).toBe(list)
  })

  it('重排后提交：顺序即 position，已有阶段带 id、新阶段不带，名称 trim', () => {
    const rows = stagesToRows([
      { id: 11, name: '练气', description: '' },
      { id: 12, name: '筑基', description: '基' },
    ])
    const added = { ...newStageRow(), name: ' 金丹 ' }
    const reordered = moveItem([...rows, added], 2, -2)
    expect(rowsToStages(reordered)).toEqual([
      { name: '金丹', description: '' },
      { id: 11, name: '练气', description: '' },
      { id: 12, name: '筑基', description: '基' },
    ])
  })

  it('validateStages：空阶梯 / 空名 / 重名 / 超长', () => {
    expect(validateStages([])).toBe('职业至少需要 1 个阶段')
    expect(validateStages([{ name: ' ' }])).toBe('阶段名称不能为空')
    expect(validateStages([{ name: '练气' }, { name: ' 练气' }])).toBe('同一职业内阶段名称不能重复')
    expect(validateStages([{ name: 'x'.repeat(51) }])).toContain('50')
    expect(validateStages([{ name: '练气' }, { name: '筑基' }])).toBeNull()
  })
})

describe('挂接上限预检', () => {
  const main = { id: 1, kind: 'main' }
  const sub = (id: number) => ({ id, kind: 'sub' })
  it('主 1 副 3、重复挂', () => {
    expect(assignBlockReason([], main)).toBeNull()
    expect(assignBlockReason([{ career_id: 1, role: 'main' }], main)).toBe('该角色已挂该职业')
    expect(assignBlockReason([{ career_id: 9, role: 'main' }], main)).toBe('该角色已有主职业')
    const three = [2, 3, 4].map((id) => ({ career_id: id, role: 'sub' }))
    expect(assignBlockReason(three, sub(5))).toBe('副职业最多 3 个')
    expect(assignBlockReason(three.slice(0, 2), sub(5))).toBeNull()
    expect(assignBlockReason(three, main)).toBeNull()
  })

  it('careerBadges：按角色汇总主职业与副职业', () => {
    const a = (over: Partial<Assignment>): Assignment => ({
      id: 1,
      character_id: 7,
      career_id: 1,
      career_name: '剑修',
      career_kind: 'main',
      role: 'main',
      stage_id: 1,
      stage_name: '筑基',
      stage_position: 2,
      stage_total: 9,
      note: '',
      updated_at: '2026-10-23T00:00:00',
      ...over,
    })
    const sub = { career_id: 2, career_name: '丹师', career_kind: 'sub', role: 'sub' }
    const map = careerBadges([
      a({}),
      a({ ...sub, stage_name: '一品', stage_position: 1, note: '初学' }),
      a({ ...sub, character_id: 8 }),
    ])
    expect(map.get(7)).toEqual({ main: '剑修·筑基（2/9）', subs: ['丹师·一品（1/9，初学）'] })
    expect(map.get(8)).toEqual({ main: null, subs: ['丹师·筑基（2/9）'] })
    expect(map.get(9)).toBeUndefined()
  })
})

describe('AI 生成职业草稿', () => {
  const result = {
    drafts: [
      { name: '剑修', kind: 'main', stages: [{ name: '练气' }], conflict: true },
      { name: '丹师', kind: 'sub', description: 'd', stages: [{ name: '一品', description: '' }, 'bad'] },
      'bad',
    ],
    warnings: ['w', 1],
  }

  it('收窄：缺字段补默认，kind 非法按主修', () => {
    const r = parseCareerGenerateResult(result)
    expect(r?.drafts).toHaveLength(2)
    expect(r?.drafts[1]).toMatchObject({
      name: '丹师',
      kind: 'sub',
      requirements: '',
      stages: [{ name: '一品', description: '' }],
      conflict: false,
    })
    expect(r?.warnings).toEqual(['w'])
    expect(parseCareerGenerateResult({ drafts: [{ kind: 'x' }] })?.drafts[0].kind).toBe('main')
    expect(parseCareerGenerateResult({})).toBeNull()
  })

  it('同名冲突默认不勾选，改名后才可保存；提交不含 conflict', () => {
    const list = toEditableDrafts(parseCareerGenerateResult(result)!.drafts)
    const none = new Set<string>()
    expect(list[0].checked).toBe(false)
    expect(draftBlockReason({ ...list[0], checked: true }, list, none)).toBe(DRAFT_NAME_CONFLICT)
    expect(draftsToBatch(list, none)).toEqual([
      { name: '丹师', kind: 'sub', description: 'd', requirements: '', abilities: '', rules: '', stages: [{ name: '一品', description: '' }] },
    ])
    const renamed = list.map((d, i) => (i === 0 ? { ...d, name: '剑修（新）', checked: true } : d))
    expect(draftsToBatch(renamed, none).map((d) => d.name)).toEqual(['剑修（新）', '丹师'])
    // 已加载的职业名单也算冲突；取消勾选的不提交
    expect(draftsToBatch(renamed, new Set(['丹师'])).map((d) => d.name)).toEqual(['剑修（新）'])
    expect(draftsToBatch(renamed.map((d) => ({ ...d, checked: false })), none)).toEqual([])
  })

  it('本批内重名 / 阶段不合法阻止保存', () => {
    const list = toEditableDrafts(parseCareerGenerateResult(result)!.drafts).map((d) => ({
      ...d,
      name: '同名',
      checked: true,
      original_conflict: false,
    }))
    expect(draftBlockReason(list[0], list, new Set())).toBe('与本次其他职业同名')
    const noStage = { ...list[1], name: '独立', stages: [] }
    expect(draftBlockReason(noStage, [noStage], new Set())).toBe('职业至少需要 1 个阶段')
  })
})

describe('接口路径', () => {
  it('卸下职业时显式传递章节锚点、操作意图和时间线版本', async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    await assignmentApi.remove(2, 7, 9, {
      edit_mode: 'progression', anchor_kind: 'chapter', chapter_id: 12, boundary: 'end', expected_revision: 4,
    })
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    const [url, request] = calls[0]
    const [path, query] = url.split('?')
    expect(path).toBe('/api/v1/projects/2/characters/7/careers/9')
    expect(Object.fromEntries(new URLSearchParams(query))).toEqual({
      edit_mode: 'progression', anchor_kind: 'chapter', chapter_id: '12', boundary: 'end', expected_revision: '4',
    })
    expect(request.method).toBe('DELETE')
  })

  it('职业 / 挂接 / 历史 / 生成', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([]), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await careerApi.batch(2, [{ name: '剑修', stages: [{ name: '练气' }] }])
    await careerApi.holders(2, 5)
    await careerApi.generate(2, { count: 3, kind: 'sub', instruction: '' })
    await assignmentApi.update(2, 7, 9, { stage_id: 3 })
    await assignmentApi.history(2, 7)
    await assignmentApi.listProject(2)
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.map((c) => [c[0], c[1].method])).toEqual([
      ['/api/v1/projects/2/careers/batch', 'POST'],
      ['/api/v1/projects/2/careers/5/holders', 'GET'],
      ['/api/v1/projects/2/careers/generate', 'POST'],
      ['/api/v1/projects/2/characters/7/careers/9', 'PATCH'],
      ['/api/v1/projects/2/characters/7/career-history', 'GET'],
      ['/api/v1/projects/2/character-careers', 'GET'],
    ])
    expect(JSON.parse(calls[0][1].body as string)).toEqual({ careers: [{ name: '剑修', stages: [{ name: '练气' }] }] })
  })
})
