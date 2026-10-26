import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ANALYSIS_STATUS,
  analysisApi,
  buildReviseRequest,
  CHANGE_KIND_LABELS,
  CHANGE_KINDS,
  CHANGE_STATUS,
  changeCountsLabel,
  countPending,
  describeChange,
  formatDelta,
  formatRatio,
  formatScore,
  groupChanges,
  parseAnalyzeDone,
  replaceChange,
  reviseChapter,
  reviseCompareFor,
  scoreDelta,
  trendSegments,
  type ReviseFormValues,
} from './analysis'
import type { AnalysisChange, AnalysisStatus, ChangeKind, ChangeStatus } from './types'

function change(over: Partial<AnalysisChange> = {}): AnalysisChange {
  return {
    id: 1,
    analysis_id: 10,
    chapter_no: 3,
    kind: 'character_state',
    target: { character_id: 1, name: '林远' },
    field_key: 'char:1:current_state',
    before: '',
    after: '重伤',
    status: 'applied',
    reason: '',
    applied_at: null,
    resolved_at: null,
    created_at: '2026-10-26T00:00:00',
    ...over,
  }
}

describe('分析状态与评分', () => {
  it('四种分析状态都有标签', () => {
    const all: AnalysisStatus[] = ['none', 'analyzing', 'analyzed', 'stale']
    expect(all.map((s) => ANALYSIS_STATUS[s].label)).toEqual(['未分析', '分析中', '已分析', '已过期'])
  })

  it('scoreDelta：一位小数，任一方缺失为 null', () => {
    const d = scoreDelta({ overall: 7.3, pacing: 8, engagement: 6, coherence: null }, { overall: 6.1, pacing: 8.5, engagement: 6 })
    expect(d).toEqual({ overall: 1.2, pacing: -0.5, engagement: 0, coherence: null })
    expect(scoreDelta({ overall: 7 }, null).overall).toBeNull()
  })

  it('formatDelta / formatScore / formatRatio', () => {
    expect(formatDelta(1.2)).toBe('↑1.2')
    expect(formatDelta(-0.5)).toBe('↓0.5')
    expect(formatDelta(0)).toBe('持平')
    expect(formatDelta(null)).toBe('')
    expect(formatScore({ overall: 7 })).toBe('7.0')
    expect(formatScore({ overall: null })).toBe('-')
    expect(formatScore(null, 'pacing')).toBe('-')
    expect(formatRatio(0.346)).toBe('35%')
    expect(formatRatio(undefined)).toBe('-')
  })

  it('trendSegments：缺失值断开折线，单点居中，纵轴 1–10', () => {
    expect(trendSegments([10, 1], 100, 100, 0)).toEqual(['0,0 100,100'])
    expect(trendSegments([5, null, 6, 7], 30, 90, 0)).toEqual(['0,50', '20,40 30,30'])
    expect(trendSegments([10], 100, 50, 0)).toEqual(['50,0'])
    expect(trendSegments([], 100, 50)).toEqual([])
  })
})

