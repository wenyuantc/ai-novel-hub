import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  buildExpandSync,
  canBuild,
  canCreateSingleChapter,
  chapterRange,
  continueAppendItems,
  continueDraftError,
  continuePreviewNos,
  expandStatus,
  keptPlans,
  MAX_OUTLINE_BYTES,
  outlineFilename,
  outlineProApi,
  parseContinueResult,
  parseExpandResult,
  parseOutlineFileText,
  planDraftError,
  removePlanSync,
  replacedPlans,
  SNAPSHOT_REASON_LABELS,
  snapshotApi,
  toEditableContinueDrafts,
  toEditablePlanDrafts,
} from './outlines'
import type { Plan, SnapshotReason } from './types'

function plan(over: Partial<Plan> = {}): Plan {
  return {
    id: 1,
    project_id: 1,
    outline_id: 3,
    seq: 1,
    title: '细纲',
    summary: '梗概',
    key_events: ['事件A'],
    character_ids: [7],
    emotion: '紧张',
    goal: '目标',
    est_words: 2500,
    chapter_id: null,
    chapter_no: null,
    has_content: false,
    created_at: '2026-10-26T00:00:00',
    updated_at: '2026-10-26T00:00:00',
    ...over,
  }
}

describe('大纲展开状态', () => {
  it('未展开 / 已建章 / 细纲建章进度', () => {
    expect(expandStatus({ plan_count: 0, built_count: 0, chapter_id: null }).label).toBe('未展开')
    expect(expandStatus({ plan_count: 0, built_count: 0, chapter_id: 5 }).label).toBe('未展开 · 已建章')
    expect(expandStatus({ plan_count: 3, built_count: 0, chapter_id: null }).label).toBe('3 章细纲 · 未建章')
    expect(expandStatus({ plan_count: 3, built_count: 1, chapter_id: null }).label).toBe('3 章细纲 · 1 章已建')
    expect(expandStatus({ plan_count: 3, built_count: 3, chapter_id: null }).label).toBe('3 章细纲 · 全部已建')
  })

  it('章节号范围、可建章、可 1 纲 1 章建章', () => {
    expect(chapterRange({ chapter_no: 3, plans: [] })).toBe('3')
    expect(chapterRange({ chapter_no: 3, plans: [plan({ chapter_no: 3 }), plan({ chapter_no: 5 }), plan()] })).toBe('3–5')
    expect(chapterRange({ chapter_no: 3, plans: [plan({ chapter_no: 4 })] })).toBe('4')
    expect(canBuild({ plan_count: 3, built_count: 1 })).toBe(true)
    expect(canBuild({ plan_count: 3, built_count: 3 })).toBe(false)
    expect(canBuild({ plan_count: 0, built_count: 0 })).toBe(false)
    expect(canCreateSingleChapter({ plan_count: 0, chapter_id: null })).toBe(true)
    expect(canCreateSingleChapter({ plan_count: 0, chapter_id: 2 })).toBe(false)
    expect(canCreateSingleChapter({ plan_count: 2, chapter_id: null })).toBe(false)
  })

  it('快照原因标签覆盖全部取值', () => {
    const reasons: SnapshotReason[] = ['wizard_replace', 'import_replace', 'reexpand', 'manual', 'restore']
    for (const r of reasons) expect(SNAPSHOT_REASON_LABELS[r].label).toBeTruthy()
  })
})

