import type { Outline } from '../api/types'

export interface VolumeGroup {
  volumeNo: number
  volumeTitle: string
  items: Outline[]
}

export function groupOutlines(items: Outline[]): VolumeGroup[] {
  const map = new Map<number, VolumeGroup>()
  const sorted = [...items].sort(
    (a, b) => a.volume_no - b.volume_no || a.chapter_no - b.chapter_no || a.sort - b.sort,
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

export function volumeLabel(g: VolumeGroup): string {
  return `第${g.volumeNo}卷${g.volumeTitle ? ` ${g.volumeTitle}` : ''}`
}
