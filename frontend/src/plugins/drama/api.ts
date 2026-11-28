import { dramaActorRequest, dramaWrite } from './write'
import { api, rawRequest } from '../../api/client'
import { receiveAttachment } from '../../api/exports'
import type { Task } from '../../api/types'

export interface Series {
  id: number
  project_id: number
  title: string
  description: string
  aspect_ratio: string
  style: string
  revision: number
}

export interface EpisodeSummary {
  id: number
  series_id: number
  number: number
  title: string
  revision: number
}

export interface Episode extends EpisodeSummary {
  sources: { chapter_id: number | null; chapter_title: string; sort: number }[]
  source_text: string
  script: string
  script_skipped: boolean
}

export type EpisodeModelType = 'text' | 'image' | 'video'
export type EpisodeModelIds = Record<EpisodeModelType, number | null>

export type DramaTaskKind = 'drama.rewrite' | 'drama.extract' | 'drama.storyboard' | 'drama.prompts' | 'drama.image'
export type SeriesInput = Pick<Series, 'title' | 'aspect_ratio' | 'style' | 'description'>
const base = '/plugins/drama'
const series = (id: number) => base + '/series/' + id
const episode = (id: number) => base + '/episodes/' + id
export const dramaApi = {
  episodeModels: (eid: number, isCurrent: () => boolean) =>
    dramaActorRequest<EpisodeModelIds>(episode(eid) + '/models', {method:'GET', isCurrent}),
  setEpisodeModel: (eid: number, type: EpisodeModelType, id: number | null, isCurrent: () => boolean) =>
    dramaWrite<EpisodeModelIds>(episode(eid) + '/models', {method:'PATCH', body:{[type]:id}, isCurrent}),
  styles: () => api.get<{ key: string; label: string }[]>(base + '/styles'),
  list: (pid: number) => api.get<Series[]>(base + '/projects/' + pid + '/series'),
  create: (pid: number, body: SeriesInput) =>
    dramaWrite<Series>(base + '/projects/' + pid + '/series', { method: 'POST', body }),
  series: (sid: number) => api.get<Series>(series(sid)),
  update: (sid: number, body: Partial<SeriesInput> & { revision: number }) =>
    dramaWrite<Series>(series(sid), { method: 'PATCH', body }),
  delete: (sid: number, revision: number) => dramaWrite<void>(series(sid), { method: 'DELETE', query: { revision } }),
  episodes: (sid: number) => api.get<EpisodeSummary[]>(series(sid) + '/episodes'),
  createEpisode: (sid: number, body: { title: string; chapter_ids?: number[]; source_text?: string }) =>
    dramaWrite<Episode>(series(sid) + '/episodes', { method: 'POST', body }),
  episode: (eid: number) => api.get<Episode>(episode(eid)),
  updateEpisode: (
    eid: number,
    body: Partial<Pick<Episode, 'source_text' | 'script' | 'script_skipped' | 'title'>> & { revision: number },
  ) => dramaWrite<Episode>(episode(eid), { method: 'PATCH', body }),
  deleteEpisode: (eid: number, revision: number) =>
    dramaWrite<void>(episode(eid), { method: 'DELETE', query: { revision } }),
  task: (sid: number, eid: number | undefined, kind: DramaTaskKind, params: Record<string, unknown>) =>
    dramaWrite<Task>((eid === undefined ? series(sid) : episode(eid)) + '/tasks', {
      method: 'POST',
      body: { ...params, kind },
    }),
  apply: (sid: number, eid: number | undefined, task: Task) =>
    dramaWrite((eid === undefined ? series(sid) : episode(eid)) + '/tasks/' + task.id + '/apply', {
      method: 'POST',
      body: { task_key: task.task_key },
    }),
  export: async (eid: number, format: 'md' | 'docx', parts: string, isCurrent: () => boolean) => {
    if (!isCurrent()) return
    const response = await rawRequest(episode(eid) + '/export', { query: { format, parts }, isCurrent })
    await receiveAttachment(response, '短剧.' + format, { isCurrent })
  },
}