describe('续写预览', () => {
  const result = {
    drafts: [1, 2, 3, 4, 5].map((i) => ({
      volume_no: 2,
      volume_title: '下卷',
      chapter_no: 10 + i,
      title: `章${i}`,
      summary: `梗概${i}`,
    })),
    warnings: ['少了一条', 3],
  }

  it('AC1：删 1 条、改 1 条后追加 4 条，预计章节号 11–14', () => {
    const parsed = parseContinueResult(result)
    expect(parsed?.warnings).toEqual(['少了一条'])
    let drafts = toEditableContinueDrafts(parsed?.drafts ?? [])
    drafts = drafts.filter((d) => d.key !== drafts[1].key)
    drafts = drafts.map((d, i) => (i === 0 ? { ...d, title: '  改过的标题 ' } : d))
    const items = continueAppendItems(drafts)
    expect(items).toHaveLength(4)
    expect(items[0]).toEqual({ volume_no: 2, volume_title: '下卷', title: '改过的标题', summary: '梗概1' })
    expect([...continuePreviewNos(drafts, 11).values()]).toEqual([11, 12, 13, 14])
  })

  it('取消勾选的不追加也不占号；标题必填', () => {
    const drafts = toEditableContinueDrafts(parseContinueResult(result)?.drafts ?? [])
    drafts[0].checked = false
    expect(continueAppendItems(drafts)).toHaveLength(4)
    const nos = continuePreviewNos(drafts, 11)
    expect(nos.has(drafts[0].key)).toBe(false)
    expect(nos.get(drafts[1].key)).toBe(11)
    expect(continueDraftError({ title: '  ', volume_title: '' })).toBe('标题不能为空')
    expect(continueDraftError({ title: '好', volume_title: '' })).toBeNull()
  })

  it('结构不对返回 null', () => {
    expect(parseContinueResult(null)).toBeNull()
    expect(parseContinueResult({ drafts: 'x' })).toBeNull()
  })
})

describe('展开预览', () => {
  const result = {
    outline_id: 3,
    drafts: [
      {
        title: '新一',
        summary: '甲',
        key_events: ['e1'],
        characters: [
          { name: '林远', character_id: 7, resolved: true },
          { name: '路人', character_id: null, resolved: false },
          { name: '林远', character_id: 7, resolved: true },
        ],
        emotion: '压抑',
        goal: '铺垫',
        est_words: 99999,
      },
      { title: '新二', summary: '乙', key_events: [], characters: [], emotion: '', goal: '', est_words: 3000 },
    ],
    existing: [{ id: 11, seq: 1, title: '旧一', summary: '', chapter_id: 5, chapter_no: 3, has_content: true }],
    warnings: [],
  }

  it('收窄结果：只取 resolved 的角色 id，未匹配的名字单独提示；字数夹到 300–20000', () => {
    const parsed = parseExpandResult(result)
    expect(parsed?.existing[0].has_content).toBe(true)
    const drafts = toEditablePlanDrafts(parsed?.drafts ?? [])
    expect(drafts[0].character_ids).toEqual([7])
    expect(drafts[0].unresolved).toEqual(['路人'])
    expect(drafts[0].est_words).toBe(20000)
    expect(parseExpandResult({ drafts: [] })).toBeNull()
  })

  it('AC3：保留有正文的细纲（带 id、全字段）在前，没有正文的旧细纲不提交；未勾选草稿不提交', () => {
    const plans = [
      plan({ id: 11, seq: 1, has_content: true, chapter_id: 5, chapter_no: 3 }),
      plan({ id: 12, seq: 2, chapter_id: 6, chapter_no: 4 }),
      plan({ id: 13, seq: 3 }),
    ]
    expect(keptPlans(plans).map((p) => p.id)).toEqual([11])
    expect(replacedPlans(plans).map((p) => p.id)).toEqual([12, 13])
    const drafts = toEditablePlanDrafts(parseExpandResult(result)?.drafts ?? [])
    drafts[1].checked = false
    const body = buildExpandSync(keptPlans(plans), drafts)
    expect(body.plans).toHaveLength(2)
    expect(body.plans[0]).toEqual({
      id: 11,
      title: '细纲',
      summary: '梗概',
      key_events: ['事件A'],
      character_ids: [7],
      emotion: '紧张',
      goal: '目标',
      est_words: 2500,
    })
    expect(body.plans[1].id).toBeUndefined()
    expect(body.plans[1].title).toBe('新一')
    expect(body.plans[1].character_ids).toEqual([7])
  })

  it('删除单条细纲：其余原样带 id 提交', () => {
    const body = removePlanSync([plan({ id: 1 }), plan({ id: 2, title: '二' })], 1)
    expect(body.plans.map((p) => p.id)).toEqual([2])
    expect(body.plans[0].title).toBe('二')
  })

  it('细纲校验', () => {
    expect(planDraftError({ title: '', emotion: '', est_words: 3000 })).toBe('标题不能为空')
    expect(planDraftError({ title: 'a', emotion: '', est_words: 100 })).toContain('300')
    expect(planDraftError({ title: 'a', emotion: 'x'.repeat(101), est_words: 3000 })).toContain('100')
    expect(planDraftError({ title: 'a', emotion: '', est_words: 3000 })).toBeNull()
  })
})

