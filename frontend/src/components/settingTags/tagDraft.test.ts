import { describe, expect, it } from 'vitest'
import { normalizeSettingTags, preventTagFormSubmit, settingTagResources } from './tagDraft'

describe('整理标签的唯一写入规则', () => {
  it('去空、去首尾空白、精确去重并保留首次顺序，不改变输入', () => {
    const raw = ['  门派 ', '', '门派', 'Sect', 'sect', '\u0085江湖\u001c', '   ']
    expect(normalizeSettingTags(raw)).toEqual(['门派', 'Sect', 'sect', '江湖'])
    expect(raw[0]).toBe('  门派 ')
    expect(normalizeSettingTags(Array(55).fill(' 同门 '))).toEqual(['同门'])
    expect(normalizeSettingTags([])).toEqual([])
  })

  it('按 Unicode 码点限制每个标签与规范化后总数，拒绝 null 和非字符串', () => {
    expect(normalizeSettingTags(['😀'.repeat(50)])).toEqual(['😀'.repeat(50)])
    expect(() => normalizeSettingTags(['😀'.repeat(51)])).toThrow('50')
    expect(() => normalizeSettingTags(Array.from({ length: 51 }, (_, i) => String(i)))).toThrow('50')
    for (const raw of [null, '门派', ['门派', 1], [null]]) expect(() => normalizeSettingTags(raw)).toThrow()
    // Python str.strip 不会移除 BOM；前后端对标签身份采用相同规则。
    expect(normalizeSettingTags(['\ufeff门派\ufeff'])).toEqual(['\ufeff门派\ufeff'])
  })

  it('Enter 添加标签时阻止外层表单提交，保留其他按键', () => {
    let prevented = 0
    preventTagFormSubmit({ key: 'Enter', preventDefault: () => { prevented += 1 } })
    preventTagFormSubmit({ key: 'a', preventDefault: () => { prevented += 1 } })
    expect(prevented).toBe(1)
  })

  it('四类保存广播整理范围与实际实体范围', () => {
    expect(settingTagResources('character')).toEqual(['setting-categories', 'characters'])
    expect(settingTagResources('world')).toEqual(['setting-categories', 'world'])
    expect(settingTagResources('organization')).toEqual(['setting-categories', 'organizations'])
    expect(settingTagResources('career')).toEqual(['setting-categories', 'careers'])
  })
})
