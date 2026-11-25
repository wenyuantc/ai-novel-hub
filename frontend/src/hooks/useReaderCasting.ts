import { useMemo, useSyncExternalStore } from 'react'
import type { ReaderAICastingResult } from '../api/reader'
import { READER_EMOTIONS, type ReaderEmotion, type ReaderPassage } from '../utils/readerDialogue'

export interface ReaderCastOverride {
  speaker?: string
  emotion?: ReaderEmotion
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function parseVoice(value: unknown) {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

function parseOverride(value: unknown): ReaderCastOverride | undefined {
  if (!object(value)) return
  const result: ReaderCastOverride = {}
  if (typeof value.speaker === 'string' && value.speaker.length <= 64) result.speaker = value.speaker
  if (READER_EMOTIONS.some((emotion) => emotion === value.emotion)) result.emotion = value.emotion as ReaderEmotion
  return Object.keys(result).length ? result : undefined
}

/** 本机缓存仅保存角色、情绪、位置和音色 ID，丢弃过期或损坏的数据。 */
function parseAiResult(raw: unknown, hash: string): ReaderAICastingResult | undefined {
  if (!hash || !object(raw) || raw.content_hash !== hash || !Array.isArray(raw.cues) || !Array.isArray(raw.voices)) return
  const cues: ReaderAICastingResult['cues'] = []
  const voices: ReaderAICastingResult['voices'] = []
  const starts = new Set<number>()
  const speakers = new Set<string>()
  for (const item of raw.cues) {
    if (!object(item) || typeof item.start !== 'number' || !Number.isSafeInteger(item.start)
      || item.start < 0 || starts.has(item.start)) return
    if (item.speaker !== null && (typeof item.speaker !== 'string' || !item.speaker.trim())) return
    const emotion = READER_EMOTIONS.find((e) => e === item.emotion)
    if (!emotion) return
    starts.add(item.start)
    cues.push({ start: item.start, speaker: item.speaker, emotion })
  }
  for (const item of raw.voices) {
    if (!object(item) || typeof item.speaker !== 'string' || !item.speaker.trim() || speakers.has(item.speaker)
      || typeof item.voice_id !== 'string' || !item.voice_id) return
    speakers.add(item.speaker)
    voices.push({ speaker: item.speaker, voice_id: item.voice_id })
  }
  return { content_hash: hash, cues, voices }
}

/** AI 只能改变已有台词的归属和情绪。正文及其段落位置取自当前已鉴权的章节。 */
export function applyReaderAiCues(detected: ReaderPassage[], result: ReaderAICastingResult | null): ReaderPassage[] {
  if (!result) return detected
  const cues = new Map(result.cues.map((cue) => [cue.start, cue]))
  return detected.map((passage) => {
    const ai = passage.kind === 'dialogue' && cues.get(passage.start)
    return ai ? { ...passage, speaker: ai.speaker, emotion: ai.emotion } : passage
  })
}

export function applyReaderCastOverrides(inferred: ReaderPassage[], overrides: Readonly<Record<string, ReaderCastOverride>>) {
  return inferred.map((passage) => {
    const override = passage.kind === 'dialogue' && overrides[String(passage.start)]
    if (!override) return passage
    return { ...passage, speaker: override.speaker ?? passage.speaker, emotion: override.emotion ?? passage.emotion }
  })
}

/** 读取旧偏好时校验版本和内容。写入前重新读取，保留同书其他标签页的修改。 */
export function castingStore<T>(key: string, version: string, parse: (value: unknown) => T | undefined) {
  let values: Record<string, T> = Object.create(null)
  let inMemoryOnly = false
  function load() {
    if (inMemoryOnly) return
    try {
      const raw = localStorage.getItem(key)
      const data: unknown = raw && JSON.parse(raw)
      const next: Record<string, T> = Object.create(null)
      if (object(data) && data.version === version && object(data.values)) {
        for (const [id, value] of Object.entries(data.values)) {
          const parsed = parse(value)
          if (parsed !== undefined) next[id] = parsed
        }
      }
      values = next
    } catch { /* 存储不可用时保留当前页的设置。 */ }
  }
  load()
  const listeners = new Set<() => void>()
  function notify() { for (const listener of listeners) listener() }
  return {
    snapshot: () => values,
    subscribe(listener: () => void) {
      listeners.add(listener)
      const sync = (event: StorageEvent) => {
        if (event.key === key || event.key === null) { load(); notify() }
      }
      window.addEventListener('storage', sync)
      return () => { listeners.delete(listener); window.removeEventListener('storage', sync) }
    },
    set(id: string, value: T | undefined) {
      load()
      values = Object.assign(Object.create(null), values)
      if (value === undefined) delete values[id]
      else values[id] = value
      try { localStorage.setItem(key, JSON.stringify({ version, values })) } catch { inMemoryOnly = true }
      notify()
    },
  }
}

export function useReaderCasting(actorId: number, projectId: number, chapterId: number, contentHash: string) {
  const prefix = `ainovel.reader.casting.${actorId}.${projectId}`
  const voiceStore = useMemo(() => castingStore(prefix, '1', parseVoice), [prefix])
  const overrideStore = useMemo(() => castingStore(`${prefix}.${chapterId}.${contentHash}`, '1', parseOverride), [prefix, chapterId, contentHash])
  const aiStore = useMemo(() => castingStore(`${prefix}.${chapterId}.${contentHash}.ai`, '1', (raw) => parseAiResult(raw, contentHash)),
    [prefix, chapterId, contentHash])
  const saved = useSyncExternalStore(aiStore.subscribe, aiStore.snapshot, aiStore.snapshot)
  const aiResult = Object.hasOwn(saved, 'result') ? saved.result : null
  const voices = useSyncExternalStore(voiceStore.subscribe, voiceStore.snapshot, voiceStore.snapshot)
  const overrides = useSyncExternalStore(overrideStore.subscribe, overrideStore.snapshot, overrideStore.snapshot)
  const effectiveVoices = useMemo(() => {
    if (!aiResult) return voices
    const merged: Record<string, string> = Object.create(null)
    for (const { speaker, voice_id } of aiResult.voices) merged[speaker] = voice_id
    return Object.assign(merged, voices)
  }, [aiResult, voices])
  return {
    voices, effectiveVoices, overrides, aiResult,
    saveAiResult(result: ReaderAICastingResult) {
      const parsed = parseAiResult(result, contentHash)
      if (!parsed) return false
      aiStore.set('result', parsed)
      return true
    },
    setVoice: (speaker: string, voiceURI: string) => voiceStore.set(speaker, parseVoice(voiceURI)),
    setOverride: (start: number, override: ReaderCastOverride) => overrideStore.set(String(start), parseOverride(override)),
  }
}
