import { useLayoutEffect, useRef, useState } from 'react'
import type { ReaderParagraph } from '../utils/reader'
import type { ReaderEmotion, ReaderPassage } from '../utils/readerDialogue'
import { localChineseVoices, NARRATOR_ROLE, readerRole, castReaderVoices, roleIdsForScript, voiceChoices, voiceDelivery, type ReaderVoice } from '../utils/readerVoices'

interface ReadAloudOptions {
  script: readonly ReaderPassage[]
  roleVoices: Readonly<Record<string, string>>
  expressive: boolean
}

interface PlaybackState {
  status: 'idle' | 'waiting' | 'playing' | 'paused'
  activeStart: number | null
  message: string | null
  activeRole: string | null
  activeEmotion: ReaderEmotion | null
  activePassageStart: number | null
  previewing: boolean
}

const idle: PlaybackState = {
  status: 'idle', activeStart: null, message: null,
  activeRole: null, activeEmotion: null, activePassageStart: null, previewing: false,
}
const installVoice = '请在系统语音设置中安装中文语音，然后重新点击朗读。'
const reselectVoice = '这个音色当前不可用，请在角色配音中重新选择本机中文音色。'
const unsupported = '当前浏览器不支持朗读，请使用支持语音合成的浏览器。'
const playbackFailed = '朗读中断，请重新点击朗读。'

