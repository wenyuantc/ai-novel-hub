import { describe, expect, it } from 'vitest'
import { createRelationRequestScope } from './relationForm'
import { applyRelationRefresh, refreshRelationData } from './relationRefresh'

function deferred<T>() {
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((_, fail) => { reject = fail })
  return { promise, reject }
}

describe('关系剧情面板刷新', () => {
  it('真实异步刷新失败时保留同一人物对的数据引用，使打开的编辑器和草稿不会被卸载', async () => {
    const key = '作品1:角色1:角色2:作者'
    const previous = { key, data: { timelineId: 7, events: [{ id: 2, value: '初识' }] } }
    const failedRead = deferred<typeof previous.data>()
    const pending = refreshRelationData(() => failedRead.promise)
    failedRead.reject(new Error('网络暂时不可用'))
    const result = await pending
    const next = applyRelationRefresh(previous, key, result)
    expect(result.error).toBe('网络暂时不可用')
    expect(next).toBe(previous)
    expect(next?.data.events[0].value).toBe('初识')

    const recovered = await refreshRelationData(async () => ({ timelineId: 7, events: [{ id: 2, value: '重逢' }] }))
    expect(applyRelationRefresh(next, key, recovered)).toEqual({ key, data: recovered.data })
  })

  it('切换人物对或身份后，失败不会展示旧数据，晚到的旧请求也不能写入', async () => {
    const oldKey = '作品1:角色1:角色2:作者'
    const newKey = '作品1:角色1:角色3:作者'
    const previous = { key: oldKey, data: { timelineId: 7 } }
    const scope = createRelationRequestScope()
    const oldTicket = scope.capture()
    const oldRead = deferred<typeof previous.data>()
    const pendingOld = refreshRelationData(() => oldRead.promise)
    scope.invalidate()

    const failed = await refreshRelationData<typeof previous.data>(async () => { throw new Error('读取失败') })
    const next = applyRelationRefresh(previous, newKey, failed)
    expect(next).toBeNull()
    expect(applyRelationRefresh(previous, '作品1:角色1:角色2:另一账号', failed)).toBeNull()
    oldRead.reject(new Error('旧请求晚到'))
    const late = await pendingOld
    expect(scope.isCurrent(oldTicket, true)).toBe(false)
    expect(late.error).toBe('旧请求晚到')
    expect(next).toBeNull()
  })
})
