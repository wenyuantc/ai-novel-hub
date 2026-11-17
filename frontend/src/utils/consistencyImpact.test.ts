import { describe, expect, it } from 'vitest'
import type { ImpactChapterSummary, ImpactDetail, StaleProgression } from '../api/consistency'
import {
  ASSESS_BLOCK_TEXT, assessGate, changeSides, editorImpactHint, evidenceOverflowNote, impactConclusion, impactEndNotice, impactHref,
  impactPermissions, impactRowNotes, impactTaskChapter, impactUpdateBody, isAcknowledged, isImpactDirty, itemStatus, memoriesHref, NO_IMPACT_EDITS,
  pendingCount, progressionHref, readConsistencyTab, readImpactChapter, readImpactTask, setAcknowledged, setItemStatus, staleLabels, summaryHref,
  uncoveredCheckRange, withImpactLocation,
} from './consistencyImpact'

const row = (patch: Partial<ImpactChapterSummary> = {}): ImpactChapterSummary => ({
  chapter_id: 5, chapter_no: 5, chapter_title: '断剑', stale_summary: false, stale_memories: 0, stale_progressions: 0, stale_count: 0,
  ledger_state: 'fresh', assessed: false, assessed_at: null, task_id: null, outdated: false, no_change: false, changes: 0, items: 0, pending_items: 0,
  changed_items: 0, uncovered: 0, ...patch,
})

const progression = (patch: Partial<StaleProgression> = {}): StaleProgression => ({
  id: 31, timeline_id: 9, subject_kind: 'character', character_id: 4, related_character_id: null, world_setting_id: null, career_id: null,
  organization_id: null, subject_name: '林远', field_key: 'current_state', custom_field_id: null, anchor_label: '第 5 章末', acknowledged: false,
  ...patch,
})

type Source = Parameters<typeof impactUpdateBody>[0]
const source = (patch: Partial<Source> = {}): Source => ({
  revision: 3,
  stale_progressions: [progression({ id: 31 }), progression({ id: 32, acknowledged: true })],
  assessment: { assessed: true, items: [{ chapter_id: 12, status: 'pending' }, { chapter_id: 14, status: 'reviewed' }] },
  ...patch,
})

describe('页签与章节深链', () => {
  it('默认是问题页签；所选章节只在影响页签下有效且必须是正整数', () => {
    expect(readConsistencyTab(new URLSearchParams(''))).toBe('issues')
    expect(readConsistencyTab(new URLSearchParams('tab=other'))).toBe('issues')
    expect(readConsistencyTab(new URLSearchParams('tab=impact'))).toBe('impact')
    expect(readImpactChapter(new URLSearchParams('tab=impact&chapter=7'))).toBe(7)
    expect(readImpactChapter(new URLSearchParams('chapter=7'))).toBeNull()
    for (const bad of ['0', '-1', '1.5', 'abc', '']) expect(readImpactChapter(new URLSearchParams(`tab=impact&chapter=${bad}`))).toBeNull()
  })
  it('地址：影响页签与可选的章节；切换时保留其他参数、回到问题页签清掉章节', () => {
    expect(impactHref(2)).toBe('/projects/2/consistency?tab=impact')
    expect(impactHref(2, 7)).toBe('/projects/2/consistency?tab=impact&chapter=7')
    expect(impactHref(2, 0)).toBe('/projects/2/consistency?tab=impact')
    const base = new URLSearchParams('keep=1&tab=impact&chapter=7')
    expect(withImpactLocation(base, 'impact', 9).toString()).toBe('keep=1&tab=impact&chapter=9')
    expect(withImpactLocation(base, 'impact').toString()).toBe('keep=1&tab=impact')
    expect(withImpactLocation(base, 'issues', 9).toString()).toBe('keep=1')
    expect(base.get('chapter')).toBe('7')
  })
  it('任务面板跳转：任务 id 只在影响页签下读取，定位或切换后从地址栏去掉', () => {
    expect(readImpactTask(new URLSearchParams('tab=impact&task=41'))).toBe(41)
    expect(readImpactTask(new URLSearchParams('task=41'))).toBeNull()
    for (const bad of ['0', '-3', '4.2', 'x']) expect(readImpactTask(new URLSearchParams(`tab=impact&task=${bad}`))).toBeNull()
    const base = new URLSearchParams('tab=impact&task=41')
    expect(withImpactLocation(base, 'impact', 5).toString()).toBe('tab=impact&chapter=5')
    expect(withImpactLocation(base, 'impact').toString()).toBe('tab=impact')
  })
  it('任务 id → 章节：进行中的评估取接口报告的章节，已结束的取最近一次评估的任务；数据未到时不下结论', () => {
    const chapters = [row({ chapter_id: 5, task_id: 40 }), row({ chapter_id: 8, task_id: 41 }), row({ chapter_id: 9 })]
    const idle = { running_task_id: null, running_chapter_id: null }
    expect(impactTaskChapter(41, idle, chapters)).toBe(8)
    expect(impactTaskChapter(77, { running_task_id: 77, running_chapter_id: 9 }, undefined)).toBe(9)
    // 概览还没读到：稍后再判断，不能当成找不到
    expect(impactTaskChapter(41, idle, undefined)).toBeUndefined()
    expect(impactTaskChapter(41, undefined, undefined)).toBeUndefined()
    // 评估未落库即失败、已被新的评估覆盖或任务已清理：找不到
    expect(impactTaskChapter(99, idle, chapters)).toBeNull()
    // 进行中的是别的任务：不借用它的章节
    expect(impactTaskChapter(99, { running_task_id: 77, running_chapter_id: 9 }, chapters)).toBeNull()
  })
})