/** 每次只向设备提交一句。超长无标点段按码点切分，保留原文。 */
export function* shortSentences(text: string) {
  let sentence = ''
  let length = 0
  let ended = false
  for (const point of text) {
    const punctuation = /[。！？!?；;.…\n]/u.test(point)
    // 小说对话中的连续句读和收引号随前句朗读，避免单独提交一个引号。
    if (length >= 120 || (ended && !punctuation && !/[”’」』）》】"']/u.test(point))) {
      yield sentence
      sentence = ''
      length = 0
      ended = false
    }
    sentence += point
    length += 1
    ended ||= punctuation
  }
  if (sentence) yield sentence
}

interface SpeechSentence {
  text: string
  start: number | null
  passageStart: number | null
  role: string
  emotion: ReaderEmotion
}

function* speechQueue(paragraphs: ReaderParagraph[], script?: readonly ReaderPassage[]): Generator<SpeechSentence, undefined> {
  const passages = script ?? paragraphs.map((p): ReaderPassage => ({
    ...p, paragraphStart: p.start, kind: 'narration', speaker: null, emotion: 'neutral',
  }))
  for (const passage of passages) {
    for (const text of shortSentences(passage.text)) {
      if (text.trim()) yield { text, start: passage.paragraphStart, passageStart: passage.start, role: readerRole(passage), emotion: passage.emotion }
    }
  }
}

type SpeechWindow = Pick<Window & typeof globalThis, 'speechSynthesis' | 'SpeechSynthesisUtterance'>

/** 一个控制器只持有一份已鉴权的正文快照，清理后所有语音回调均失效。 */
export function createReadAloud(
  platform: Partial<SpeechWindow>,
  paragraphs: ReaderParagraph[],
  sessionCurrent: () => boolean,
  rate: () => number,
  publish: (state: PlaybackState) => void,
  options: () => ReadAloudOptions | undefined = () => undefined,
  publishVoices?: (voices: ReaderVoice[]) => void,
) {
  const synth = platform.speechSynthesis
  const Utterance = platform.SpeechSynthesisUtterance
  let state = idle
  let disposed = false
  let utterance: SpeechSynthesisUtterance | null = null
  let queue: ReturnType<typeof speechQueue> | null = null
  let sentence: SpeechSentence | undefined
  let cast: ReaderVoice[] = []
  let roleIds: string[] = [NARRATOR_ROLE]
  let preview: { sampleVoiceId?: string } | null = null
  let mappings: ReadAloudOptions['roleVoices'] | undefined
  let assignments: ReturnType<typeof castReaderVoices> = new Map()

  function update(next: PlaybackState) {
    state = next
    if (!disposed) publish(next)
  }
  function cancelUtterance() {
    if (!utterance || !synth) return
    // cancel 可能同步或延后触发旧事件，先放弃旧对象。
    utterance = null
    synth.cancel()
    if (synth.paused) synth.resume()
  }
  function stop(message: string | null = null) {
    cancelUtterance()
    queue = null
    sentence = undefined
    preview = null
    update(message ? { ...idle, message } : idle)
  }
  function speak() {
    if (disposed || !sessionCurrent() || !sentence) return stop()
    if (!synth || !Utterance) return stop(unsupported)
    const { text, start, role, passageStart } = sentence
    try {
      const available = localChineseVoices(synth)
      if (!available.length) {
        publishVoices?.([])
        return stop(installVoice)
      }
      const config = options()
      if (mappings !== config?.roleVoices) {
        mappings = config?.roleVoices
        assignments = castReaderVoices(cast, roleIds, mappings ?? {})
      }
      const assigned = assignments.get(role)
      const id = preview?.sampleVoiceId ?? assigned?.voiceId
      const voice = available.find((item) => item.voiceURI === id)
      if (!voice) {
        publishVoices?.(voiceChoices(available))
        return stop(reselectVoice)
      }
      const emotion = preview?.sampleVoiceId !== undefined || config?.expressive ? sentence.emotion : 'neutral'
      const item = new Utterance(text)
      item.voice = voice
      item.lang = voice.lang
      Object.assign(item, voiceDelivery(rate(), emotion, assigned?.pitch ?? 1))
      utterance = item
      const current = () => {
        if (disposed || utterance !== item) return false
        if (sessionCurrent()) return true
        stop()
        return false
      }
      item.onstart = () => {
        if (current() && preview === null) update({
          ...idle, status: 'playing', activeStart: start, activeRole: role, activeEmotion: emotion, activePassageStart: passageStart,
        })
      }
      item.onend = () => {
        if (!current()) return
        if (state.status === 'paused') return cancelUtterance()
        utterance = null
        sentence = queue?.next().value
        speak()
      }
      item.onerror = (event) => {
        if (!current()) return
        if (state.status === 'paused' && (event.error === 'canceled' || event.error === 'interrupted')) return cancelUtterance()
        const unavailable = event.error === 'voice-unavailable' || event.error === 'language-unavailable'
        stop(unavailable ? (localChineseVoices(synth).length ? reselectVoice : installVoice) : playbackFailed)
      }
      update(preview !== null ? { ...idle, previewing: true } : { ...state, status: 'waiting', message: null })
      synth.speak(item)
    } catch {
      stop(playbackFailed)
    }
  }
  function refreshVoices() {
    if (!synth || !Utterance || disposed) return
    const voices = localChineseVoices(synth)
    publishVoices?.(voiceChoices(voices))
    const voice = utterance?.voice
    if ((queue || utterance) && !voices.length) {
      stop(installVoice)
    } else if (voice && !voices.some((v) => v.voiceURI === voice.voiceURI && v.lang === voice.lang)) {
      stop(reselectVoice)
    } else if (voices.length && state.message === installVoice) {
      update(idle)
    }
  }

  function prepare() {
    if (disposed || state.status !== 'idle' || !sessionCurrent()) return false
    if (preview) stop()
    const config = options()
    const script = config?.script
    cast = synth ? voiceChoices(localChineseVoices(synth)) : []
    publishVoices?.(cast)
    roleIds = script ? roleIdsForScript(script) : [NARRATOR_ROLE]
    mappings = config?.roleVoices
    assignments = castReaderVoices(cast, roleIds, mappings ?? {})
    return true
  }

  synth?.addEventListener('voiceschanged', refreshVoices)
  publish(idle)
  if (synth && Utterance) refreshVoices()
  else publishVoices?.([])
  return {
    start() {
      if (!prepare()) return
      queue = speechQueue(paragraphs, options()?.script)
      sentence = queue.next().value
      speak()
    },
    preview(voiceId: string, emotion: ReaderEmotion = 'neutral', role = NARRATOR_ROLE) {
      if (!prepare()) return
      preview = { sampleVoiceId: voiceId }
      sentence = { text: '你好，让我们一起听听这个声音。', start: null, passageStart: null, role, emotion }
      speak()
    },
    previewPassage(start: number) {
      const passage = options()?.script.find((p) => p.start === start)
      if (!passage || !prepare()) return
      preview = {}
      queue = speechQueue([], [passage])
      sentence = queue.next().value
      speak()
    },
    pause() {
      if (state.status !== 'playing' && state.status !== 'waiting') return
      const started = state.status === 'playing'
      update({ ...state, status: 'paused' })
      // 有的设备用结束当前句模拟暂停。只在引擎确实保留了发声位置时原位继续。
      if (started && synth) {
        synth.pause()
        if (synth.paused && synth.speaking) return
      }
      cancelUtterance()
    },
    resume() {
      if (state.status !== 'paused') return
      if (!sessionCurrent()) return stop()
      refreshVoices()
      if (!queue) return
      if (utterance && synth?.paused && synth.speaking) {
        update({ ...state, status: 'playing' })
        synth.resume()
      } else {
        cancelUtterance()
        speak()
      }
    },
    stop: () => stop(),
    dispose() {
      disposed = true
      synth?.removeEventListener('voiceschanged', refreshVoices)
      stop()
    },
  }
}

export function useReadAloud(paragraphs: ReaderParagraph[], snapshotKey: string, rate: number, sessionCurrent: () => boolean, options?: ReadAloudOptions) {
  const [state, setState] = useState(idle)
  const [voices, setVoices] = useState<ReaderVoice[]>([])
  const script = options?.script
  const settings = useRef({ rate, sessionCurrent, options })
  const player = useRef<ReturnType<typeof createReadAloud> | null>(null)
  useLayoutEffect(() => { settings.current = { rate, sessionCurrent, options } }, [rate, sessionCurrent, options])
  useLayoutEffect(() => {
    const current = createReadAloud(
      window, paragraphs, () => settings.current.sessionCurrent(), () => settings.current.rate,
      setState, () => settings.current.options, setVoices,
    )
    player.current = current
    window.addEventListener('pagehide', current.stop)
    return () => {
      window.removeEventListener('pagehide', current.stop)
      player.current = null
      current.dispose()
    }
  }, [paragraphs, snapshotKey, script])
  return {
    ...state, voices,
    preview: (voiceId: string, emotion?: ReaderEmotion, role?: string) => player.current?.preview(voiceId, emotion, role),
    previewPassage: (start: number) => player.current?.previewPassage(start),
    start: () => player.current?.start(),
    pause: () => player.current?.pause(),
    resume: () => player.current?.resume(),
    stop: () => player.current?.stop(),
  }
}
