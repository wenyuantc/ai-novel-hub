import { describe, expect, it } from 'vitest'
import { parseAdminUserId } from './adminUserDetails'

describe('用户详情及owner深链ID', () => {
  it('仅接受正十进制安全整数', () => {
    expect(parseAdminUserId('7')).toBe(7)
    expect(parseAdminUserId('007')).toBe(7)
    expect(parseAdminUserId(String(Number.MAX_SAFE_INTEGER))).toBe(Number.MAX_SAFE_INTEGER)
    for (const value of [null, undefined, '', '0', '-1', '1.5', '1e2', ' 7', 'Infinity', 'abc', '9007199254740992']) expect(parseAdminUserId(value)).toBeNull()
  })
})
