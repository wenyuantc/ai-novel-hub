import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import type { ConsistencyIssue, IssueEvidence, RunStats } from '../api/consistency'
import {
  checkEndNotice, checkScopeBody, consistencyPermissions, createRequestGate, draftFromIssue, EVIDENCE_DELETED, EVIDENCE_STALE, EVIDENCE_UNANCHORED,
  evidenceChapterLabel, evidenceLink, hasIssueFilter, isCheckConflict, isDraftDirty, isStaleRevision, ISSUE_PAGE_SIZE,
  issueChaptersLabel, issueListQuery, issueUpdateBody, rebaseDraft, runNotices, runScopeLabel, settleCurrent,
  streamWatchAction,
} from './consistency'
import { readMentionAnchor, staleAnchorMessage } from './mentions'

const HASH = 'a'.repeat(64)
const evidence = (patch: Partial<IssueEvidence> = {}): IssueEvidence => ({
  chapter_id: 7, chapter_no: 5, chapter_title: '断剑', fact_id: '3', quote: '佩剑断为两截', start: 12, end: 18, text_hash: HASH, stale: false, ...patch,
})
const issue = (patch: Partial<ConsistencyIssue> = {}): ConsistencyIssue => ({
  id: 9, project_id: 2, category: 'item', title: '佩剑', description: '', suggestion: '', severity: 'high', status: 'open', note: '',
  status_changed_by: null, status_changed_at: null, revision: 3, evidence: [evidence()], reappeared: false, needs_recheck: false,
  not_reproduced: false, created_at: '', updated_at: '', ...patch,
})
const stats = (patch: Partial<RunStats> = {}): RunStats => ({
  total: 0, extracted: 0, reused: 0, skipped: 0, failed: 0, created: 0, updated: 0, reappeared: 0, not_reproduced: 0, stale_outside: 0,
  batches: 0, failed_batches: 0, skipped_chapters: [], warnings: [], ...patch,
})

describe('证据跳转', () => {
  it('有偏移且未失效：阅读器与编辑器使用提及锚点的同一组参数', () => {
    const link = evidenceLink(2, evidence())
    expect(link).toEqual({
      kind: 'anchored',
      readerHref: `/projects/2/read/7?mention_start=12&mention_end=18&mention_hash=${HASH}&mention_from=consistency`,
      editorHref: `/projects/2/chapters/7?mention_start=12&mention_end=18&mention_hash=${HASH}&mention_from=consistency`,
      notes: [],
    })
    if (link.kind !== 'anchored') throw new Error('应为带锚点的链接')
    // 目标页的既有解析能读回同一锚点
    for (const href of [link.readerHref, link.editorHref]) {
      const anchor = readMentionAnchor(new URL(href, 'https://example.test').searchParams)
      expect(anchor).toEqual({ start: 12, end: 18, content_hash: HASH, origin: 'consistency' })
      // 位置失效时目标页不提示「回提及列表」
      expect(staleAnchorMessage(anchor, '请回提及列表')).toBe('原文已变化，旧位置已失效，请回到来源页面重新定位。')
    }
  })

  it('无偏移：跳到该章开头并标注未能逐字定位', () => {
    expect(evidenceLink(2, evidence({ start: null, end: null }))).toEqual({
      kind: 'chapter_start', readerHref: '/projects/2/read/7', editorHref: '/projects/2/chapters/7', notes: [EVIDENCE_UNANCHORED],
    })
    // 指纹不合法或区间为空时不生成会被目标页拒绝的锚点
    expect(evidenceLink(2, evidence({ text_hash: 'bad' })).kind).toBe('chapter_start')
    expect(evidenceLink(2, evidence({ start: 5, end: 5 })).kind).toBe('chapter_start')
    expect(evidenceLink(2, evidence({ start: null, end: null, stale: true })).notes).toEqual([EVIDENCE_UNANCHORED, EVIDENCE_STALE])
  })

  it('正文已修改：仍带原锚点跳转并标注位置可能失效，由目标页按指纹提示', () => {
    const link = evidenceLink(2, evidence({ stale: true }))
    expect(link.kind).toBe('anchored')
    expect(link.notes).toEqual([EVIDENCE_STALE])
    expect(link.kind === 'anchored' && link.readerHref).toContain(`mention_hash=${HASH}`)
  })

  it('章节已删除：只给说明，不给链接', () => {
    for (const gone of [evidence({ chapter_no: null, chapter_title: null, stale: true }), evidence({ chapter_id: null, chapter_no: null, chapter_title: null, stale: true })]) {
      const link = evidenceLink(2, gone)
      expect(link).toEqual({ kind: 'deleted', notes: [EVIDENCE_DELETED] })
      expect('readerHref' in link).toBe(false)
      expect(evidenceChapterLabel(gone)).toBe(EVIDENCE_DELETED)
    }
  })

  it('章节标签与涉及章节：去重、按章号、已删除合并', () => {
    expect(evidenceChapterLabel(evidence())).toBe('第 5 章 · 断剑')
    expect(evidenceChapterLabel(evidence({ chapter_title: '' }))).toBe('第 5 章')
    expect(issueChaptersLabel(issue({ evidence: [
      evidence({ chapter_no: 12 }), evidence(), evidence({ chapter_no: 12 }), evidence({ chapter_no: null }), evidence({ chapter_id: null, chapter_no: null }),
    ] }))).toBe('第 5 章、第 12 章、章节已删除')
    expect(issueChaptersLabel(issue({ evidence: [] }))).toBe('')
  })
})

