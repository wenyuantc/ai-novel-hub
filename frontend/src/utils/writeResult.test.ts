import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { writeResultUnknown } from './writeResult'

describe('写请求结果是否可确定', () => {
  it.each([0, 408, 500, 503])('HTTP %s 需要重新核对', (status) => {
    expect(writeResultUnknown(new ApiError(status, '失败'))).toBe(true)
  })
  it.each([400, 403, 404, 409, 422])('HTTP %s 是已返回的明确拒绝', (status) => {
    expect(writeResultUnknown(new ApiError(status, '失败'))).toBe(false)
  })
  it('非 HTTP 异常不推断事务结果', () => {
    expect(writeResultUnknown(new Error('响应解析失败'))).toBe(true)
  })
})
