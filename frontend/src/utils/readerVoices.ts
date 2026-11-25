import type { ReaderEmotion, ReaderPassage } from './readerDialogue'

export const NARRATOR_ROLE = '@narrator'
export const DIALOGUE_ROLE = '@dialogue'

export interface ReaderVoice { id: string; name: string; lang: string }

export function localChineseVoices(synth: SpeechSynthesis) {
  return synth.getVoices().filter((voice) => voice.localService === true && /^(zh|cmn|yue)(?:[-_]|$)/i.test(voice.lang))
}

export function voiceChoices(voices: SpeechSynthesisVoice[]): ReaderVoice[] {
  const byId = new Map<string, ReaderVoice>()
  for (const { voiceURI: id, name, lang } of voices) {
    if (!byId.has(id)) byId.set(id, { id, name, lang })
  }
  return Array.from(byId.values())
}

export function readerRole(passage: Pick<ReaderPassage, 'kind' | 'speaker'>): string {
  return passage.kind === 'narration' ? NARRATOR_ROLE : passage.speaker || DIALOGUE_ROLE
}

export function readerRoleLabel(role: string) {
  if (role === NARRATOR_ROLE) return '旁白'
  if (role === DIALOGUE_ROLE) return '未指定角色'
  return role
}

export function roleIdsForScript(script: readonly ReaderPassage[]): string[] {
  const roles = new Set(script.map(readerRole))
  const named = [...roles].filter((role) => role !== NARRATOR_ROLE && role !== DIALOGUE_ROLE)
  return [NARRATOR_ROLE, ...(roles.has(DIALOGUE_ROLE) ? [DIALOGUE_ROLE] : []), ...named]
}

interface AssignedVoice { voiceId?: string; pitch: number }

const rolePitches = [1.09, 0.91, 1.06, 0.94, 1.12, 0.88, 1.03, 0.97]

function rolePitch(role: string, occupied: Set<number>) {
  let hash = 0
  for (const point of role) hash = (hash * 31 + point.codePointAt(0)!) >>> 0
  const preferred = hash % rolePitches.length
  for (let offset = 0; offset < rolePitches.length; offset++) {
    const pitch = rolePitches[(preferred + offset) % rolePitches.length]
    if (!occupied.has(pitch)) return pitch
  }
  return rolePitches[preferred]
}

/** 先保留每个手选音色，再把自动角色分到使用最少的设备声音上。缺失的手选 URI 留给界面提示重选。 */
export function castReaderVoices(
  voices: readonly ReaderVoice[], roles: readonly string[], preferences: Readonly<Record<string, string>>,
): Map<string, AssignedVoice> {
  const assigned = new Map<string, AssignedVoice>()
  const counts = new Map<string, number>()
  function assign(role: string, id: string | undefined) {
    assigned.set(role, { voiceId: id, pitch: 1 })
    if (id !== undefined) counts.set(id, (counts.get(id) ?? 0) + 1)
  }

  for (const role of roles) {
    if (Object.hasOwn(preferences, role) && preferences[role]) assign(role, preferences[role])
  }
  for (const [index, role] of roles.entries()) {
    if (assigned.has(role)) continue
    if (!voices.length) {
      assign(role, undefined)
      continue
    }
    let selected = voices[index % voices.length]
    // 同样空闲时保留本来的轮换顺序，只有已被占用时才需要比较其他候选。
    for (let i = 1; i < voices.length && counts.has(selected.id); i++) {
      const candidate = voices[(index + i) % voices.length]
      if ((counts.get(candidate.id) ?? 0) < counts.get(selected.id)!) selected = candidate
    }
    assign(role, selected.id)
  }

  if (voices.length < roles.length) {
    const usedPitches = new Map<string, Set<number>>()
    for (const role of roles) {
      const entry = assigned.get(role)!
      const id = entry.voiceId
      if (role === NARRATOR_ROLE || id === undefined || (counts.get(id) ?? 0) < 2) continue
      const occupied = usedPitches.get(id) ?? new Set<number>()
      entry.pitch = rolePitch(role, occupied)
      occupied.add(entry.pitch)
      usedPitches.set(id, occupied)
    }
  }
  return assigned
}

const expression: Record<ReaderEmotion, { rate: number; pitch: number; volume: number }> = {
  neutral: { rate: 1, pitch: 1, volume: 1 },
  happy: { rate: 1.08, pitch: 1.13, volume: 1 },
  angry: { rate: 1.12, pitch: 0.94, volume: 1 },
  afraid: { rate: 1.07, pitch: 1.12, volume: 0.84 },
  sad: { rate: 0.88, pitch: 0.92, volume: 0.88 },
  whisper: { rate: 0.93, pitch: 0.97, volume: 0.64 },
}

export function voiceDelivery(rate: number, emotion: ReaderEmotion, rolePitch: number) {
  const style = expression[emotion]
  return { rate: rate * style.rate, pitch: rolePitch * style.pitch, volume: style.volume }
}