describe('筛选参数', () => {
  it('未选条件不出现在请求里，页码与页大小固定给出', () => {
    expect(issueListQuery({}, 1)).toEqual({ page: 1, page_size: ISSUE_PAGE_SIZE })
    expect(hasIssueFilter({})).toBe(false)
  })

  it('状态、类别、章节与需重新核查映射到后端参数名', () => {
    expect(issueListQuery({ status: 'resolved', category: 'timeline', chapterId: 8, recheck: 'yes' }, 3)).toEqual({
      status: 'resolved', category: 'timeline', chapter_id: 8, needs_recheck: true, page: 3, page_size: ISSUE_PAGE_SIZE,
    })
    // 「无需重新核查」必须显式传 false，而不是省略
    expect(issueListQuery({ recheck: 'no' }, 1).needs_recheck).toBe(false)
    expect(hasIssueFilter({ recheck: 'no' })).toBe(true)
  })

  it('非法章节与页码被收敛', () => {
    expect(issueListQuery({ chapterId: 0 }, 0)).toEqual({ page: 1, page_size: ISSUE_PAGE_SIZE })
    expect(issueListQuery({ chapterId: 1.5 }, Number.NaN).page).toBe(1)
  })
})

describe('范围选择到请求体', () => {
  const chapters = [{ id: 11, chapter_no: 1 }, { id: 15, chapter_no: 2 }, { id: 12, chapter_no: 3 }]

  it('全书检查不带起止章节，即使表单里残留了选择', () => {
    expect(checkScopeBody({ mode: 'all', startChapterId: 11, endChapterId: 12 }, chapters)).toEqual({ body: { mode: 'all' } })
  })

  it('按范围：起止齐备且按章号（而非 id）比较先后', () => {
    expect(checkScopeBody({ mode: 'range', startChapterId: 15, endChapterId: 12 }, chapters)).toEqual({
      body: { mode: 'range', start_chapter_id: 15, end_chapter_id: 12 },
    })
    expect(checkScopeBody({ mode: 'range', startChapterId: 12, endChapterId: 12 }, chapters)).toEqual({
      body: { mode: 'range', start_chapter_id: 12, end_chapter_id: 12 },
    })
    expect(checkScopeBody({ mode: 'range', startChapterId: 12, endChapterId: 15 }, chapters)).toEqual({ error: '起始章节不能晚于结束章节' })
  })

  it('缺少起止或章节已不存在时不生成请求', () => {
    expect(checkScopeBody({ mode: 'range', startChapterId: 11, endChapterId: null }, chapters)).toEqual({ error: '请选择起止章节' })
    expect(checkScopeBody({ mode: 'range' }, chapters)).toEqual({ error: '请选择起止章节' })
    expect(checkScopeBody({ mode: 'range', startChapterId: 11, endChapterId: 99 }, chapters)).toEqual({ error: '所选章节已不存在，请重新选择' })
  })

  it('最近一次检查的范围文案', () => {
    const scope = { mode: 'range' as const, start_chapter_id: 11, end_chapter_id: 12, issue_id: null, chapter_ids: [11, 15, 12] }
    expect(runScopeLabel({ ...scope, mode: 'all' }, chapters)).toBe('全书')
    expect(runScopeLabel(scope, chapters)).toBe('第 1–3 章')
    expect(runScopeLabel({ ...scope, end_chapter_id: 11 }, chapters)).toBe('第 1 章')
    expect(runScopeLabel({ ...scope, end_chapter_id: 99 }, chapters)).toBe('所选范围（3 章）')
    expect(runScopeLabel({ ...scope, mode: 'issue', issue_id: 4 }, chapters)).toBe('重新核查问题 #4')
  })

  it('概要提示：范围外过期台账、比对批次失败与后端警告', () => {
    expect(runNotices({ stats: stats() })).toEqual([])
    expect(runNotices({ stats: stats({ stale_outside: 2, batches: 5, failed_batches: 1, warnings: ['第 2 批：证据被丢弃'] }) })).toEqual([
      '范围外有 2 章的事实台账已过期，未参与本次比对', '5 批比对中有 1 批失败，本次不标记「本次未复现」', '第 2 批：证据被丢弃',
    ])
  })
})

