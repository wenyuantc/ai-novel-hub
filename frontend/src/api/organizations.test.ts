import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { memberApi, memberChanges, memberSnapshot, memberPositionIntent } from './organizations'

afterEach(() => vi.unstubAllGlobals())

describe('成员时间线请求', () => {
  it('剧情退出保留章节锚点、章末边界及读取时版本', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    await memberApi.remove(4, 8, 15, {
      edit_mode: 'progression', expected_revision: 7, anchor_kind: 'planned', chapter_no: 20, boundary: 'end',
    })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/organizations/8/members/15?edit_mode=progression&expected_revision=7&anchor_kind=planned&chapter_no=20&boundary=end')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'DELETE' })
  })

  it('版本冲突保留服务端代码，由表单处理且不自动重放', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      detail: '已有新变化', code: 'stale_revision',
    }), { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const request = memberApi.update(4, 8, 15, {
      edit_mode: 'baseline', expected_revision: 3, position: '掌门',
    })
    await expect(request).rejects.toMatchObject({ status: 409, body: { code: 'stale_revision' } } satisfies Partial<ApiError>)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('首次登记计划章成员时发送不存在前置条件并保留计划章锚点', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ id: 15 }), { status: 201 }))
    vi.stubGlobal('fetch', fetchMock)
    await memberApi.create(4, 8, {
      character_id: 13, position: '弟子', status: 'active', note: '',
      edit_mode: 'progression', expected_revision: 0, anchor_kind: 'planned', chapter_no: 20, boundary: 'end',
    })
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
      expected_revision: 0, anchor_kind: 'planned', chapter_no: 20, boundary: 'end',
    })
  })

  it('读空后遇到并发新增时保留 409，不将旧输入自动重放为新成员', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      detail: '成员历史已变化', code: 'stale_revision',
    }), { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(memberApi.create(4, 8, {
      character_id: 13, position: '弟子', edit_mode: 'baseline', expected_revision: 0,
    })).rejects.toMatchObject({ status: 409, body: { code: 'stale_revision' } })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('删除完整历史使用稳定角色与组织，而不是已消失的当前成员 id', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    await memberApi.removeRecord(4, 8, 13, 7)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects/4/organizations/8/membership-records/13?expected_revision=7')
  })
})

describe('成员表单与章节快照', () => {
  it('只提交相对所选章节实际修改的字段，不携带未来职位和状态', () => {
    const early = memberSnapshot({ exists: true, position: '弟子', status: 'active', note: '初入门' })
    expect(early.exists).toBe(true)
    if (!early.exists) throw new Error('不应缺席')
    expect(memberChanges(early.state, { ...early.state, note: '早章备注' })).toEqual({ note: '早章备注' })
    const legacy = { ...early.state, position: ' 弟子 ' }
    expect(memberChanges(legacy, { ...legacy })).toEqual({})
  })

  it('成员备注保持原本无限长文本，缺席和不完整快照不会伪造状态', () => {
    const note = '长备注'.repeat(120)
    const snapshot = memberSnapshot({ exists: true, position: '弟子', status: 'active', note })
    expect(snapshot.exists && snapshot.state.note).toBe(note)
    expect(memberSnapshot({ exists: false })).toEqual({ exists: false })
    expect(() => memberSnapshot({ exists: true, position: '弟子', status: 'invalid', note: '' })).toThrow()
    expect(() => memberSnapshot({ exists: true, position: '弟子', status: 'active' })).toThrow()
  })

  it('已有章使用章节 ID，计划章使用章号，并拒绝无效锚点', () => {
    expect(memberPositionIntent({ anchor_kind: 'chapter', chapter_id: 12, chapter_no: null, boundary: 'start' }, 3)).toEqual({
      edit_mode: 'progression', expected_revision: 3, anchor_kind: 'chapter', chapter_id: 12, boundary: 'start',
    })
    expect(memberPositionIntent({ anchor_kind: 'planned', chapter_id: null, chapter_no: 20, boundary: 'end' }, 4)).toEqual({
      edit_mode: 'progression', expected_revision: 4, anchor_kind: 'planned', chapter_no: 20, boundary: 'end',
    })
    expect(() => memberPositionIntent({ anchor_kind: 'planned', chapter_id: null, chapter_no: 0, boundary: 'start' }, 4)).toThrow()
  })
})
