import { describe, expect, it } from 'vitest'
import type { Outline } from '../api/types'
import { groupOutlines, volumeLabel } from './outline'

function o(id: number, volume_no: number, chapter_no: number, sort: number, volume_title = ''): Outline {
  return { id, project_id: 1, volume_no, volume_title, chapter_no, title: `t${id}`, summary: '', sort }
}

describe('大纲按卷分组', () => {
  it('卷内按 sort 排列（上移 / 下移只改 sort），sort 相同再按章节号与 id', () => {
    const groups = groupOutlines([o(1, 1, 1, 2), o(2, 1, 2, 1), o(3, 2, 3, 1, '下卷'), o(4, 1, 3, 2), o(5, 1, 3, 2)])
    expect(groups.map((g) => g.volumeNo)).toEqual([1, 2])
    expect(groups[0].items.map((x) => x.id)).toEqual([2, 1, 4, 5])
    expect(groups[1].volumeTitle).toBe('下卷')
  })

  it('卷名取第一个非空值；标签带卷名', () => {
    const [g] = groupOutlines([o(1, 3, 1, 1), o(2, 3, 2, 2, '风起')])
    expect(g.volumeTitle).toBe('风起')
    expect(volumeLabel(g)).toBe('第3卷 风起')
    expect(volumeLabel({ volumeNo: 2, volumeTitle: '' })).toBe('第2卷')
  })
})
