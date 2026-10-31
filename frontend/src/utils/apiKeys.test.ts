import { describe, expect, it } from 'vitest'
import { buildApiKeyInput, buildApiKeyUpdate, hasApiKeyScopeChanged } from './apiKeys'

const now = Date.parse('2026-10-30T02:00:00Z')

describe('密钥配置表单', () => {
  it('指定小说：去除名称空白和重复选择，按 UTC 计算默认 90 天有效期', () => {
    expect(buildApiKeyInput({ name: '  阅读器  ', all_projects: false, project_ids: [3, 3, 8], expiry: '90' }, now)).toEqual({
      name: '阅读器', all_projects: false, project_ids: [3, 8], expires_at: '2027-01-28T02:00:00.000Z',
    })
  })

  it('全部小说不携带残留选择，且明确发送不过期', () => {
    expect(buildApiKeyInput({ name: '同步工具', all_projects: true, project_ids: [3], expiry: 'never' }, now)).toEqual({
      name: '同步工具', all_projects: true, project_ids: [], expires_at: null,
    })
  })

  it('编辑保持当前有效期时省略 expires_at，不把已过期 Key 意外延长或改成永久', () => {
    const body = buildApiKeyInput({ name: '改名', all_projects: false, project_ids: [3], expiry: 'keep' }, now)
    expect(body).not.toHaveProperty('expires_at')
  })

  it('自定义时间按带时区的 ISO 发送', () => {
    expect(buildApiKeyInput({ name: '工具', all_projects: true, project_ids: [], expiry: 'custom', custom_expires_at: '2026-11-01T10:00:00+08:00' }, now).expires_at)
      .toBe('2026-11-01T02:00:00.000Z')
  })

  it('拒绝未选小说、自定义空时间和已到期时间', () => {
    expect(() => buildApiKeyInput({ name: '工具', all_projects: false, project_ids: [], expiry: '90' }, now)).toThrow('至少选择一部小说')
    expect(() => buildApiKeyInput({ name: '工具', all_projects: true, project_ids: [], expiry: 'custom' }, now)).toThrow('请选择到期时间')
    expect(() => buildApiKeyInput({ name: '工具', all_projects: true, project_ids: [], expiry: 'custom', custom_expires_at: '2026-10-30T02:00:00Z' }, now)).toThrow('到期时间必须晚于当前时间')
  })

  it('仅授权回收站作品而投影为空时，可改名或有效期且不重写隐藏授权', () => {
    const original = { all_projects: false, project_ids: [] }
    expect(buildApiKeyUpdate({ name: '改名', ...original, expiry: 'keep' }, original, now)).toEqual({ name: '改名' })
    expect(buildApiKeyUpdate({ name: '改名', ...original, expiry: 'never' }, original, now)).toEqual({ name: '改名', expires_at: null })
    expect(hasApiKeyScopeChanged(original, original)).toBe(false)
  })

  it('混合授权仅改名不重写范围，重排或重复可见ID不算范围变化', () => {
    const original = { all_projects: false, project_ids: [3, 8] }
    const values = { name: '改名', all_projects: false, project_ids: [8, 3, 8], expiry: 'keep' as const }
    expect(hasApiKeyScopeChanged(values, original)).toBe(false)
    expect(buildApiKeyUpdate(values, original, now)).toEqual({ name: '改名' })
  })

  it('明确调整范围仍完整替换，编辑和创建都拒绝新的空指定范围', () => {
    const original = { all_projects: false, project_ids: [3, 8] }
    const values = { name: '工具', all_projects: false, project_ids: [9, 9], expiry: 'keep' as const }
    expect(buildApiKeyUpdate(values, original, now)).toEqual({ name: '工具', all_projects: false, project_ids: [9] })
    expect(buildApiKeyUpdate({ ...values, all_projects: true }, original, now)).toEqual({ name: '工具', all_projects: true, project_ids: [] })
    expect(() => buildApiKeyUpdate({ ...values, project_ids: [] }, original, now)).toThrow('至少选择一部小说')
    expect(() => buildApiKeyInput({ ...values, project_ids: [] }, now)).toThrow('至少选择一部小说')
  })

  it('全部作品模式忽略残留ID，仅改名不会产生范围更新', () => {
    const original = { all_projects: true, project_ids: [] }
    expect(buildApiKeyUpdate({ name: '工具', all_projects: true, project_ids: [9], expiry: 'keep' }, original, now)).toEqual({ name: '工具' })
    expect(() => buildApiKeyUpdate({ name: '工具', all_projects: false, project_ids: [], expiry: 'keep' }, original, now)).toThrow('至少选择一部小说')
  })
})
