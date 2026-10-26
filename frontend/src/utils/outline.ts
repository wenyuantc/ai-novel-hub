import type { Outline } from '../api/types'

export interface VolumeGroup<T extends Outline = Outline> {
  volumeNo: number
  volumeTitle: string
  items: T[]
}

/**
 * 按卷分组：顺序与后端列表一致 (volume_no, sort, chapter_no, id)——大纲页上移 / 下移只改 sort，不改章节号。
 * 卷名取该卷第一个非空的 volume_title。
 */
export function groupOutlines<T extends Outline>(items: T[]): VolumeGroup<T>[] {
  const map = new Map<number, VolumeGroup<T>>()
  const sorted = [...items].sort(
    (a, b) => a.volume_no - b.volume_no || a.sort - b.sort || a.chapter_no - b.chapter_no || a.id - b.id,
  )
  for (const o of sorted) {
    let g = map.get(o.volume_no)
    if (!g) {
      g = { volumeNo: o.volume_no, volumeTitle: o.volume_title, items: [] }
      map.set(o.volume_no, g)
    }
    if (!g.volumeTitle && o.volume_title) g.volumeTitle = o.volume_title
    g.items.push(o)
  }
  return [...map.values()]
}

export function volumeLabel(g: Pick<VolumeGroup, 'volumeNo' | 'volumeTitle'>): string {
  return `第${g.volumeNo}卷${g.volumeTitle ? ` ${g.volumeTitle}` : ''}`
}
