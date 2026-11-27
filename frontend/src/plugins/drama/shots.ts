import { dramaWrite } from './write'
import { request } from '../../api/client'

export interface ShotFields {
  title: string
  description: string
  shot_size: string
  atmosphere: string
  duration_seconds: number
  scene_id: number | null
  video_prompt: string
  look_ids: number[]
  prop_ids: number[]
}

export interface Shot extends ShotFields {
  id: number
  episode_id: number
  position: number
  revision: number
}

const collection = (eid: number) => `/plugins/drama/episodes/${eid}/shots`
const item = (id: number) => `/plugins/drama/shots/${id}`
export const shotApi = {
  list: (eid: number) => request<Shot[]>(collection(eid)),
  create: (eid: number, fields: Partial<ShotFields>) =>
    dramaWrite<Shot>(collection(eid), { method: 'POST', body: fields }),
  update: (id: number, revision: number, fields: Partial<ShotFields>) =>
    dramaWrite<Shot>(item(id), { method: 'PATCH', body: { revision, ...fields } }),
  remove: (id: number, revision: number) => dramaWrite<void>(item(id), { method: 'DELETE', query: { revision } }),
  order: (eid: number, revision: number, shotIds: number[]) =>
    dramaWrite<Shot[]>(collection(eid) + '/order', { method: 'PUT', body: { revision, shot_ids: shotIds } }),
}

export function moveShot(ids: number[], source: number, target: number) {
  const from = ids.indexOf(source)
  const to = ids.indexOf(target)
  if (from < 0 || to < 0 || from === to) return ids
  const result = [...ids]
  result.splice(from, 1)
  result.splice(to, 0, source)
  return result
}

/** Use literal names, including punctuation and spaces, and prefer the longest match. */
export function splitPromptReferences(text: string, names: string[]) {
  const ordered = [...new Set(names.filter(Boolean))].sort((a, b) => b.length - a.length)
  const result: { value: string; linked: boolean }[] = []
  let start = 0
  for (let at = text.indexOf('@'); at !== -1; at = text.indexOf('@', at + 1)) {
    const match = ordered.find((name) => text.startsWith(name, at + 1))
    if (!match) continue
    if (start < at) result.push({ value: text.slice(start, at), linked: false })
    result.push({ value: '@' + match, linked: true })
    at += match.length
    start = at + 1
  }
  if (start < text.length) result.push({ value: text.slice(start), linked: false })
  return result
}
