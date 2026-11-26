import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadStoryDate, storyTimelineApi, type StoryCalendar, type TimelineItem } from './storyTimeline'

const calendar = (revision: number): StoryCalendar => ({ revision, era_name: '星历', months: [
  { id: 'mist', name: revision === 1 ? '雾月' : '雪月', days: 20 },
] })
const item = (revision: number): TimelineItem => ({ revision, kind: 'chapter', chapter_id: 1, chapter_no: 1,
  chapter_title: '归来', active: true, scene_id: null, scene_title: null, scene_sort: null,
  explicit_date: null, effective_date: null, source: 'unknown', date_label: null })

afterEach(() => vi.restoreAllMocks())

describe('章景日期编辑快照', () => {
  it('读历法后月份在另一编辑器改名，新日期版本须配对新历法', async () => {
    const calendars = vi.spyOn(storyTimelineApi, 'calendar')
      .mockResolvedValueOnce(calendar(1)).mockResolvedValueOnce(calendar(2))
    vi.spyOn(storyTimelineApi, 'item').mockResolvedValue(item(2))
    const snap = await loadStoryDate(5, 'chapter', 1, {})
    expect(calendars).toHaveBeenCalledTimes(2)
    expect(snap.calendar.months[0].name).toBe('雪月')
    expect(snap.calendar.revision).toBe(snap.item.revision)
  })
  it('连续交错时结束重读并告知重试', async () => {
    vi.spyOn(storyTimelineApi, 'calendar').mockResolvedValue(calendar(1))
    const rows = vi.spyOn(storyTimelineApi, 'item').mockResolvedValue(item(2))
    await expect(loadStoryDate(5, 'chapter', 1, {})).rejects.toThrow('请重新读取')
    expect(rows).toHaveBeenCalledTimes(3)
  })
})
