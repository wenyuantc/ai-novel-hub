import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuditCleanupPreview, AuditSummary } from '../api/types'
import { auditDateRange, auditFilterQuery, auditRangeLabel, auditSummaryRows, auditTargetLabel, auditValidPage, cleanupPreviewUsable, createAuditRequestGate } from './auditLogs'

const range = { from_at: '2026-10-29T16:00:00.000Z', to_at: '2026-10-31T16:00:00.000Z' }
const preview: AuditCleanupPreview = { ...range, count: 3, protected_count: 2, expires_at: '2026-10-31T01:10:00Z', preview_token: 'secret-preview' }
const now = Date.parse('2026-10-31T01:00:00Z')
afterEach(() => vi.unstubAllEnvs())

describe('审计日期与筛选', () => {
  it('本地起始日零点到结束日次日零点，转成 UTC 半开区间', () => {
    vi.stubEnv('TZ', 'Asia/Shanghai')
    expect(auditDateRange('2026-10-30', '2026-10-31')).toEqual(range)
    expect(auditRangeLabel(range)).toContain('2026-10-30 00:00 UTC+08:00（含）')
    expect(auditRangeLabel(range)).toContain('2026-11-01 00:00 UTC+08:00（不含）')
    expect(auditDateRange('2026-10-30', '2026-10-30').to_at).toBe('2026-10-30T16:00:00.000Z')
  })
  it('夏令时转换日不假定一天恒为 24 小时', () => {
    vi.stubEnv('TZ', 'America/New_York')
    expect(auditDateRange('2026-03-08', '2026-03-08')).toEqual({ from_at: '2026-03-08T05:00:00.000Z', to_at: '2026-03-09T04:00:00.000Z' })
    expect(auditDateRange('2026-11-01', '2026-11-01')).toEqual({ from_at: '2026-11-01T04:00:00.000Z', to_at: '2026-11-02T05:00:00.000Z' })
  })
  it.each([['', '2026-10-31'], ['2026-02-30', '2026-03-01'], ['2026-10-31', '2026-10-30'], ['2026-1-01', '2026-01-02']])('拒绝空日期、越界日和反向范围：%s/%s', (from, to) => {
    expect(() => auditDateRange(from, to)).toThrow()
  })
  it('组合筛选只正规化日期和用户名；未成对日期不发请求', () => {
    vi.stubEnv('TZ', 'Asia/Shanghai')
    expect(auditFilterQuery({ actor_q: ' 历史用户 ', actor_user_id: 7, result: 'failure', target_type: 'chapter', target_id: 3, fromDate: '2026-10-30', toDate: '2026-10-31' })).toEqual({ actor_q: '历史用户', actor_user_id: 7, result: 'failure', target_type: 'chapter', target_id: 3, ...range })
    expect(auditFilterQuery({ actor_q: '  ' })).toEqual({ actor_q: undefined })
    expect(() => auditFilterQuery({ fromDate: '2026-10-30' })).toThrow('请选择完整、有效的起止日期')
  })
  it('清理后回到有效页，空列表仍为第 1 页', () => {
    expect(auditValidPage(5, 21, 20)).toBe(2)
    expect(auditValidPage(5, 20, 20)).toBe(1)
    expect(auditValidPage(2, 0, 20)).toBe(1)
    expect(auditValidPage(2, 100, 20)).toBe(2)
  })
})

describe('清理确认与过期异步隔离', () => {
  it('有效、同范围、非空预览才可确认，不比较预计数与实际数', () => {
    expect(cleanupPreviewUsable(preview, range, now)).toBe(true)
    expect(cleanupPreviewUsable({ ...preview, from_at: '2026-10-30T00:00:00+08:00' }, range, now)).toBe(true)
    expect(cleanupPreviewUsable(preview, { ...range, to_at: '2026-11-01T16:00:00Z' }, now)).toBe(false)
    for (const invalid of [null, { ...preview, count: 0 }, { ...preview, preview_token: '' }, { ...preview, expires_at: 'bad-time' }]) expect(cleanupPreviewUsable(invalid, range, now)).toBe(false)
    expect(cleanupPreviewUsable(preview, range, Date.parse(preview.expires_at))).toBe(false)
  })
  it('撤销旧请求后晚到响应不能复活确认，后续请求仍可生效', async () => {
    const gate = createAuditRequestGate()
    let finish!: () => void
    const late = new Promise<void>((resolve) => { finish = resolve })
    const ticket = gate.begin()
    let visible: AuditCleanupPreview | null = null
    const request = late.then(() => { if (gate.isCurrent(ticket)) visible = preview })
    gate.invalidate()
    const nextTicket = gate.begin()
    finish()
    await request
    expect(visible).toBeNull()
    expect(gate.isCurrent(nextTicket)).toBe(true)
    gate.invalidate()
    expect(gate.isCurrent(nextTicket)).toBe(false)
  })
})

describe('只读审计摘要', () => {
  it('已删除目标仍由快照标识；创建失败允许没有目标 ID', () => {
    expect(auditTargetLabel({ target_type: 'chapter', target_id: 8, target_label: '旧标题' })).toBe('章节 #8 · 旧标题')
    expect(auditTargetLabel({ target_type: 'user', target_id: null, target_label: null })).toBe('用户')
  })
  it('仅投影字段、状态、字数和有界 ID，不承诺全文快照', () => {
    const summary: AuditSummary = {
      changed_fields: ['content', 'starred', 'note'], changes: [{ field: 'starred', before: false, after: true }],
      word_count_before: 10, word_count_after: 0, body_chars_before: null, body_chars_after: null, reference_count_before: null, reference_count_after: null,
      affected_count: 101, affected_ids: [1, 2], ids_truncated: true, chapter_count: null, version_count: 2, from_at: null, to_at: null, expected_count: null, deleted_count: null, high_water_id: null,
    }
    expect(auditSummaryRows(summary)).toEqual([
      { label: '变更字段', value: '正文内容、标星、版本备注' }, { label: '标星', value: '否 → 是' },
      { label: '正文字数', value: '10 → 0' }, { label: '受影响数量', value: '101' }, { label: '版本数', value: '2' }, { label: '受影响 ID', value: '1、2（仅展示有界列表，其余省略）' },
    ])
  })
})
