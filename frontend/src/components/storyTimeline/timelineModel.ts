import type { TimelineItem } from '../../api/storyTimeline'

export const dateSources = { chapter: '章节默认', scene: '场景单独指定', inherited: '沿用本章', unknown: '未标时间' }
export function itemTitle(item: TimelineItem) {
  const chapter = `第 ${item.chapter_no} 章 · ${item.chapter_title || '未命名章节'}`
  if (item.kind === 'chapter') return chapter
  return `${chapter} / 场景 ${(item.scene_sort ?? 0) + 1} · ${item.scene_title || '未命名场景'}${item.active ? '' : '（已退场）'}`
}
export function dateText(item: TimelineItem) {
  return item.date_label ? `${dateSources[item.source]} · ${item.date_label}` : '未标时间'
}
export function manuscriptHref(pid: number, item: TimelineItem) {
  return `/projects/${pid}/chapters/${item.chapter_id}${item.scene_id === null ? '' : `?scene_id=${item.scene_id}`}`
}