describe('回写条目展示', () => {
  it('标签覆盖所有类型与状态', () => {
    const kinds: ChangeKind[] = [
      'character_state',
      'life_status',
      'relationship_update',
      'relationship_create',
      'career_stage',
      'org_membership',
    ]
    expect(CHANGE_KINDS).toEqual(kinds)
    for (const k of kinds) expect(CHANGE_KIND_LABELS[k]).toBeTruthy()
    const statuses: ChangeStatus[] = ['applied', 'pending', 'reverted', 'rejected', 'skipped', 'failed']
    expect(statuses.map((s) => CHANGE_STATUS[s].label)).toEqual(['已写入', '待确认', '已撤销', '已拒绝', '已跳过', '失败'])
  })

  it('角色状态 / 存活状态', () => {
    expect(describeChange(change())).toEqual({ subject: '林远', field: '当前状态', before: '（空）', after: '重伤' })
    expect(
      describeChange(change({ kind: 'life_status', target: { character_id: 2, name: '苏晚' }, before: 'alive', after: 'dead' })),
    ).toEqual({ subject: '苏晚', field: '存活状态', before: '存活', after: '死亡' })
  })

  it('关系字段更新：亲密度 / 状态中文 / 描述', () => {
    const target = { relationship_id: 5, a_id: 1, b_id: 3, a_name: '林远', b_name: '张三', field: 'intimacy' }
    expect(describeChange(change({ kind: 'relationship_update', target, before: 50, after: 20 }))).toEqual({
      subject: '林远 ↔ 张三',
      field: '亲密度',
      before: '50',
      after: '20',
    })
    const st = describeChange(
      change({ kind: 'relationship_update', target: { ...target, field: 'status' }, before: 'normal', after: 'broken' }),
    )
    expect([st.field, st.before, st.after]).toEqual(['关系状态', '正常', '破裂'])
  })

  it('新建关系：before 为无，after 汇总称呼 / 类别 / 亲密度 / 状态 / 描述', () => {
    const v = describeChange(
      change({
        kind: 'relationship_create',
        target: { a_id: 1, b_id: 4, a_name: '林远', b_name: '王五' },
        before: null,
        after: { role_a: '宿敌', role_b: '宿敌', category: 'hostile', intimacy: -80, status: 'normal', description: '血仇' },
        status: 'pending',
      }),
    )
    expect(v).toEqual({
      subject: '林远 ↔ 王五',
      field: '新建关系',
      before: '（无）',
      after: '宿敌，敌对，亲密度 -80，正常，血仇',
    })
  })

  it('境界与组织成员', () => {
    expect(
      describeChange(
        change({
          kind: 'career_stage',
          target: { character_id: 1, name: '林远', career_id: 2, career_name: '剑修' },
          before: { stage_id: 3, stage: '第2阶·筑基' },
          after: { stage_id: 4, stage: '第3阶·金丹' },
        }),
      ),
    ).toEqual({ subject: '林远 · 剑修', field: '境界', before: '第2阶·筑基', after: '第3阶·金丹' })
    expect(
      describeChange(
        change({
          kind: 'org_membership',
          target: { organization_id: 1, organization_name: '青云门', character_id: 5, name: '李四' },
          before: null,
          after: { position: '', status: 'active' },
        }),
      ),
    ).toEqual({ subject: '李四 · 青云门', field: '组织成员', before: '非成员', after: '无职位（在任）' })
  })

  it('未匹配条目只有原始名字，照常展示', () => {
    const v = describeChange(
      change({
        kind: 'relationship_update',
        target: { a_name: '路人甲', b_name: '林远' },
        field_key: '',
        before: null,
        after: { intimacy: 10 },
        status: 'skipped',
        reason: '未找到名为「路人甲」的角色',
      }),
    )
    expect(v).toEqual({ subject: '路人甲 ↔ 林远', field: '关系', before: '（无）', after: '亲密度 10' })
    const org = describeChange(
      change({
        kind: 'org_membership',
        target: { name: '李四', organization_name: '天机阁' },
        before: null,
        after: { action: 'join', position: '长老', status: null },
        status: 'skipped',
      }),
    )
    expect(org.after).toBe('加入：长老')
    expect(describeChange(change({ kind: 'career_stage', target: { name: '林远', career_name: '剑修' }, before: null, after: { stage: '金丹' } })).after).toBe('金丹')
  })

  it('分组按固定顺序、计数待确认、替换条目', () => {
    const list = [
      change({ id: 1, kind: 'org_membership' }),
      change({ id: 2, kind: 'character_state', status: 'pending' }),
      change({ id: 3, kind: 'org_membership', status: 'pending' }),
    ]
    const groups = groupChanges(list)
    expect(groups.map((g) => [g.kind, g.items.map((c) => c.id)])).toEqual([
      ['character_state', [2]],
      ['org_membership', [1, 3]],
    ])
    expect(countPending(list)).toBe(2)
    expect(countPending(undefined)).toBe(0)
    expect(replaceChange(list, { ...list[2], status: 'applied' }).map((c) => c.status)).toEqual(['applied', 'pending', 'applied'])
  })

  it('changeCountsLabel', () => {
    expect(changeCountsLabel({ applied: 3, pending: 1, skipped: 0 })).toBe('已写入 3 · 待确认 1')
    expect(changeCountsLabel({})).toBe('')
  })
})

