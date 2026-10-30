import { describe, expect, it } from 'vitest'
import { buildApiKeyInput } from './apiKeys'

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
})
