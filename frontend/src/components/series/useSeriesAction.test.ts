import { describe, expect, it } from 'vitest'
import { invalidateRequestGeneration } from '../../api/client'
import { currentSeriesIdentity } from './useSeriesAction'

describe('系列请求身份', () => {
  it('同一账号重登后，旧请求所属代次不能与当前会话相等', () => {
    const before = currentSeriesIdentity()
    invalidateRequestGeneration()
    expect(currentSeriesIdentity()).not.toBe(before)
  })
})
