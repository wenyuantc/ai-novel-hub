import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  buildForeshadowOverrides,
  candidateLabel,
  chapterX,
  defaultPlantChapter,
  defaultResolveChapter,
  EMPTY_FORESHADOW_FORM,
  FORESHADOW_STATUS,
  FORESHADOW_STATUSES,
  foreshadowApi,
  foreshadowChapterSummary,
  foreshadowFormError,
  foreshadowToForm,
  formatForeshadowValue,
  formToForeshadowInput,
  isLongTerm,
  newForeshadowDraft,
  overdueBy,
  readCandidates,
  reminderBadgeCount,
  reminderSections,
  timelineBars,
  timelineRange,
  timelineTicks,
} from './foreshadows'
import type { Foreshadow, ForeshadowReminders, ForeshadowStatus } from './types'

function fs(over: Partial<Foreshadow> = {}): Foreshadow {
  return {
    id: 1,
    project_id: 1,
    title: '玉佩来历',
    content: '主角身上的玉佩',
    plant_hint: '',
    reveal_hint: '',
    status: 'planted',
    planned_plant_chapter: null,
    actual_plant_chapter: 3,
    planned_resolve_chapter: 8,
    actual_resolve_chapter: null,
    importance: 3,
    include_in_context: true,
    note: '',
    source: 'manual',
    character_ids: [],
    character_names: [],
    is_long_term: false,
    overdue_by: 0,
    created_at: '2026-10-26T00:00:00',
    updated_at: '2026-10-26T00:00:00',
    ...over,
  }
}

describe('状态与派生判定', () => {
  it('标签覆盖所有状态', () => {
    const all: ForeshadowStatus[] = ['pending', 'planted', 'resolved', 'partial', 'abandoned']
    expect(FORESHADOW_STATUSES).toEqual(all)
    expect(all.map((s) => FORESHADOW_STATUS[s].label)).toEqual(['待埋', '已埋', '已回收', '部分回收', '已废弃'])
  })

  it('超期：仅已埋 / 部分回收、计划回收章早于当前章', () => {
    expect(overdueBy(fs(), 10)).toBe(2)
    expect(overdueBy(fs(), 8)).toBe(0)
    expect(overdueBy(fs({ status: 'partial' }), 9)).toBe(1)
    expect(overdueBy(fs({ status: 'resolved' }), 10)).toBe(0)
    expect(overdueBy(fs({ status: 'pending' }), 10)).toBe(0)
    expect(overdueBy(fs({ planned_resolve_chapter: null }), 10)).toBe(0)
  })

  it('长线：已埋 / 部分回收且没有计划回收章', () => {
    expect(isLongTerm(fs({ planned_resolve_chapter: null }))).toBe(true)
    expect(isLongTerm(fs({ planned_resolve_chapter: null, status: 'partial' }))).toBe(true)
    expect(isLongTerm(fs({ planned_resolve_chapter: null, status: 'pending' }))).toBe(false)
    expect(isLongTerm(fs())).toBe(false)
  })

  it('章节说明', () => {
    expect(foreshadowChapterSummary(fs({ planned_plant_chapter: 2 }))).toEqual({ plant: '第 3 章（计划第 2 章）', resolve: '计划第 8 章' })
    expect(
      foreshadowChapterSummary(fs({ actual_plant_chapter: null, planned_plant_chapter: null, planned_resolve_chapter: null })),
    ).toEqual({ plant: '未定', resolve: '长线（未定）' })
    expect(foreshadowChapterSummary(fs({ actual_resolve_chapter: 8 })).resolve).toBe('第 8 章')
  })

  it('历史值展示', () => {
    expect(formatForeshadowValue('status', 'resolved')).toBe('已回收')
    expect(formatForeshadowValue('actual_plant_chapter', 3)).toBe('第 3 章')
    expect(formatForeshadowValue('planned_resolve_chapter', null)).toBe('（无）')
    expect(formatForeshadowValue('include_in_context', false)).toBe('关闭')
    expect(formatForeshadowValue('characters', ['林远', '张三'])).toBe('林远、张三')
    expect(formatForeshadowValue('characters', [])).toBe('（无）')
    expect(formatForeshadowValue('note', '')).toBe('（空）')
  })
})

