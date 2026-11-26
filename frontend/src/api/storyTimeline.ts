import { request, type RequestOptions } from './client'

export type TargetKind = 'chapter' | 'scene'
export type TimelineOrder = 'story' | 'chapter'
export interface StoryMonth { id: string; name: string; days: number }
export interface StoryCalendar { revision: number; era_name: string | null; months: StoryMonth[] }
export interface StoryDate { year: number; month_id: string; day: number }
export interface TimelineItem {
  kind: TargetKind
  chapter_id: number
  chapter_no: number
  chapter_title: string
  scene_id: number | null
  scene_sort: number | null
  scene_title: string | null
  active: boolean
  explicit_date: StoryDate | null
  effective_date: StoryDate | null
  source: 'chapter' | 'scene' | 'inherited' | 'unknown'
  date_label: string | null
  revision: number
}
export interface GeneratedCalendar extends StoryCalendar { era_name: string }
export interface CalendarGenerateRequest { expected_revision: number; instruction: string; ai_config_id: number | null }
export interface CalendarImpact { blocked: TimelineItem[]; reordered: TimelineItem[] }
export interface CalendarChange { expected_revision: number; era_name: string; months: StoryMonth[]; confirm_reorder?: boolean }
type Options = Pick<RequestOptions, 'signal' | 'isCurrent' | 'editorActor'>
const base = (pid: number) => `/projects/${pid}/story-timeline`

export const storyTimelineApi = {
  calendar: (pid: number, opts: Options) => request<StoryCalendar>(`${base(pid)}/calendar`, opts),
  generateCalendar: (pid: number, body: CalendarGenerateRequest, opts: Options) =>
    request<GeneratedCalendar>(`${base(pid)}/calendar/generate`, { ...opts, method: 'POST', body }),
  preview: (pid: number, body: CalendarChange, opts: Options) =>
    request<CalendarImpact>(`${base(pid)}/calendar/preview`, { ...opts, method: 'POST', body }),
  saveCalendar: (pid: number, body: CalendarChange, opts: Options) =>
    request<StoryCalendar>(`${base(pid)}/calendar`, { ...opts, method: 'PUT', body }),
  item: (pid: number, kind: TargetKind, id: number, opts: Options) =>
    request<TimelineItem>(`${base(pid)}/${kind}/${id}/date`, opts),
  saveDate: (pid: number, kind: TargetKind, id: number, expected_revision: number, date: StoryDate | null, opts: Options) =>
    request<TimelineItem>(`${base(pid)}/${kind}/${id}/date`, { ...opts, method: 'PUT', body: { expected_revision, date } }),
  list: (pid: number, order: TimelineOrder, offset: number, limit: number, opts: Options) =>
    request<{ items: TimelineItem[]; total: number; revision: number }>(base(pid), { ...opts, query: { order, offset, limit } }),
}

/** 章景与历法分开读取。只有同一修订的结果可以一起展示并用于下一次保存。 */
export async function loadStoryDate(pid: number, kind: TargetKind, id: number, opts: Options) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const [calendar, item] = await Promise.all([
      storyTimelineApi.calendar(pid, opts), storyTimelineApi.item(pid, kind, id, opts),
    ])
    if (calendar.revision === item.revision) return { calendar, item }
  }
  throw new Error('时间线正在被修改，请重新读取后再保存。')
}
