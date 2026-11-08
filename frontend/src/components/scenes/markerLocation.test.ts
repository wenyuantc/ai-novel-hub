import { describe, expect, it } from 'vitest'
import { markerLocation, readMarkerLocation } from './markerLocation'
import type { MarkerTimelineItem } from '../../api/richDocument'

const active: MarkerTimelineItem = {
  key: 'mark', chapter_id: 3, chapter_no: 1, chapter_title: '', scene_id: 7, scene_title: '',
  scene_active: true, section_key: null, section_title: null, content_revision: 9,
  status: 'active', start: 2, end: 4, color: 'blue', last_start: null, excerpt: '正文', excerpt_truncated: false,
}

describe('标记导航身份', () => {
  it('活动正文和区块目标均携带章节、场景及版本，区块附带身份', () => {
    const result = markerLocation(active)
    expect(result).toEqual({ chapterId: 3, sceneId: 7, revision: 9, start: 2, end: 4 })
    expect(readMarkerLocation({ markerLocation: result }, 3)).toEqual(result)
    const sectionKey = '12345678-1234-4123-8123-123456789abc'
    const section = markerLocation({ ...active, section_key: sectionKey })
    expect(section).toEqual({ ...result, sectionKey })
    expect(readMarkerLocation({ markerLocation: section }, 3)).toEqual(section)
    expect(readMarkerLocation({ markerLocation: { ...result, sectionKey: 'other' } }, 3)).toBeNull()
    expect(markerLocation({ ...active, scene_active: false })).toBeNull()
  })

  it('拒绝错章、错误数值及空范围', () => {
    const good = markerLocation(active)
    expect(readMarkerLocation({ markerLocation: good }, 4)).toBeNull()
    expect(readMarkerLocation({ markerLocation: { ...good, start: 4, end: 4 } }, 3)).toBeNull()
    expect(readMarkerLocation({ markerLocation: { ...good, sceneId: '7' } }, 3)).toBeNull()
    expect(readMarkerLocation(null, 3)).toBeNull()
  })
})