describe('列表行的结论与计数', () => {
  it('最近评估结论：未评估／未发现剧情事实变化／N 处事实变化', () => {
    expect(impactConclusion(row()).label).toBe('未评估')
    expect(impactConclusion(row({ assessed: true, no_change: true })).label).toBe('未发现剧情事实变化')
    expect(impactConclusion(row({ assessed: true, changes: 3 })).label).toBe('3 处事实变化')
    // 未评估优先：只保存过第一级确认的记录不显示结论
    expect(impactConclusion(row({ assessed: false, changes: 3 })).label).toBe('未评估')
  })
  it('过期计数只列出大于 0 的类别', () => {
    expect(staleLabels(row())).toEqual([])
    expect(staleLabels(row({ stale_summary: true, stale_memories: 2, stale_progressions: 1 }))).toEqual([
      '章摘要已过期', '剧情记忆 2 条已过期', '设定变化 1 条待确认',
    ])
  })
  it('评估附注：再次修改、待复核项数、被列章节已修改、无法判断；未评估没有附注', () => {
    expect(impactRowNotes(row({ outdated: true, items: 2, pending_items: 2 }))).toEqual([])
    expect(impactRowNotes(row({ assessed: true, outdated: true, items: 3, pending_items: 2, changed_items: 1, uncovered: 4 }))).toEqual([
      '评估后正文又有修改', '可能受影响 3 章，待复核 2 项', '1 个受影响章节在评估后已修改', '4 章无法判断',
    ])
    expect(impactRowNotes(row({ assessed: true, items: 2, pending_items: 0 }))).toEqual(['可能受影响 2 章，均已处理'])
    expect(impactRowNotes(row({ assessed: true, no_change: true }))).toEqual([])
  })
  it('事实变化的前后对照与证据条数说明', () => {
    expect(changeSides({ kind: 'added', before: '', after: '佩剑已毁' })).toEqual({ before: null, after: '佩剑已毁' })
    expect(changeSides({ kind: 'removed', before: '佩剑完好', after: '' })).toEqual({ before: '佩剑完好', after: null })
    expect(changeSides({ kind: 'changed', before: '佩剑完好', after: '佩剑已毁' })).toEqual({ before: '佩剑完好', after: '佩剑已毁' })
    const evidence = [{ fact_id: '1', quote: '', start: null, end: null, text_hash: '' }]
    expect(evidenceOverflowNote({ fact_count: 1, evidence })).toBe('')
    expect(evidenceOverflowNote({ fact_count: 8, evidence })).toBe('该章共有 8 条相关事实，这里只展示前 1 条')
  })
})

describe('派生内容的跳转', () => {
  it('章摘要到本章分析抽屉，剧情记忆到记忆页', () => {
    expect(summaryHref(2, 5)).toBe('/projects/2/chapters/5?panel=analysis')
    expect(memoriesHref(2)).toBe('/projects/2/memories')
  })
  it('设定变化按主体类型到对应详情；四类条目带本章章末位置，关系按时间线，主体缺失退到所在页面', () => {
    const position = 'view=effective&chapter_id=5&boundary=end'
    expect(progressionHref(2, 5, progression())).toBe(`/projects/2/characters?id=4&${position}`)
    expect(progressionHref(2, 5, progression({ subject_kind: 'world', character_id: null, world_setting_id: 8 }))).toBe(`/projects/2/world?id=8&${position}`)
    expect(progressionHref(2, 5, progression({ subject_kind: 'organization', character_id: null, organization_id: 6 }))).toBe(`/projects/2/organizations?id=6&${position}`)
    expect(progressionHref(2, 5, progression({ subject_kind: 'career_entry', character_id: null, career_id: 3 }))).toBe(`/projects/2/careers?id=3&${position}`)
    expect(progressionHref(2, 5, progression({ subject_kind: 'relationship', related_character_id: 7 }))).toBe('/projects/2/relationships?timeline_id=9')
    expect(progressionHref(2, 5, progression({ subject_kind: 'career', career_id: 3 }))).toBe('/projects/2/characters?id=4')
    expect(progressionHref(2, 5, progression({ subject_kind: 'membership', organization_id: 6 }))).toBe('/projects/2/characters?id=4')
    expect(progressionHref(2, 5, progression({ character_id: null }))).toBe('/projects/2/characters')
    expect(progressionHref(2, 5, progression({ subject_kind: 'unknown' }))).toBe('/projects/2/characters')
  })
})

