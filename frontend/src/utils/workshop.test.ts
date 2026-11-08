import { describe, expect, it } from 'vitest'
import type { PromptTemplateDetail, WorkshopItem, WorkshopSubmission, WritingStyle } from '../api/types'
import { emptyStyleDraft } from '../api/styles'
import { canRestoreWorkshopItem, normalizeWorkshopTags, promptShare, styleShare, updateShare, workshopImportPairs, workshopSubmissionKindLabel, workshopTab, workshopTagsError } from './workshop'

const template = (enabled = true): PromptTemplateDetail => ({
  key: 'chapter.generate', name: '写章节', category: 'chapter', category_label: '章节', description: '写作', overridden: true, enabled,
  outdated: false, updated_at: '', default_system: '默认系统', default_user: '默认用户', default_fingerprint: 'fp', variables: [], locked: [], components: [],
  override: { system: '我的系统', user: '我的用户', enabled, base_fingerprint: 'fp', base_system: '', base_user: '', updated_at: '' },
})
export const workshopItemFixture = (overrides: Partial<WorkshopItem> = {}): WorkshopItem => ({
  id: 1, kind: 'style', prompt_key: null, name: '雨夜风格', description: '短句叙事', category: 'suspense', tags: ['雨夜'], content: { pov: '第一人称' },
  author_name: 'alice', author_username: null, is_mine: false, is_anonymous: false, is_official: false,
  status: 'active', hidden_by: null, like_count: 3, download_count: 2, liked: false, imported: false,
  published_at: '2026-10-28T08:00:00', created_at: '2026-10-28T08:00:00', updated_at: '2026-10-28T08:00:00', ...overrides,
})

describe('工坊分享与导入', () => {
  it('审核深链接只允许管理员；非法页签和普通用户审核请求回退浏览', () => {
    expect(workshopTab('review', true)).toBe('review')
    expect(workshopTab('review', false)).toBe('browse')
    expect(workshopTab('mine', false)).toBe('mine')
    expect(workshopTab('unknown', true)).toBe('browse')
    expect(workshopTab(null, true)).toBe('browse')
  })
  it('只分享当前保存的覆盖，停用覆盖也可分享；导入对比取当前生效内容', () => {
    const content = { system: '工坊系统', user: '工坊用户' }
    expect(workshopImportPairs(template(), content).map((p) => p.a)).toEqual(['我的系统', '我的用户'])
    expect(workshopImportPairs(template(false), content).map((p) => p.a)).toEqual(['默认系统', '默认用户'])
    expect(workshopImportPairs({ ...template(), override: null }, content).map((p) => p.a)).toEqual(['默认系统', '默认用户'])
    expect(promptShare(template(false))?.content).toEqual({ system: '我的系统', user: '我的用户' })
    expect(promptShare({ ...template(), override: null })).toBeNull()
  })
  it('风格分享只发内容字段，不携带预设标记、引用或 ID', () => {
    const style: WritingStyle = { ...emptyStyleDraft(), id: 7, name: '冷峻', description: '冷静', pov: '第三人称', preset_key: null, is_preset: false, sort: 1, references: { projects: 5, chapters: 3 }, created_at: '', updated_at: '' }
    const body = styleShare(style)
    expect(body.name).toBe('冷峻')
    expect(body.content).toMatchObject({ description: '冷静', pov: '第三人称' })
    expect(body.content).not.toHaveProperty('id')
    expect(body.content).not.toHaveProperty('references')
  })
  it('条目更新携带条目 ID，驳回提交重新发送原 item_id', () => {
    const item = workshopItemFixture()
    expect(updateShare(item)).toMatchObject({ item_id: 1, content: item.content, category: 'suspense' })
    const submission: WorkshopSubmission = {
      id: 12, item_id: 1, kind: 'style', prompt_key: null, name: '雨夜风格', description: '短句叙事',
      category: 'suspense', tags: ['雨夜'], content: item.content, is_anonymous: false,
      author_name: 'alice', author_username: null, status: 'rejected', review_note: '调整内容', reviewed_at: '',
      created_at: '', updated_at: '',
    }
    expect(updateShare(submission).item_id).toBe(1)
    expect(updateShare({ ...submission, item_id: null }).item_id).toBeNull()
    expect(updateShare(item)).not.toHaveProperty('is_official')
    expect(updateShare(item)).not.toHaveProperty('like_count')
  })
  it('作者只能恢复自己下架的条目，管理员可恢复任意条目', () => {
    const hidden = workshopItemFixture({ status: 'hidden', is_mine: true, hidden_by: 'author' })
    expect(canRestoreWorkshopItem(hidden, false)).toBe(true)
    expect(canRestoreWorkshopItem({ ...hidden, hidden_by: 'admin' }, false)).toBe(false)
    expect(canRestoreWorkshopItem({ ...hidden, is_mine: false }, false)).toBe(false)
    expect(canRestoreWorkshopItem({ ...hidden, is_mine: false, hidden_by: 'admin' }, true)).toBe(true)
  })
  it('审核通过后回写条目 ID 不应把首次提交误标为更新', () => {
    expect(workshopSubmissionKindLabel({ status: 'pending', item_id: null })).toBe('首次提交')
    expect(workshopSubmissionKindLabel({ status: 'pending', item_id: 1 })).toBe('条目更新')
    expect(workshopSubmissionKindLabel({ status: 'rejected', item_id: 1 })).toBe('条目更新')
    expect(workshopSubmissionKindLabel({ status: 'approved', item_id: 1 })).toBe('已发布')
  })
  it('标签去空白去重，限制个数/码点长度与特殊字符', () => {
    expect(normalizeWorkshopTags([' 都市 ', '都市', '', '悬疑'])).toEqual(['都市', '悬疑'])
    expect(workshopTagsError(['😀'.repeat(12)])).toBeNull()
    expect(workshopTagsError(['😀'.repeat(13)])).toContain('1–12')
    expect(workshopTagsError(['a', 'b', 'c', 'd', 'e', 'f'])).toContain('5')
    for (const tag of ['a"b', 'a\\b', 'a\nb', '']) expect(workshopTagsError([tag])).not.toBeNull()
  })
})
