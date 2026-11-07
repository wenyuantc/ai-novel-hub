import { describe, expect, it } from 'vitest'
import { storyPositionQuery } from './storyPosition'
import { entityDetailHref, readEntityFieldPosition } from './entityLinks'
import { toPositionInput } from '../components/progressions/position'

describe('真实场景位置传输', () => {
  it('只传稳定场景身份和开始/结束，不自报章号或顺序', () => {
    const position = { anchor_kind: 'scene' as const, scene_id: 12, chapter_id: null, chapter_no: null, boundary: 'end' as const }
    expect(storyPositionQuery(position)).toEqual({ scene_id: 12, boundary: 'end' })
    expect(toPositionInput(position)).toEqual(position)
    const href = entityDetailHref(1, { kind: 'character', id: 7 }, position)
    expect(href).toContain('scene_id=12')
    expect(readEntityFieldPosition(new URLSearchParams(href.split('?')[1]))).toEqual(position)
  })
  it('混合位置与非法Scene ID不产生有效查询或历史深链', () => {
    expect(() => storyPositionQuery({ anchor_kind: 'scene', scene_id: 2, chapter_id: 4, chapter_no: null, boundary: 'start' })).toThrow()
    expect(toPositionInput({ anchor_kind: 'scene', scene_id: 0, chapter_id: null, chapter_no: null, boundary: 'start' })).toBeNull()
    expect(readEntityFieldPosition(new URLSearchParams('view=effective&scene_id=2&chapter_no=4&boundary=start'))).toBeNull()
  })
})