describe('评估按钮的禁用原因', () => {
  const free = { checkBusy: false, impactBusy: false, hasConfig: true }
  it('assess_block 映射到提示；只有 no_baseline 引导先运行检查', () => {
    for (const block of ['no_baseline', 'unchanged', 'empty'] as const) {
      expect(assessGate({ detail: { can_assess: false, assess_block: block }, ...free })).toEqual({
        reason: ASSESS_BLOCK_TEXT[block], needsCheck: block === 'no_baseline',
      })
    }
    expect(ASSESS_BLOCK_TEXT.no_baseline).toContain('先运行一次一致性检查')
    expect(ASSESS_BLOCK_TEXT.unchanged).toContain('没有改动')
    expect(ASSESS_BLOCK_TEXT.empty).toContain('正文为空')
  })
  it('可评估时依次受检查、评估互斥与模型配置限制；章节自身的原因优先', () => {
    const detail = { can_assess: true, assess_block: null }
    expect(assessGate({ detail, ...free })).toEqual({ reason: '', needsCheck: false })
    expect(assessGate({ detail, ...free, hasConfig: null }).reason).toBe('')
    expect(assessGate({ detail, ...free, checkBusy: true, impactBusy: true }).reason).toContain('一致性检查')
    expect(assessGate({ detail, ...free, impactBusy: true }).reason).toContain('修改影响评估')
    expect(assessGate({ detail, ...free, hasConfig: false }).reason).toContain('文本模型')
    expect(assessGate({ detail: { can_assess: false, assess_block: 'empty' }, ...free, checkBusy: true }).reason).toBe(ASSESS_BLOCK_TEXT.empty)
    expect(assessGate({ detail: { can_assess: false, assess_block: null }, ...free }).reason).not.toBe('')
  })
})

describe('确认状态：请求体与冲突合并', () => {
  it('没有改动（含改回原值）时不提交', () => {
    expect(impactUpdateBody(source(), NO_IMPACT_EDITS)).toBeNull()
    const back = setAcknowledged(setItemStatus(NO_IMPACT_EDITS, 12, 'pending'), 32, true)
    expect(impactUpdateBody(source(), back)).toBeNull()
    expect(isImpactDirty(source(), back)).toBe(false)
    expect(isImpactDirty(null, setItemStatus(NO_IMPACT_EDITS, 12, 'reviewed'))).toBe(false)
  })
  it('只提交改过的影响项；已确认有改动时整体提交当前已确认的 id', () => {
    const edits = setItemStatus(NO_IMPACT_EDITS, 12, 'dismissed')
    expect(impactUpdateBody(source(), edits)).toEqual({ revision: 3, items: [{ chapter_id: 12, status: 'dismissed' }] })
    expect(impactUpdateBody(source(), setAcknowledged(NO_IMPACT_EDITS, 31, true))).toEqual({ revision: 3, acknowledged: [31, 32] })
    expect(impactUpdateBody(source(), setAcknowledged(edits, 32, false))).toEqual({
      revision: 3, items: [{ chapter_id: 12, status: 'dismissed' }], acknowledged: [],
    })
    expect(pendingCount(source(), edits)).toBe(0)
    expect(pendingCount(source(), NO_IMPACT_EDITS)).toBe(1)
  })
  it('尚无记录（revision 0、未评估）只能保存已确认，不带影响项', () => {
    const fresh = source({ revision: 0, assessment: null })
    const edits = setAcknowledged(setItemStatus(NO_IMPACT_EDITS, 12, 'reviewed'), 31, true)
    expect(impactUpdateBody(fresh, edits)).toEqual({ revision: 0, acknowledged: [31, 32] })
    // 只保存过第一级确认的记录：assessed = false 时同样不提交影响项
    const unassessed = source({ assessment: { assessed: false, items: [{ chapter_id: 12, status: 'pending' }] } })
    expect(impactUpdateBody(unassessed, setItemStatus(NO_IMPACT_EDITS, 12, 'reviewed'))).toBeNull()
  })
  it('冲突后读到最新详情：本地勾选保留并以最新修订提交，没改过的跟随服务端，已不存在的条目不提交', () => {
    const edits = setAcknowledged(setItemStatus(setItemStatus(NO_IMPACT_EDITS, 12, 'reviewed'), 99, 'dismissed'), 31, true)
    const latest = source({
      revision: 8,
      // 第 14 章被别人改成无需处理；第 16 章是新评估带来的；设定变化 32 已不再过期
      stale_progressions: [progression({ id: 31 })],
      assessment: { assessed: true, items: [{ chapter_id: 12, status: 'pending' }, { chapter_id: 14, status: 'dismissed' }, { chapter_id: 16, status: 'pending' }] },
    })
    expect(itemStatus({ chapter_id: 12, status: 'pending' }, edits)).toBe('reviewed')
    expect(itemStatus({ chapter_id: 14, status: 'dismissed' }, edits)).toBe('dismissed')
    expect(isAcknowledged(progression({ id: 31 }), edits)).toBe(true)
    expect(isImpactDirty(latest, edits)).toBe(true)
    expect(impactUpdateBody(latest, edits)).toEqual({ revision: 8, items: [{ chapter_id: 12, status: 'reviewed' }], acknowledged: [31] })
    // 别人已做了同样的选择：本地勾选等于没改
    const same = source({ revision: 9, stale_progressions: [progression({ id: 31, acknowledged: true })], assessment: { assessed: true, items: [{ chapter_id: 12, status: 'reviewed' }] } })
    expect(impactUpdateBody(same, edits)).toBeNull()
  })
})