describe('状态草稿与冲突', () => {
  it('只提交改过的字段；没有改动不发请求', () => {
    const draft = draftFromIssue(issue({ note: '旧备注' }))
    expect(isDraftDirty(draft)).toBe(false)
    expect(issueUpdateBody(draft)).toBeNull()
    expect(issueUpdateBody({ ...draft, status: 'ignored' })).toEqual({ revision: 3, status: 'ignored' })
    // 清空备注传空字符串，不传 null
    expect(issueUpdateBody({ ...draft, note: '' })).toEqual({ revision: 3, note: '' })
    expect(issueUpdateBody({ ...draft, status: 'resolved', note: '已改第 12 章' })).toEqual({ revision: 3, status: 'resolved', note: '已改第 12 章' })
  })

  it('识别修订冲突：只有 409 且 code=stale_revision', () => {
    expect(isStaleRevision(new ApiError(409, '问题已被修改', { detail: '问题已被修改', code: 'stale_revision' }))).toBe(true)
    expect(isStaleRevision(new ApiError(409, '已有进行中的一致性检查', { detail: '' }))).toBe(false)
    expect(isStaleRevision(new ApiError(403, '只读', { code: 'stale_revision' }))).toBe(false)
    expect(isStaleRevision(new Error('x'))).toBe(false)
    expect(isCheckConflict(new ApiError(409, '已有进行中的一致性检查'))).toBe(true)
    expect(isCheckConflict(new ApiError(400, '范围无效'))).toBe(false)
  })

  it('冲突后刷新：保留本地备注草稿，未改的字段跟随服务端，修订号换成最新', () => {
    const edited = { ...draftFromIssue(issue({ note: '旧备注' })), note: '我正在写的备注' }
    // 另一端把状态改成了已忽略并写了别的备注
    const rebased = rebaseDraft(edited, issue({ revision: 4, status: 'ignored', note: '对方的备注' }))
    expect(rebased).toEqual({ issueId: 9, revision: 4, status: 'ignored', note: '我正在写的备注', baseStatus: 'ignored', baseNote: '对方的备注' })
    expect(isDraftDirty(rebased)).toBe(true)
    // 再次保存以最新修订提交，只带本地改过的备注，不会把对方的状态改回去
    expect(issueUpdateBody(rebased)).toEqual({ revision: 4, note: '我正在写的备注' })
  })

  it('本地改过的状态同样保留；没有本地改动时完全取服务端值', () => {
    const edited = { ...draftFromIssue(issue()), status: 'resolved' as const }
    expect(rebaseDraft(edited, issue({ revision: 5, status: 'ignored', note: '新' }))).toMatchObject({ revision: 5, status: 'resolved', note: '新' })
    expect(rebaseDraft(draftFromIssue(issue()), issue({ revision: 5, status: 'ignored', note: '新' }))).toEqual(draftFromIssue(issue({ revision: 5, status: 'ignored', note: '新' })))
  })

  it('不是同一个问题时不沿用草稿', () => {
    const edited = { ...draftFromIssue(issue()), note: '另一个问题的草稿' }
    expect(rebaseDraft(edited, issue({ id: 10, note: '' }))).toEqual(draftFromIssue(issue({ id: 10, note: '' })))
  })
})

