import { describe, expect, it } from 'vitest'
import type { WorldSetting } from '../../api/types'
import { hasWorldContentDraft, toWorldBaselinePatch, worldBaselineMatches, worldFormValues, worldMetadataMatches } from './worldForm'

const item: WorldSetting = { id: 3, project_id: 8, title: '天剑', category: 'item', content: '剑已毁', sort: 1 }

describe('世界观基础编辑', () => {
  it('别名作为全书元数据进行去重提交和版本冲突比较，不重发基础正文', () => {
    const initial = worldFormValues({ ...item, aliases: ['古剑'] }, '完整的剑')
    expect(toWorldBaselinePatch({ ...initial, aliases: [' 灵剑 ', '灵剑'] }, initial, 4)).toEqual({ aliases: ['灵剑'], edit_mode: 'baseline', expected_revision: 4 })
    expect(toWorldBaselinePatch({ ...initial, aliases: [] }, initial, 4)).toEqual({ aliases: [], edit_mode: 'baseline', expected_revision: 4 })
    expect(worldMetadataMatches(initial, { ...item, aliases: ['新别名'] })).toBe(false)
    expect(initial.content).toBe('完整的剑')
  })
  it('以服务端基础值而不是最新投影初始化基础文本', () => {
    const initial = worldFormValues(item, '完整的剑')
    expect(initial.content).toBe('完整的剑')
    expect(worldBaselineMatches(initial, { content: '完整的剑' })).toBe(true)
    expect(worldBaselineMatches(initial, { content: '剑已毁' })).toBe(false)
  })

  it('仅改标题时不发送旧内容，基础内容和元数据可以一个 PATCH 原子保存', () => {
    const initial = worldFormValues(item, '完整的剑')
    expect(toWorldBaselinePatch({ ...initial, title: '  灵剑  ' }, initial, 4)).toEqual({
      title: '灵剑', edit_mode: 'baseline', expected_revision: 4,
    })
    expect(toWorldBaselinePatch({ ...initial, title: '灵剑', content: '古剑' }, initial, 4)).toEqual({
      title: '灵剑', content: '古剑', edit_mode: 'baseline', expected_revision: 4,
    })
  })

  it('无变化时不发请求，校准前识别未保存的基础文本草稿', () => {
    const initial = worldFormValues(item, '完整的剑')
    expect(toWorldBaselinePatch(initial, initial, 4)).toBeNull()
    expect(hasWorldContentDraft({ ...initial, title: '灵剑' }, initial)).toBe(false)
    expect(hasWorldContentDraft({ ...initial, content: '古剑' }, initial)).toBe(true)
  })

  it('刷新后比较同一条目的元数据，避免使用新版本静默覆盖他人的标题或排序', () => {
    const initial = worldFormValues(item, '完整的剑')
    expect(worldMetadataMatches(initial, { ...item, content: '最新投影' })).toBe(true)
    expect(worldMetadataMatches(initial, { ...item, title: '改名' })).toBe(false)
    expect(worldMetadataMatches(initial, { ...item, category: 'place' })).toBe(false)
    expect(worldMetadataMatches(initial, { ...item, sort: 2 })).toBe(false)
  })
})