describe('表单', () => {
  it('预检文案与后端一致', () => {
    expect(foreshadowFormError({ ...EMPTY_FORESHADOW_FORM, title: '  ' })).toBe('伏笔标题不能为空')
    expect(foreshadowFormError({ ...EMPTY_FORESHADOW_FORM, title: 'a', status: 'planted' })).toBe('请填写实际埋设章节')
    expect(foreshadowFormError({ ...EMPTY_FORESHADOW_FORM, title: 'a', status: 'partial' })).toBe('请填写实际回收章节')
    expect(foreshadowFormError({ ...EMPTY_FORESHADOW_FORM, title: 'a', status: 'resolved', actual_resolve_chapter: 5 })).toBe('')
    expect(foreshadowFormError({ ...EMPTY_FORESHADOW_FORM, title: 'a' })).toBe('')
  })

  it('表单 → 请求体：去空白、章节显式 null、角色去重、重要度夹紧', () => {
    const body = formToForeshadowInput({
      ...foreshadowToForm(fs({ character_ids: [2, 3] })),
      title: '  玉佩  ',
      planned_resolve_chapter: null,
      character_ids: [2, 2, 3],
      importance: 9,
    })
    expect(body.title).toBe('玉佩')
    expect(body.planned_resolve_chapter).toBeNull()
    expect('planned_resolve_chapter' in body).toBe(true)
    expect(body.character_ids).toEqual([2, 3])
    expect(body.importance).toBe(5)
  })

  it('快捷操作默认章节', () => {
    expect(defaultPlantChapter(fs({ status: 'pending', actual_plant_chapter: null, planned_plant_chapter: 4 }), 6)).toBe(4)
    expect(defaultPlantChapter(fs({ status: 'pending', actual_plant_chapter: null }), 6)).toBe(6)
    expect(defaultPlantChapter(fs({ status: 'pending', actual_plant_chapter: null }), 0)).toBeNull()
    expect(defaultResolveChapter(fs(), 10)).toBe(10)
    expect(defaultResolveChapter(fs({ actual_plant_chapter: 12 }), 10)).toBe(12)
    expect(defaultResolveChapter(fs(), 0)).toBe(8)
  })
})