describe('只读显隐', () => {
  it('查看者：可看清单，不能改状态、发起检查或重新核查', () => {
    expect(consistencyPermissions({ canRead: true, canEdit: false, canUseAI: false })).toEqual({
      canView: true, canUpdateIssue: false, canStartCheck: false, canRecheck: false, canCancelCheck: false,
    })
  })

  it('编辑者与作者：可改状态并发起；取消只对发起者本人显示', () => {
    const editor = { canRead: true, canEdit: true, canUseAI: true }
    expect(consistencyPermissions(editor)).toEqual({ canView: true, canUpdateIssue: true, canStartCheck: true, canRecheck: true, canCancelCheck: false })
    expect(consistencyPermissions(editor, true).canCancelCheck).toBe(true)
  })

  it('人工代管（可编辑但无 AI 能力）：可改状态，不能发起或取消', () => {
    expect(consistencyPermissions({ canRead: true, canEdit: true, canUseAI: false }, true)).toEqual({
      canView: true, canUpdateIssue: true, canStartCheck: false, canRecheck: false, canCancelCheck: false,
    })
  })

  it('无读取权限时全部关闭', () => {
    expect(Object.values(consistencyPermissions({ canRead: false, canEdit: true, canUseAI: true }, true)).every((value) => value === false)).toBe(true)
  })
})

describe('进度订阅的兜底', () => {
  const watch = (patch: Partial<Parameters<typeof streamWatchAction>[0]> = {}) => streamWatchAction({
    streaming: true, followedTaskId: 8, summaryLoaded: true, runningTaskId: 8, ...patch,
  })
  it('订阅中且概要仍列着该任务：不干预', () => {
    expect(watch()).toBe('none')
  })
  it('订阅流中断或无事件、概要已不再列着该任务（结束或换成别的检查）：核对任务真实状态', () => {
    expect(watch({ runningTaskId: null })).toBe('verify')
    expect(watch({ runningTaskId: 9 })).toBe('verify')
  })
  it('没有订阅、任务未知或概要尚未加载时不核对', () => {
    expect(watch({ streaming: false, runningTaskId: null })).toBe('none')
    expect(watch({ followedTaskId: null, runningTaskId: null })).toBe('none')
    expect(watch({ summaryLoaded: false, runningTaskId: null })).toBe('none')
  })
  it('任务仍在排队、运行或待审核时不当作结束（概要可能只是旧数据）；结束后给出对应提示', () => {
    for (const status of ['queued', 'running', 'paused'] as const) expect(checkEndNotice(status)).toBeNull()
    expect(checkEndNotice('succeeded')).toEqual({ level: 'success', text: '一致性检查已完成' })
    expect(checkEndNotice('cancelled')).toEqual({ level: 'info', text: '检查已取消，已完成章节的事实台账已保留' })
    expect(checkEndNotice('failed', '模型不可用')).toEqual({ level: 'error', text: '模型不可用' })
    expect(checkEndNotice('interrupted')).toEqual({ level: 'error', text: '一致性检查未完成' })
  })
})

describe('迟到响应丢弃', () => {
  it('新的请求开始后，旧请求的结果与错误都不交付', async () => {
    const gate = createRequestGate()
    let resolveOld!: (value: string) => void
    let rejectOlder!: (error: Error) => void
    const older = settleCurrent(gate.begin(), new Promise<string>((_, reject) => { rejectOlder = reject }))
    const old = settleCurrent(gate.begin(), new Promise<string>((resolve) => { resolveOld = resolve }))
    const latest = settleCurrent(gate.begin(), Promise.resolve('新筛选的结果'))
    resolveOld('旧筛选的结果')
    rejectOlder(new Error('旧请求失败'))
    expect(await latest).toEqual({ status: 'ok', value: '新筛选的结果' })
    expect(await old).toEqual({ status: 'stale' })
    expect(await older).toEqual({ status: 'stale' })
  })

  it('切换作品或关闭详情（invalidate）使在途请求失效；当前请求的错误照常交付', async () => {
    const gate = createRequestGate()
    const isCurrent = gate.begin()
    expect(isCurrent()).toBe(true)
    const failure = new Error('网络错误')
    expect(await settleCurrent(isCurrent, Promise.reject(failure))).toEqual({ status: 'error', error: failure })
    const pending = settleCurrent(gate.begin(), Promise.resolve('迟到'))
    gate.invalidate()
    expect(await pending).toEqual({ status: 'stale' })
    expect(isCurrent()).toBe(false)
  })
})
