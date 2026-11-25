import { request, type RequestOptions } from './client'

export type WritingPeriod = 'day' | 'week'
export interface WritingGoal {
  period: WritingPeriod | null
  target_words: number | null
  revision: number
}
export interface WritingStatsRange {
  start_date: string
  end_date: string
}
export interface WritingStatsDay {
  date: string
  manual_words: number | null
  ai_words: number | null
  partial: boolean
}
export interface WritingStatsOverview extends WritingStatsRange {
  today: string
  tracking_started_at: string
  today_manual_words: number
  today_ai_words: number
  goal: WritingGoal
  period_manual_words: number
  streak_days: number
  days: WritingStatsDay[]
}
type Options = Pick<RequestOptions, 'signal' | 'isCurrent' | 'editorActor'>

export const writingStatsApi = {
  overview: (projectId: number, range?: WritingStatsRange, options?: Options) =>
    request<WritingStatsOverview>(`/projects/${projectId}/writing-stats/overview`, {
      ...options, query: { ...range },
    }),
  setGoal: (projectId: number, body: Omit<WritingGoal, 'revision'> & { expected_revision: number }, options?: Options) =>
    request<WritingGoal>(`/projects/${projectId}/writing-stats/goal`, { ...options, method: 'PUT', body }),
}
