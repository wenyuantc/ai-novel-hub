import { asLifeStatus } from '../../api/entityTasks'
import { LIFE_STATUS } from '../../api/characters'
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
    aliases: [...(c.aliases ?? [])],
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

const BASELINE_FIELDS = [
  'age', 'gender', 'appearance', 'personality', 'background', 'profile', 'current_state', 'life_status',
] as const

type CharacterBaseline = Pick<CharacterFormValues, typeof BASELINE_FIELDS[number]>

function readCharacterBaseline(baseline: Record<string, unknown>): CharacterBaseline {
  const text = (key: string): string => {
    const value = baseline[key]
    if (typeof value !== 'string') throw new Error('角色基础设定不完整，请重试')
    return value
  }
  const lifeStatus = text('life_status')
  if (!Object.hasOwn(LIFE_STATUS, lifeStatus)) throw new Error('角色基础存活状态无效，请重试')
  return {
    age: text('age'),
    gender: text('gender'),
    appearance: text('appearance'),
    personality: text('personality'),
    background: text('background'),
    profile: text('profile'),
    current_state: text('current_state'),
    life_status: asLifeStatus(lifeStatus),
  }
}

/** 以角色身份资料和时间线基础快照初始化表单；不可用最新状态代替缺失的基础值。 */
export function characterToBaselineForm(c: Character, baseline: Record<string, unknown>): CharacterFormValues {
  return { ...characterToForm(c), ...readCharacterBaseline(baseline) }
}

/** 仅当基础字段均未变化，才可沿用保留的草稿并接收刷新后的时间线修订号。 */
export function characterBaselineMatches(initial: CharacterFormValues, baseline: Record<string, unknown>): boolean {
  const next = readCharacterBaseline(baseline)
  return BASELINE_FIELDS.every((key) => initial[key] === next[key])
}

/** 只提交相对已加载基础值发生变化的字段，避免旧表单把最新状态写回基础。 */
export function formToCharacterPatch(v: CharacterFormValues, initial: CharacterFormValues): Partial<CharacterInput> {
  const next = formToCharacterInput(v)
  const previous = formToCharacterInput(initial)
  const changes = Object.entries(next).filter(([key, value]) => {
    if (value === undefined) return false
    const oldValue = previous[key as keyof CharacterInput]
    if (Array.isArray(value) && Array.isArray(oldValue)) {
      return value.length !== oldValue.length || value.some((item, index) => item !== oldValue[index])
    }
    return value !== oldValue
  })
  return Object.fromEntries(changes) as Partial<CharacterInput>
}

interface CharacterFormDraft {
  initial: CharacterFormValues
  values: CharacterFormValues
}

function copyCharacterForm(values: CharacterFormValues): CharacterFormValues {
  return { ...values, aliases: [...(values.aliases ?? [])] }
}

/** 会话内暂存各角色的基础草稿；保存后或权限范围变化时由调用方清理。 */
export function createCharacterDraftCache() {
  const drafts = new Map<number | null, CharacterFormDraft>()
  return {
    remember(characterId: number | null, initial: CharacterFormValues, values: CharacterFormValues): void {
      if (Object.keys(formToCharacterPatch(values, initial)).length === 0) {
        drafts.delete(characterId)
      } else {
        drafts.set(characterId, { initial: copyCharacterForm(initial), values: copyCharacterForm(values) })
      }
    },
    recall(characterId: number | null): CharacterFormDraft | null {
      const draft = drafts.get(characterId)
      return draft ? { initial: copyCharacterForm(draft.initial), values: copyCharacterForm(draft.values) } : null
    },
    forget(characterId: number | null): void { drafts.delete(characterId) },
    clear(): void { drafts.clear() },
  }
}

export interface CharacterEditorTicket {
  projectId: number
  characterId: number | null
  generation: number
}

export interface CharacterLoadTicket extends CharacterEditorTicket {
  request: number
}

/** 防止角色切换、关闭重开或较慢的旧请求更新当前抽屉。 */
export function createCharacterRequestGate() {
  let generation = 0
  let request = 0
  let active: CharacterEditorTicket | null = null
  const isCurrent = (ticket: CharacterEditorTicket): boolean =>
    active !== null && ticket.generation === active.generation &&
    ticket.projectId === active.projectId && ticket.characterId === active.characterId
  return {
    open(projectId: number, characterId: number | null): CharacterEditorTicket {
      active = { projectId, characterId, generation: ++generation }
      request = 0
      return active
    },
    close(): void {
      active = null
      generation += 1
      request += 1
    },
    isCurrent,
    beginLoad(ticket: CharacterEditorTicket): CharacterLoadTicket | null {
      return isCurrent(ticket) ? { ...ticket, request: ++request } : null
    },
    isCurrentLoad(ticket: CharacterLoadTicket): boolean {
      return isCurrent(ticket) && ticket.request === request
    },
  }
}
