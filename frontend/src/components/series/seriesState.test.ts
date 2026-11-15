import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { SeriesEntry } from '../../api/series'
import {
  bookCharacterPatch, canChangeSeriesBook, canDeleteSeriesEntry, diffSeriesValues, editableSeriesBooks, inSettingScope, projectReference,
  seriesUsageLabel,
  rebaseSeriesAi, SERIES_AI_DEFAULTS, seriesNeedsReview,
} from './seriesState'

const sharedEntry: SeriesEntry = {
  kind: 'world', id: 21, data: { title: '白塔', aliases: [], category: 'place', content: '主世界白塔' },
  ai_settings: SERIES_AI_DEFAULTS, has_uses: true, uses: [{ project_id: 4, ref: { kind: 'world', id: 123 } }],
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

  it('对设定协作者按包含隐藏书的 has_uses 限制删除，不用可见 uses 数量推断', () => {
    const hiddenOnly = { ...sharedEntry, uses: [], has_uses: true }
    expect(canDeleteSeriesEntry(hiddenOnly, false)).toBe(false)
    expect(seriesUsageLabel(hiddenOnly, false)).toBe('已有作品采用')
    expect(canDeleteSeriesEntry({ ...hiddenOnly, has_uses: false }, false)).toBe(true)
    expect(seriesUsageLabel({ ...hiddenOnly, has_uses: false }, false)).toBe('尚未被作品采用')
    expect(canDeleteSeriesEntry(hiddenOnly, true)).toBe(true)
    expect(seriesUsageLabel(sharedEntry, true)).toBe('1 本作品采用')
    // 旧响应缺少标记时也不能误认为可以删除。
    expect(canDeleteSeriesEntry({ ...hiddenOnly, has_uses: undefined } as unknown as SeriesEntry, false)).toBe(false)
  })

  it('系列编辑者的收回目标只包含本人作者或已接受作品编辑者，管理员写权限不替代协作', () => {
    const books = [
      { id: 1, title: '自己的书', recycled: false }, { id: 2, title: '编辑的书', recycled: false },
      { id: 3, title: '只读的书', recycled: false }, { id: 4, title: '回收的书', recycled: true },
      { id: 5, title: '管理员代管的书', recycled: false },
    ]
    const projects = [
      { id: 1, access: { role: 'owner', is_collaborator: false, can_edit_content: true } },
      { id: 2, access: { role: 'editor', is_collaborator: true, can_edit_content: true } },
      { id: 3, access: { role: 'viewer', is_collaborator: true, can_edit_content: true } },
      { id: 4, access: { role: 'owner', is_collaborator: false, can_edit_content: true } },
      { id: 5, access: { role: 'admin', is_collaborator: false, can_edit_content: true } },
    ] as const
    expect(editableSeriesBooks(books, projects, false).map((book) => book.id)).toEqual([1, 2])
    expect(editableSeriesBooks(books, projects, true).map((book) => book.id)).toEqual([1, 2, 3, 5])
    expect(editableSeriesBooks(books, null, false)).toEqual([])
    expect(canChangeSeriesBook('owner', false, true)).toBe(true)
    expect(canChangeSeriesBook('editor', true, true)).toBe(true)
    expect(canChangeSeriesBook('viewer', true, true)).toBe(false)
    expect(canChangeSeriesBook('admin', false, true)).toBe(false)
    expect(canChangeSeriesBook('editor', true, false)).toBe(false)
  })
})
