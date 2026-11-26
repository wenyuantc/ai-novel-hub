import { describe, expect, it } from 'vitest'
import { loginReturnPath } from './loginReturn'

describe('第三方登录回跳', () => {
  it('保留站内页面及查询参数', () => {
    expect(loginReturnPath('/projects/7/overview?tab=work')).toBe('/projects/7/overview?tab=work')
  })
  it.each([null, 'https://example.org', '//example.org/path', '/\\example.org', '/\t/example.org'])('拒绝外部或无效地址 %j', value => {
    expect(loginReturnPath(value)).toBe('/projects')
  })
})