describe('按建议重写请求', () => {
  const base: ReviseFormValues = {
    analysisId: 7,
    suggestionIds: [],
    instruction: '',
    preserve: [],
    targetWords: null,
    aiConfigId: null,
  }

  it('至少选择一条建议或填写要求', () => {
    expect(buildReviseRequest(base)).toEqual({ ok: false, error: '请至少选择一条建议或填写修改要求' })
    expect(buildReviseRequest({ ...base, instruction: '   ' }).ok).toBe(false)
  })

  it('去重、去空白；省略目标字数', () => {
    const r = buildReviseRequest({
      ...base,
      suggestionIds: [2, 1, 2],
      instruction: '  加强悬念 ',
      preserve: ['保持整体结构', ' ', '保持整体结构 ', '保留关键对白'],
      aiConfigId: 3,
    })
    expect(r).toEqual({
      ok: true,
      body: {
        analysis_id: 7,
        suggestion_ids: [2, 1],
        instruction: '加强悬念',
        preserve: ['保持整体结构', '保留关键对白'],
        ai_config_id: 3,
      },
    })
  })

  it('目标字数与数量上限', () => {
    expect(buildReviseRequest({ ...base, suggestionIds: [1], targetWords: 200 })).toEqual({
      ok: false,
      error: '目标字数应在 300–20000 之间',
    })
    const ok = buildReviseRequest({ ...base, suggestionIds: [1], targetWords: 3000 })
    expect(ok.ok && ok.body.target_words).toBe(3000)
    expect(buildReviseRequest({ ...base, suggestionIds: Array.from({ length: 21 }, (_, i) => i + 1) }).ok).toBe(false)
    expect(buildReviseRequest({ ...base, instruction: 'x', preserve: Array.from({ length: 11 }, (_, i) => `p${i}`) }).ok).toBe(false)
    expect(buildReviseRequest({ ...base, instruction: 'x'.repeat(2001) }).ok).toBe(false)
  })
})

describe('reviseCompareFor', () => {
  it('取 TaskOut.analysis_id 为基准，since 取已知最新分析与基准中较大者', () => {
    expect(reviseCompareFor({ kind: 'chapter.revise', analysis_id: 5 }, 8)).toEqual({ baseAnalysisId: 5, sinceAnalysisId: 8 })
    expect(reviseCompareFor({ kind: 'chapter.revise', analysis_id: 5 }, 3)).toEqual({ baseAnalysisId: 5, sinceAnalysisId: 5 })
    expect(reviseCompareFor({ kind: 'chapter.revise', analysis_id: 5 }, null)).toEqual({
      baseAnalysisId: 5,
      sinceAnalysisId: 5,
    })
  })

  it('非重写任务或缺少 analysis_id 时返回 null', () => {
    expect(reviseCompareFor({ kind: 'chapter.generate', analysis_id: null }, 3)).toBeNull()
    expect(reviseCompareFor({ kind: 'chapter.polish', analysis_id: 5 }, 3)).toBeNull()
    expect(reviseCompareFor({ kind: 'chapter.revise', analysis_id: null }, 3)).toBeNull()
  })
})

describe('parseAnalyzeDone', () => {
  it('收窄 chapter.analyze 的 done.result', () => {
    expect(parseAnalyzeDone(null)).toBeNull()
    expect(parseAnalyzeDone({ chapter: {} })).toBeNull()
    expect(
      parseAnalyzeDone({
        analysis_id: 9,
        chapter_id: 3,
        chapter_no: 2,
        summary: '摘要',
        scores: { overall: 7 },
        metrics: {},
        warnings: ['w', 1],
        change_counts: { applied: 2 },
      }),
    ).toEqual({
      analysis_id: 9,
      chapter_id: 3,
      chapter_no: 2,
      summary: '摘要',
      scores: { overall: 7 },
      change_counts: { applied: 2 },
      warnings: ['w'],
    })
  })
})

describe('接口路径', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('分析 / 状态 / 历史 / 条目操作 / 重写', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await analysisApi.analyze(3)
    await analysisApi.analyzePending(2)
    await analysisApi.statuses(2)
    await analysisApi.status(3)
    await analysisApi.list(3)
    await analysisApi.get(9)
    await analysisApi.revert(1)
    await analysisApi.confirm(1)
    await analysisApi.reject(1)
    await reviseChapter(3, { analysis_id: 9, suggestion_ids: [1], instruction: '', preserve: [] })
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.map(([url, init]) => `${init.method ?? 'GET'} ${url}`)).toEqual([
      'POST /api/v1/chapters/3/analyze',
      'POST /api/v1/projects/2/analysis/analyze-pending',
      'GET /api/v1/projects/2/analysis/statuses',
      'GET /api/v1/chapters/3/analysis-status',
      'GET /api/v1/chapters/3/analyses',
      'GET /api/v1/analyses/9',
      'POST /api/v1/analysis-changes/1/revert',
      'POST /api/v1/analysis-changes/1/confirm',
      'POST /api/v1/analysis-changes/1/reject',
      'POST /api/v1/chapters/3/revise',
    ])
  })
})
