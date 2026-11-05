import { describe, expect, it } from 'vitest'
import { chapterOptions } from '../progressions/position'
import {
  buildRelationUpdate,
  readRelationSnapshot,
  relationSubject,
  type RelationValues,
} from './relationForm'

const oldState = {
  exists: true,
  identity: { a_id: 1, b_id: 2, type_id: 4 },
  intimacy: 10,
  status: 'normal',
  description: '初识',
}

const values: RelationValues = {
  a_id: 1, b_id: 2, type_id: 4, intimacy: 10, status: 'normal', description: '初识',
}

describe('关系按位置编辑', () => {
  it('以稳定人物对查找时间线，不依赖当前关系方向', () => {
    expect(relationSubject(9, 2)).toEqual({ subject_kind: 'relationship', character_id: 2, related_character_id: 9 })
    expect(() => relationSubject(2, 2)).toThrow()
  })

  it('校验基础或有效快照完整且身份属于固定人物对', () => {
    expect(readRelationSnapshot(oldState, [1, 2])).toEqual({ ...values, exists: true })
    expect(readRelationSnapshot({ exists: false }, [1, 2])).toEqual({ exists: false })
    expect(() => readRelationSnapshot({ ...oldState, identity: { a_id: 1, b_id: 3, type_id: 4 } }, [1, 2])).toThrow()
    expect(() => readRelationSnapshot({ ...oldState, intimacy: 101 }, [1, 2])).toThrow()
    expect(() => readRelationSnapshot({ exists: true }, [1, 2])).toThrow()
  })

  it('只提交相对目标章节的实际变化，绝不把最新状态带回早章', () => {
    const before = readRelationSnapshot(oldState, [1, 2])
    const result = buildRelationUpdate(before, { ...values, description: '初识，互有戒心' }, {
      mode: 'progression', revision: 7,
      position: { anchor_kind: 'chapter', chapter_id: 5, chapter_no: null, boundary: 'start' },
      note: '',
    })
    expect(result).toEqual({
      ok: true,
      body: { edit_mode: 'progression', expected_revision: 7, anchor_kind: 'chapter', chapter_id: 5, chapter_no: null, boundary: 'start', description: '初识，互有戒心' },
    })
  })

  it('人物方向与类型作为一组提交，不接受替换为第三人', () => {
    const before = readRelationSnapshot(oldState, [1, 2])
    const context = { mode: 'baseline' as const, revision: 3, position: null, note: '' }
    expect(buildRelationUpdate(before, { ...values, a_id: 2, b_id: 1 }, context)).toEqual({
      ok: true, body: { edit_mode: 'baseline', expected_revision: 3, a_id: 2, type_id: 4 },
    })
    expect(buildRelationUpdate(before, { ...values, b_id: 3 }, context)).toMatchObject({ ok: false })
  })

  it('选择计划章时，即使同号章节已存在也保留计划锚点', async () => {
    const { buildRelationEvent } = await import('./relationForm')
    // 已有章节 20 与计划章 20 可以并存；请求只能按照作者选定的类型发送。
    const existingChapters = [{ id: 91, chapter_no: 20, title: '同号实体章' }]
    expect(chapterOptions(existingChapters)).toEqual([{ value: 91, label: '第 20 章 · 同号实体章' }])
    const position = { anchor_kind: 'planned' as const, chapter_id: null, chapter_no: 20, boundary: 'end' as const }
    expect(buildRelationUpdate(readRelationSnapshot(oldState, [1, 2]), { ...values, intimacy: 11 }, {
      mode: 'progression', revision: 3, position, note: '',
    })).toMatchObject({ ok: true, body: { anchor_kind: 'planned', chapter_id: null, chapter_no: 20, boundary: 'end' } })
    expect(buildRelationEvent({ field: 'presence', presence: false, values, position, operation: 'replace', sortOrder: 0, note: '' }, [1, 2], 3))
      .toMatchObject({ ok: true, value: { anchor_kind: 'planned', chapter_id: null, chapter_no: 20 } })
  })

  it('计划章节必须有效；未发生改变不提交伪造变化', () => {
    const before = readRelationSnapshot(oldState, [1, 2])
    expect(buildRelationUpdate(before, values, {
      mode: 'progression', revision: 2,
      position: { anchor_kind: 'planned', chapter_id: null, chapter_no: 12, boundary: 'end' }, note: '',
    })).toEqual({ ok: true, body: null })
    expect(buildRelationUpdate(before, { ...values, intimacy: 12 }, {
      mode: 'progression', revision: 2,
      position: { anchor_kind: 'planned', chapter_id: null, chapter_no: 0, boundary: 'end' }, note: '',
    })).toMatchObject({ ok: false })
    expect(buildRelationUpdate({ exists: false }, values, {
      mode: 'baseline', revision: 2, position: null, note: '',
    })).toMatchObject({ ok: false })
  })
})