describe('确认新伏笔', () => {
  const after = { title: '神秘信件', content: '没有署名', plant_hint: '', planned_resolve_chapter: 12, importance: 3 }

  it('after → 初值', () => {
    expect(newForeshadowDraft(after)).toEqual({
      title: '神秘信件',
      content: '没有署名',
      plant_hint: '',
      reveal_hint: '',
      planned_resolve_chapter: 12,
      importance: 3,
      character_ids: [],
    })
    expect(newForeshadowDraft(null).importance).toBe(3)
  })

  it('只发改动的字段；清空计划回收章显式 null；关联角色动过或改变才发（含空列表）', () => {
    const init = newForeshadowDraft(after)
    expect(buildForeshadowOverrides(init, init)).toEqual({ ok: true, overrides: {} })
    expect(
      buildForeshadowOverrides(init, { ...init, title: ' 无名信 ', planned_resolve_chapter: null, importance: 5, character_ids: [4, 4] }),
    ).toEqual({ ok: true, overrides: { title: '无名信', planned_resolve_chapter: null, importance: 5, character_ids: [4] } })
    expect(buildForeshadowOverrides(init, { ...init, reveal_hint: '揭示寄信人' })).toEqual({
      ok: true,
      overrides: { reveal_hint: '揭示寄信人' },
    })
    expect(buildForeshadowOverrides(init, { ...init, title: '  ' })).toEqual({ ok: false, error: '伏笔标题不能为空' })
    // 动过关联角色就提交，含清空后的空列表；没动过且与初值相同则不发
    expect(buildForeshadowOverrides(init, init, { charactersTouched: true })).toEqual({
      ok: true,
      overrides: { character_ids: [] },
    })
    const withChars = { ...init, character_ids: [4, 7] }
    expect(buildForeshadowOverrides(withChars, { ...withChars, character_ids: [] })).toEqual({
      ok: true,
      overrides: { character_ids: [] },
    })
    expect(buildForeshadowOverrides(withChars, { ...withChars, character_ids: [7, 4] })).toEqual({ ok: true, overrides: {} })
  })

  it('候选规范化与说明', () => {
    const list = readCandidates([
      { action: 'resolve', ref_id: 5, text: '旧伤', keyword: '旧伤', expected_resolve_chapter: null, partial: true },
      { action: 'plant', text: '信件', keyword: '信' },
      { action: 'other' },
      'bad',
    ])
    expect(list).toHaveLength(2)
    expect(list.map(candidateLabel)).toEqual(['回收 #5（部分）', '新埋设'])
    expect(list[1].ref_id).toBeNull()
    expect(candidateLabel({ ...list[1], ref_id: 3 })).toBe('埋下 #3')
    expect(readCandidates(null)).toEqual([])
  })
})

describe('章节提醒', () => {
  const rem: ForeshadowReminders = {
    chapter_no: 8,
    to_plant: [fs({ id: 12, status: 'pending', plant_hint: '打斗间隙瞥见' })],
    must_resolve: [fs({ id: 5, reveal_hint: '旧伤来自宗主' }), fs({ id: 6 })],
    overdue: [fs({ id: 3, overdue_by: 2 })],
    upcoming: [],
    long_term: [fs({ id: 2, planned_resolve_chapter: null })],
  }

  it('五档顺序、跳过空档、附加说明', () => {
    const sections = reminderSections(rem)
    expect(sections.map((s) => s.key)).toEqual(['to_plant', 'must_resolve', 'overdue', 'long_term'])
    expect(sections[0].items[0].detail).toBe('埋法：打斗间隙瞥见')
    expect(sections[1].items.map((i) => i.detail)).toEqual(['回收：旧伤来自宗主', ''])
    expect(sections[2].items[0].detail).toBe('已超期 2 章')
    expect(reminderSections(null)).toEqual([])
  })

  it('徽标数 = 计划埋设 + 必须回收', () => {
    expect(reminderBadgeCount(rem)).toBe(3)
    expect(reminderBadgeCount(undefined)).toBe(0)
  })
})

