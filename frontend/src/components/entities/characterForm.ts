import { asLifeStatus } from '../../api/entityTasks'
import type { Character, CharacterDraft, CharacterInput, LifeStatus } from '../../api/types'

/** 角色编辑表单值（抽屉与 AI 草稿预览共用） */
export interface CharacterFormValues {
  name: string
  aliases: string[]
  role_type: string
  age: string
  gender: string
  life_status: LifeStatus
  always_in_context: boolean
  appearance: string
  personality: string
  background: string
  profile: string
  current_state: string
  sort: number | null
}

export const EMPTY_CHARACTER: CharacterFormValues = {
  name: '',
  aliases: [],
  role_type: '',
  age: '',
  gender: '',
  life_status: 'alive',
  always_in_context: false,
  appearance: '',
  personality: '',
  background: '',
  profile: '',
  current_state: '',
  sort: null,
}

export function characterToForm(c: Character | CharacterDraft): CharacterFormValues {
  return {
    name: c.name,
    aliases: c.aliases ?? [],
    role_type: c.role_type,
    age: c.age,
    gender: c.gender,
    life_status: asLifeStatus(c.life_status),
    always_in_context: c.always_in_context,
    appearance: c.appearance,
    personality: c.personality,
    background: c.background,
    profile: c.profile,
    current_state: c.current_state,
    sort: 'sort' in c ? c.sort : null,
  }
}

export function formToCharacterInput(v: CharacterFormValues): CharacterInput {
  return {
    name: v.name.trim(),
    aliases: (v.aliases ?? []).map((a) => a.trim()).filter(Boolean),
    role_type: v.role_type?.trim() ?? '',
    age: v.age?.trim() ?? '',
    gender: v.gender?.trim() ?? '',
    life_status: v.life_status ?? 'alive',
    always_in_context: !!v.always_in_context,
    appearance: v.appearance ?? '',
    personality: v.personality ?? '',
    background: v.background ?? '',
    profile: v.profile ?? '',
    current_state: v.current_state ?? '',
    sort: v.sort ?? undefined,
  }
}