describe('关系剧情事件', () => {
  const position = { anchor_kind: 'planned' as const, chapter_id: null, chapter_no: 20, boundary: 'end' as const }
  it('建立事件必须包含固定人物对的完整状态', async () => {
    const { buildRelationEvent } = await import('./relationForm')
    const base = { field: 'presence' as const, presence: true, values, position, operation: 'replace' as const, sortOrder: 0, note: '' }
    expect(buildRelationEvent(base, [1, 2], 8)).toMatchObject({
      ok: true, value: { field_key: 'presence', expected_revision: 8, value: { exists: true, state: oldStateWithoutExists() } },
    })
    expect(buildRelationEvent({ ...base, values: { ...values, b_id: 3 } }, [1, 2], 8)).toMatchObject({ ok: false })
    expect(buildRelationEvent({ ...base, values: { ...values, type_id: 0 } }, [1, 2], 8)).toMatchObject({ ok: false })
  })
  it('结束关系和身份事件使用结构化替换，文本才允许追加', async () => {
    const { buildRelationEvent } = await import('./relationForm')
    const base = { field: 'presence' as const, presence: false, values, position, operation: 'replace' as const, sortOrder: 0, note: '' }
    expect(buildRelationEvent(base, [1, 2], 9)).toMatchObject({ ok: true, value: { value: { exists: false }, operation: 'replace' } })
    expect(buildRelationEvent({ ...base, field: 'identity' }, [1, 2], 9)).toMatchObject({ ok: true, value: { value: oldState.identity } })
    expect(buildRelationEvent({ ...base, operation: 'append' }, [1, 2], 9)).toMatchObject({ ok: false })
    expect(buildRelationEvent({ ...base, field: 'description', operation: 'append', values: { ...values, description: '' } }, [1, 2], 9)).toMatchObject({ ok: false })
  })
})

function oldStateWithoutExists() {
  return { identity: oldState.identity, intimacy: oldState.intimacy, status: oldState.status, description: oldState.description }
}

describe('关系请求作用域', () => {
  it('切换人物或撤销读取权限后，较慢的旧响应不能写入界面', async () => {
    const { createRelationRequestScope } = await import('./relationForm')
    const scope = createRelationRequestScope()
    const displayed: string[] = []
    let resolveOld!: (value: string) => void
    const oldResponse = new Promise<string>((resolve) => { resolveOld = resolve })
    const oldTicket = scope.capture()
    const applyOld = oldResponse.then((value) => { if (scope.isCurrent(oldTicket, true)) displayed.push(value) })
    scope.invalidate()
    const nextTicket = scope.capture()
    if (scope.isCurrent(nextTicket, true)) displayed.push('新人物对')
    resolveOld('旧人物对')
    await applyOld
    expect(displayed).toEqual(['新人物对'])
    expect(scope.isCurrent(nextTicket, false)).toBe(false)
  })
})
