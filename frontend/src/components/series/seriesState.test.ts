import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { SeriesEntry } from '../../api/series'
import {
  bookCharacterPatch, diffSeriesValues, inSettingScope, projectReference,
  rebaseSeriesAi, SERIES_AI_DEFAULTS, seriesNeedsReview,
} from './seriesState'

const sharedEntry: SeriesEntry = {
  kind: 'world', id: 21, data: { title: '白塔', aliases: [], category: 'place', content: '主世界白塔' },
  ai_settings: SERIES_AI_DEFAULTS, uses: [{ project_id: 4, ref: { kind: 'world', id: 123 } }],
}

describe('共享设定的作品边界', () => {
  it('从当前书的采用记录解析实体 ID，不能将系列条目 ID 当本书引用', () => {
    expect(projectReference(sharedEntry, 4)).toEqual({ kind: 'world', id: 123 })
    expect(projectReference(sharedEntry, 5)).toBeUndefined()
    expect(projectReference({ ...sharedEntry, uses: [{ project_id: 4, ref: { kind: 'character', id: 123 } }] }, 4)).toBeUndefined()
    expect(inSettingScope(undefined, 'book')).toBe(true)
    expect(inSettingScope({ scope: 'series' }, 'book')).toBe(false)
    expect(inSettingScope({ scope: 'series' }, 'series')).toBe(true)
    expect(inSettingScope({ scope: 'series' }, 'all')).toBe(true)
  })

  it('本书角色编辑不带上系列基础，即使页面有促升前的旧草稿', () => {
    expect(bookCharacterPatch({ name: '草稿姓名', aliases: ['错误草稿'], profile: '草稿简介', age: '20', current_state: '章初情况', sort: 3 }))
      .toEqual({ age: '20', current_state: '章初情况', sort: 3 })
  })

  it('读取冲突后合并确实改动的字段，不放开并发新增的 AI 隐藏', () => {
    const base = { ...SERIES_AI_DEFAULTS, ai_hidden_builtin_fields: ['profile'], mention_exclusions: ['塔门外'] }
    const draft = { ...base, ai_hidden_builtin_fields: [], mention_exclusions: ['星河边'] }
    const latest = { ...base, ai_mode: 'never' as const, ai_hidden_builtin_fields: ['profile', 'background'], mention_exclusions: ['塔门外', '东门前'] }
    const rebased = rebaseSeriesAi(base, draft, latest)
    expect(rebased).toMatchObject({ ai_mode: 'never', ai_hidden_builtin_fields: ['background'], mention_exclusions: ['东门前', '星河边'] })
    expect(diffSeriesValues(latest, rebased)).not.toHaveProperty('ai_mode')
  })

  it('来源改变和未知写入结果要求作者重新核对', () => {
    expect(seriesNeedsReview(new ApiError(409, '来源已变更', { code: 'series_source_changed' }))).toBe(true)
    expect(seriesNeedsReview(new ApiError(409, '版本不匹配', { code: 'stale_revision' }))).toBe(true)
    expect(seriesNeedsReview(new ApiError(0, '断网'))).toBe(true)
    expect(seriesNeedsReview(new ApiError(409, '仍在使用', { code: 'series_setting_in_use' }))).toBe(false)
  })
})
