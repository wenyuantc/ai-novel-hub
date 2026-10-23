import { api } from './client'
import type {
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
