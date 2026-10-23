import { describe, expect, it } from 'vitest'
import { cleanAliases, isProtagonist, roleTypeOptions } from './characters'

describe('角色字段', () => {
  it('定位下拉兼容旧自由文本值', () => {
    expect(roleTypeOptions('主角').map((o) => o.value)).toEqual(['主角', '重要配角', '配角', '反派', '其他'])
    const opts = roleTypeOptions('亦正亦邪的导师')
    expect(opts.at(-1)).toEqual({ value: '亦正亦邪的导师', label: '亦正亦邪的导师（原值）' })
    expect(roleTypeOptions('')).toHaveLength(5)
  })

  it('主角判定与后端一致', () => {
    expect(isProtagonist({ role_type: ' Protagonist ' })).toBe(true)
    expect(isProtagonist({ role_type: '主角' })).toBe(true)
    expect(isProtagonist({ role_type: '配角', always_in_context: true })).toBe(true)
    expect(isProtagonist({ role_type: '配角' })).toBe(false)
  })

  it('别名清洗与限制', () => {
    expect(cleanAliases([' 小远 ', '', '小远', '远哥'])).toEqual({ aliases: ['小远', '远哥'], error: null })
    expect(cleanAliases(['x'.repeat(51)]).error).toBe('单个别名不能超过 50 字')
    expect(cleanAliases(Array.from({ length: 11 }, (_, i) => `a${i}`)).error).toBe('别名最多 10 个')
  })
})