describe('只读显隐', () => {
  it('查看者只能看；编辑者可确认；有 AI 能力才能评估与发起检查；取消只给发起者本人', () => {
    expect(impactPermissions({ canRead: true, canEdit: false, canUseAI: false }, true)).toEqual({
      canView: true, canConfirm: false, canAssess: false, canStartCheck: false, canCancelAssess: false,
    })
    const editor = { canRead: true, canEdit: true, canUseAI: true }
    expect(impactPermissions(editor)).toEqual({ canView: true, canConfirm: true, canAssess: true, canStartCheck: true, canCancelAssess: false })
    expect(impactPermissions(editor, true).canCancelAssess).toBe(true)
    expect(impactPermissions({ canRead: true, canEdit: true, canUseAI: false }, true)).toMatchObject({ canConfirm: true, canAssess: false, canCancelAssess: false })
    expect(Object.values(impactPermissions({ canRead: false, canEdit: true, canUseAI: true }, true)).every((value) => value === false)).toBe(true)
  })
})

describe('快捷检查范围', () => {
  it('取现存章节中章号最小与最大的两章；与给出顺序无关', () => {
    expect(uncoveredCheckRange([
      { chapter_id: 30, chapter_no: 14, chapter_title: '' }, { chapter_id: 11, chapter_no: 9, chapter_title: '' },
      { chapter_id: 25, chapter_no: 12, chapter_title: '' },
    ])).toEqual({ startChapterId: 11, endChapterId: 30 })
    expect(uncoveredCheckRange([{ chapter_id: 11, chapter_no: 9, chapter_title: '' }])).toEqual({ startChapterId: 11, endChapterId: 11 })
  })
  it('已删除的章节不参与；全部已删除或为空时没有范围', () => {
    expect(uncoveredCheckRange([
      { chapter_id: 40, chapter_no: null, chapter_title: null }, { chapter_id: 25, chapter_no: 12, chapter_title: '' },
    ])).toEqual({ startChapterId: 25, endChapterId: 25 })
    expect(uncoveredCheckRange([{ chapter_id: 40, chapter_no: null, chapter_title: null }])).toBeNull()
    expect(uncoveredCheckRange([])).toBeNull()
  })
})

describe('编辑页提示与任务结束', () => {
  const detail = (patch: Partial<Pick<ImpactDetail, 'chapter_id' | 'stale_count'>> = {}) => ({ chapter_id: 5, stale_count: 3, ...patch })
  it('只在本章有过期派生内容时显示', () => {
    expect(editorImpactHint(detail(), 5)).toBe('本章改动后有 3 项派生内容可能过期')
    expect(editorImpactHint(detail({ stale_count: 0 }), 5)).toBeNull()
    expect(editorImpactHint(detail(), 6)).toBeNull()
    expect(editorImpactHint(null, 5)).toBeNull()
    expect(editorImpactHint(undefined, 5)).toBeNull()
  })
  it('评估任务结束提示；进行中不当作已结束', () => {
    for (const status of ['queued', 'running', 'paused'] as const) expect(impactEndNotice(status)).toBeNull()
    expect(impactEndNotice('succeeded')).toEqual({ level: 'success', text: '修改影响评估已完成' })
    expect(impactEndNotice('cancelled')?.level).toBe('info')
    expect(impactEndNotice('failed', '模型调用失败')).toEqual({ level: 'error', text: '模型调用失败' })
    expect(impactEndNotice('interrupted')).toEqual({ level: 'error', text: '修改影响评估未完成' })
  })
})
