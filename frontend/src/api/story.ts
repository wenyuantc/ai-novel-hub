import { api, request } from './client'
import { invalidateProject } from '../utils/projectRefresh'
import type {
  ChapterCast,
  Character,
  CharacterInput,
  Outline,
  OutlineInput,
  WorldSetting,
  WorldSettingInput,
} from './types'

function makeCrud<TItem, TInput>(segment: string) {
  const base = (pid: number) => `/projects/${pid}/${segment}`
  return {
    list: (pid: number) => api.get<TItem[]>(base(pid)),
    create: (pid: number, body: TInput) => api.post<TItem>(base(pid), body),
    update: (pid: number, id: number, body: Partial<TInput>) => api.patch<TItem>(`${base(pid)}/${id}`, body),
    remove: (pid: number, id: number) => api.del(`${base(pid)}/${id}`),
  }
}

export const worldApi = makeCrud<WorldSetting, WorldSettingInput>('world')
export const characterApi = makeCrud<Character, CharacterInput>('characters')
export const outlineApi = makeCrud<Outline, OutlineInput>('outlines')

/** 章节出场角色（手动指定，可选）：GET 返回 id 列表，PUT 整体替换 */
export const castApi = {
  get: (pid: number, cid: number) => api.get<ChapterCast>(`/projects/${pid}/chapters/${cid}/cast`),
  set: (pid: number, cid: number, characterIds: number[]) =>
    request<ChapterCast>(`/projects/${pid}/chapters/${cid}/cast`, {
      method: 'PUT',
      body: { character_ids: characterIds },
    }).then((result) => { invalidateProject(pid, ['planning', 'chapters']); return result }),
}