describe('时间线', () => {
  it('已回收：实际埋设到实际回收实线，两端实心', () => {
    const [bar] = timelineBars([fs({ status: 'resolved', actual_resolve_chapter: 7 })], 10)
    expect(bar.segments).toEqual([{ from: 3, to: 7, kind: 'solid' }])
    expect(bar.markers).toEqual([
      { chapter: 3, type: 'plant', actual: true },
      { chapter: 7, type: 'resolve', actual: true },
    ])
    expect(bar.arrow).toBe(false)
  })

  it('已埋未到期：到计划回收章虚线，回收点空心', () => {
    const [bar] = timelineBars([fs()], 5)
    expect(bar.segments).toEqual([{ from: 3, to: 8, kind: 'dashed' }])
    expect(bar.markers[1]).toEqual({ chapter: 8, type: 'resolve', actual: false })
    expect(bar.overdue).toBe(0)
  })

  it('超期：虚线到计划回收章，再接红线到当前章', () => {
    const [bar] = timelineBars([fs()], 11)
    expect(bar.overdue).toBe(3)
    expect(bar.segments).toEqual([
      { from: 3, to: 8, kind: 'dashed' },
      { from: 8, to: 11, kind: 'overdue' },
    ])
  })

  it('长线：延伸到当前章并带箭头', () => {
    const [bar] = timelineBars([fs({ planned_resolve_chapter: null })], 10)
    expect(bar.arrow).toBe(true)
    expect(bar.segments).toEqual([{ from: 3, to: 10, kind: 'dashed' }])
  })

  it('待埋：计划埋设到计划回收虚线、两端空心；废弃同理', () => {
    const [bar] = timelineBars(
      [fs({ status: 'pending', actual_plant_chapter: null, planned_plant_chapter: 4, planned_resolve_chapter: 9 })],
      2,
    )
    expect(bar.segments).toEqual([{ from: 4, to: 9, kind: 'dashed' }])
    expect(bar.markers.every((m) => !m.actual)).toBe(true)
  })

  it('部分回收：实线到实际回收章，未收束部分继续虚线到计划回收章', () => {
    const [bar] = timelineBars([fs({ status: 'partial', actual_resolve_chapter: 6, planned_resolve_chapter: 9 })], 7)
    expect(bar.segments).toEqual([
      { from: 3, to: 6, kind: 'solid' },
      { from: 6, to: 9, kind: 'dashed' },
    ])
  })

  it('范围、坐标与刻度', () => {
    const bars = timelineBars([fs(), fs({ id: 2, actual_plant_chapter: 5, planned_resolve_chapter: 20 })], 12)
    const range = timelineRange(bars, 12)
    expect(range).toEqual({ min: 3, max: 20 })
    expect(chapterX(3, range, 30, 100)).toBe(100)
    expect(chapterX(5, range, 30, 100)).toBe(160)
    expect(timelineRange([], 0)).toEqual({ min: 1, max: 2 })
    expect(timelineTicks({ min: 3, max: 6 })).toEqual([3, 4, 5, 6])
    const ticks = timelineTicks({ min: 1, max: 120 })
    expect(ticks[0]).toBe(1)
    expect(ticks[ticks.length - 1]).toBe(120)
    expect(ticks).toContain(10)
    expect(ticks.length).toBeLessThanOrEqual(42)
  })
})

describe('接口路径', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('CRUD / 快捷操作 / 历史 / 统计 / 提醒', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await foreshadowApi.list(2)
    await foreshadowApi.list(2, { status: 'planted', overdue: true, long_term: false, character_id: 7, sort: 'importance' })
    await foreshadowApi.create(2, { title: 'a' })
    await foreshadowApi.update(2, 5, { planned_resolve_chapter: null })
    await foreshadowApi.plant(2, 5, 3)
    await foreshadowApi.resolve(2, 5, 8, true)
    await foreshadowApi.abandon(2, 5)
    await foreshadowApi.history(2, 5)
    await foreshadowApi.stats(2)
    await foreshadowApi.reminders(2, 9)
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.map(([url, init]) => `${init.method ?? 'GET'} ${url}`)).toEqual([
      'GET /api/v1/projects/2/foreshadows',
      'GET /api/v1/projects/2/foreshadows?status=planted&overdue=true&character_id=7&sort=importance',
      'POST /api/v1/projects/2/foreshadows',
      'PATCH /api/v1/projects/2/foreshadows/5',
      'POST /api/v1/projects/2/foreshadows/5/plant',
      'POST /api/v1/projects/2/foreshadows/5/resolve',
      'POST /api/v1/projects/2/foreshadows/5/abandon',
      'GET /api/v1/projects/2/foreshadows/5/history',
      'GET /api/v1/projects/2/foreshadows/stats',
      'GET /api/v1/projects/2/chapters/9/foreshadow-reminders',
    ])
    expect(JSON.parse(String(calls[3][1].body))).toEqual({ planned_resolve_chapter: null })
    expect(JSON.parse(String(calls[5][1].body))).toEqual({ chapter_no: 8, partial: true })
  })
})