describe('导入导出', () => {
  it('解析文件：非 JSON / 超过 2MB（紧凑序列化口径）', () => {
    expect(parseOutlineFileText('{bad').error).toBe('文件不是有效的 JSON')
    expect(parseOutlineFileText('{"format":"ainovel.outline"}').error).toBeNull()
    const big = JSON.stringify({ s: 'x'.repeat(MAX_OUTLINE_BYTES) })
    expect(parseOutlineFileText(big).error).toBe('大纲文件过大，上限为 2MB')
    // 缩进不计入大小
    const indented = JSON.stringify({ a: [1, 2, 3] }, null, 8)
    expect(parseOutlineFileText(indented).error).toBeNull()
  })

  it('导出文件名', () => {
    expect(outlineFilename('我的/小说', new Date(2026, 9, 26))).toBe('我的_小说-大纲-20261026.json')
    expect(outlineFilename('  ', new Date(2026, 0, 2))).toBe('ainovel-大纲-20260102.json')
  })
})

describe('接口路径', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('大纲进阶 / 快照', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await outlineProApi.tree(2)
    await outlineProApi.move(2, 3, 'up')
    await outlineProApi.append(2, [{ volume_no: 1, volume_title: '', title: 'a', summary: '' }])
    await outlineProApi.plans(2, 3)
    await outlineProApi.updatePlan(2, 3, 4, { title: 'b' })
    await outlineProApi.syncPlans(2, 3, { plans: [] })
    await outlineProApi.build(2, 3)
    await outlineProApi.chapterPlan(2, 9)
    await outlineProApi.continue(2, { count: 5 })
    await outlineProApi.expand(2, 3, { count: 3 })
    await outlineProApi.export(2)
    await outlineProApi.importPreview(2, { x: 1 })
    await outlineProApi.import(2, { x: 1 }, 'replace')
    await snapshotApi.list(2)
    await snapshotApi.create(2, '备注')
    await snapshotApi.get(2, 5)
    await snapshotApi.restore(2, 5)
    await snapshotApi.remove(2, 5)
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.map(([url, init]) => `${init.method ?? 'GET'} ${url}`)).toEqual([
      'GET /api/v1/projects/2/outlines/tree',
      'POST /api/v1/projects/2/outlines/3/move',
      'POST /api/v1/projects/2/outlines/append',
      'GET /api/v1/projects/2/outlines/3/plans',
      'PATCH /api/v1/projects/2/outlines/3/plans/4',
      'PUT /api/v1/projects/2/outlines/3/plans',
      'POST /api/v1/projects/2/outlines/3/plans/build',
      'GET /api/v1/projects/2/chapters/9/plan',
      'POST /api/v1/projects/2/outlines/continue',
      'POST /api/v1/projects/2/outlines/3/expand',
      'GET /api/v1/projects/2/outlines/export',
      'POST /api/v1/projects/2/outlines/import/preview',
      'POST /api/v1/projects/2/outlines/import',
      'GET /api/v1/projects/2/outline-snapshots',
      'POST /api/v1/projects/2/outline-snapshots',
      'GET /api/v1/projects/2/outline-snapshots/5',
      'POST /api/v1/projects/2/outline-snapshots/5/restore',
      'DELETE /api/v1/projects/2/outline-snapshots/5',
    ])
    expect(JSON.parse(String(calls[1][1].body))).toEqual({ direction: 'up' })
    expect(JSON.parse(String(calls[12][1].body))).toEqual({ data: { x: 1 }, mode: 'replace' })
    expect(JSON.parse(String(calls[14][1].body))).toEqual({ note: '备注' })
  })
})
