import { describe, expect, it, vi } from 'vitest'
import { readerParagraphs } from '../utils/reader'
import type { ReaderPassage } from '../utils/readerDialogue'
import { NARRATOR_ROLE, castReaderVoices, roleIdsForScript } from '../utils/readerVoices'
import { createReadAloud, shortSentences } from './useReadAloud'

const voice = (lang: string, localService: boolean, name = lang) => ({ voiceURI: name, name, lang, localService })
const local = voice('zh-CN', true)
const unavailable = [voice('zh-CN', false), voice('en-US', true)]

type Options = NonNullable<ReturnType<NonNullable<Parameters<typeof createReadAloud>[5]>>>

function fixture(text = '花开。花落。\n\n花开。', options?: Options) {
  let voices = [local]
  let rate = 1
  let current = true
  let nativePause = false
  const events = new EventTarget()
  const queued: TestUtterance[] = []
  class TestUtterance {
    constructor(public text: string) {}
    rate = 0
    pitch = 0
    volume = 0
    voice = null
    onstart!: () => void
    onend!: () => void
    onerror!: (event: { error: string }) => void
  }
  const speech = {
    paused: false, speaking: false,
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    getVoices: vi.fn(() => voices),
    speak: vi.fn((item: TestUtterance) => { queued.push(item); speech.speaking = true }),
    cancel: vi.fn(() => { speech.speaking = false }),
    resume: vi.fn(() => { speech.paused = false }),
    pause: vi.fn(() => { speech.paused = nativePause; speech.speaking = nativePause }),
  }
  const platform = { SpeechSynthesisUtterance: TestUtterance, speechSynthesis: speech } as unknown as Parameters<typeof createReadAloud>[0]
  const publish = vi.fn<Parameters<typeof createReadAloud>[4]>()
  const catalog = vi.fn<NonNullable<Parameters<typeof createReadAloud>[6]>>()
  const player = createReadAloud(platform, readerParagraphs(text), () => current, () => rate, publish, () => options, catalog)
  return {
    speech, queued, player, publish, catalog, nativePause: () => { nativePause = true },
    setRate: (value: number) => { rate = value }, revoke: () => { current = false },
    state: () => publish.mock.lastCall?.[0],
    voices: (next: typeof voices, notify = true) => {
      voices = next
      if (notify) events.dispatchEvent(new Event('voiceschanged'))
    },
  }
}

describe('逐句朗读', () => {
  it('保留断句符号和空白，超长无标点段按完整码点切分', () => {
    const text = '开头。 ' + '甲'.repeat(118) + '🌸尾！完'
    const parts = [...shortSentences(text)]
    expect(parts).toEqual(['开头。', ' ' + '甲'.repeat(118) + '🌸', '尾！', '完'])
    expect(parts.join('')).toBe(text)
    expect([...shortSentences('“回来了？！”她点头。')]).toEqual(['“回来了？！”', '她点头。'])
  })

  it('有用户操作才排一句；设备开始发声时定位原段，章末清除', () => {
    const f = fixture()
    expect(f.queued).toHaveLength(0)
    f.player.start()
    f.player.start()
    expect(f.queued).toHaveLength(1)
    expect(f.state()).toMatchObject({ status: 'waiting', activeStart: null })
    f.queued[0].onstart()
    expect(f.state()).toMatchObject({ status: 'playing', activeStart: 0 })
    f.setRate(1.5)
    f.queued[0].onend()
    expect(f.queued[1]).toMatchObject({ text: '花落。', rate: 1.5, voice: local })
    f.queued[0].onend()
    expect(f.queued).toHaveLength(2)
    f.queued[1].onend()
    f.queued[2].onstart()
    expect(f.state()?.activeStart).toBe(8)
    f.queued[2].onend()
    expect(f.state()).toMatchObject({ status: 'idle', activeStart: null, message: null, activeRole: null, previewing: false })
  })

  it('没有本机中文声音时绝不提交；列表变化仅允许手动再试', () => {
    const f = fixture()
    for (const voices of [[], unavailable]) {
      f.voices(voices)
      f.player.start()
      expect(f.state()?.message).toContain('安装中文语音')
      expect(f.queued).toHaveLength(0)
    }
    f.voices([...unavailable, local])
    expect(f.state()?.message).toBeNull()
    expect(f.queued).toHaveLength(0)
    f.player.start()
    expect(f.queued[0].voice).toBe(local)
    f.voices(unavailable, false)
    f.queued[0].onend()
    expect(f.state()?.message).toContain('安装中文语音')
    expect(f.queued).toHaveLength(1)
  })

  it('声音移除立即停止，错误事件清空高亮并可手动重试', () => {
    const f = fixture()
    f.player.start()
    f.queued[0].onstart()
    f.voices(unavailable)
    expect(f.speech.cancel).toHaveBeenCalledOnce()
    expect(f.state()?.activeStart).toBeNull()
    f.voices([local])
    f.player.start()
    f.queued[1].onerror({ error: 'synthesis-failed' })
    expect(f.state()).toMatchObject({ status: 'idle', message: expect.stringContaining('重新'), activeStart: null })
    f.player.start()
    expect(f.queued[2].text).toBe('花开。')
  })

  it('原生暂停从原位置继续，下一句才使用新语速；停止会释放引擎暂停', () => {
    const f = fixture()
    f.nativePause()
    f.player.start()
    f.queued[0].onstart()
    f.player.pause()
    expect(f.state()).toMatchObject({ status: 'paused', activeStart: 0 })
    f.setRate(2)
    f.player.resume()
    expect(f.speech.resume).toHaveBeenCalledOnce()
    expect(f.queued).toHaveLength(1)
    expect(f.queued[0].rate).toBe(1)
    f.queued[0].onend()
    expect(f.queued[1].rate).toBe(2)
    f.queued[1].onstart()
    f.player.pause()
    f.player.stop()
    expect(f.speech.paused).toBe(false)
    expect(f.state()?.activeStart).toBeNull()
  })

  it('暂停未保留位置时只重读当前句，忽略被取消和被停止的迟到事件', () => {
    const f = fixture()
    f.player.start()
    f.queued[0].onend()
    f.queued[1].onstart()
    f.player.pause()
    f.setRate(0.75)
    f.queued[1].onend()
    expect(f.queued).toHaveLength(2)
    expect(f.state()).toMatchObject({ status: 'paused', activeStart: 0 })
    f.player.resume()
    expect(f.queued[2]).toMatchObject({ text: '花落。', rate: 0.75 })
    f.player.stop()
    f.player.start()
    f.queued[2].onstart()
    f.queued[2].onerror({ error: 'interrupted' })
    f.queued[2].onend()
    expect(f.queued.map((item) => item.text)).toEqual(['花开。', '花落。', '花落。', '花开。'])
    expect(f.state()).toMatchObject({ status: 'waiting', activeStart: null })
  })

  it('退出、失权、替换快照后旧事件不会继续发声', () => {
    const f = fixture()
    f.player.start()
    f.revoke()
    f.queued[0].onend()
    f.player.start()
    expect(f.state()?.status).toBe('idle')
    expect(f.queued).toHaveLength(1)

    const old = fixture()
    old.player.start()
    old.queued[0].onstart()
    old.player.pause()
    old.player.dispose()
    old.queued[0].onstart()
    old.queued[0].onend()
    old.player.start()
    expect(old.queued).toHaveLength(1)
  })

  it('不支持时提示更换浏览器，空章节保持安静', () => {
    const publish = vi.fn()
    const player = createReadAloud({}, readerParagraphs('正文'), () => true, () => 1, publish)
    player.start()
    expect(publish).toHaveBeenLastCalledWith(expect.objectContaining({ message: expect.stringContaining('浏览器') }))
    const f = fixture('  \n ')
    f.player.start()
    expect(f.queued).toHaveLength(0)
  })
})


const passages: ReaderPassage[] = [
  { kind: 'narration', start: 0, end: 2, paragraphStart: 0, text: '入夜', speaker: null, emotion: 'neutral' },
  { kind: 'dialogue', start: 2, end: 6, paragraphStart: 0, text: '回来。快！', speaker: '阿云', emotion: 'afraid' },
]
const otherVoice = voice('zh-CN', true, '本机女声')
const options = (): Options => ({ roleVoices: {}, expressive: true, script: passages })

describe('角色配音与试听', () => {
  it('只选本机中文，旁白和角色轮用音色、叠加全局语速和情绪', () => {
    const f = fixture('', options())
    f.voices([...unavailable, local, otherVoice])
    const choices = f.catalog.mock.lastCall![0]
    expect(choices.map((v) => v.id)).toEqual([local.voiceURI, otherVoice.voiceURI])
    expect(castReaderVoices(choices, roleIdsForScript(passages), {}).get('阿云')?.voiceId).toBe(otherVoice.voiceURI)
    f.setRate(1.5)
    f.player.start()
    expect(f.queued[0]).toMatchObject({ voice: local, rate: 1.5, pitch: 1, volume: 1 })
    f.queued[0].onend()
    expect(f.queued[1].voice).toBe(otherVoice)
    expect(f.queued[1].pitch).toBeGreaterThan(1)
    expect(f.queued[1].rate).toBeGreaterThan(1.5)
    expect(f.queued[1].volume).toBeLessThan(1)
    f.queued[1].onstart()
    expect(f.state()).toMatchObject({ activeRole: '阿云', activeStart: 0, activePassageStart: 2, activeEmotion: 'afraid' })
  })

  it('只有一个声音时人物与旁白音高有别；不使用情绪时保持基本语速与音量', () => {
    const config = { ...options(), expressive: false }
    const f = fixture('', config)
    f.player.start()
    f.queued[0].onend()
    expect(f.queued[1]).toMatchObject({ voice: local, rate: 1, volume: 1 })
    expect(f.queued[1].pitch).not.toBe(f.queued[0].pitch)
    f.queued[1].onend()
    expect(f.queued[2].pitch).toBe(f.queued[1].pitch)
  })

  it('自动分配保留本次播放的声音，设备重排序不交换角色；缺失的指定声音不换成其他声音', () => {
    const config = options()
    const f = fixture('', config)
    f.voices([local, otherVoice])
    f.player.start()
    f.voices([otherVoice, local])
    f.queued[0].onend()
    expect(f.queued[1].voice).toBe(otherVoice)
    f.voices([local])
    expect(f.state()).toMatchObject({ status: 'idle', message: expect.stringContaining('重新选择') })
    config.roleVoices = { [NARRATOR_ROLE]: otherVoice.voiceURI }
    f.player.start()
    expect(f.queued).toHaveLength(2)
    expect(f.state()?.message).toContain('重新选择')
    config.roleVoices = { [NARRATOR_ROLE]: local.voiceURI }
    f.player.start()
    expect(f.queued[2].voice).toBe(local)
  })

  it('固定试听不读稿件；实际台词试听只读选定段，替换或开播使旧事件失效', () => {
    const f = fixture('', options())
    f.player.preview(local.voiceURI, 'angry', '阿云')
    expect(f.queued[0].text).toBe('你好，让我们一起听听这个声音。')
    expect(f.state()).toMatchObject({ status: 'idle', previewing: true, activeStart: null })
    f.player.previewPassage(2)
    f.queued[0].onend()
    expect(f.queued[1]).toMatchObject({ text: '回来。', voice: local })
    f.queued[1].onstart()
    expect(f.state()?.activeStart).toBeNull()
    f.queued[1].onend()
    expect(f.queued[2].text).toBe('快！')
    f.queued[2].onend()
    expect(f.state()?.previewing).toBe(false)
    f.player.previewPassage(10)
    expect(f.queued).toHaveLength(3)
    f.player.previewPassage(2)
    f.player.start()
    f.queued[3].onstart()
    f.queued[3].onend()
    expect(f.queued[4].text).toBe('入夜')
    f.player.previewPassage(2)
    expect(f.queued).toHaveLength(5)
  })

  it('试听遇到撤权、所选声音撤下或只有远程声音时停止', () => {
    const f = fixture('', options())
    f.voices([local, otherVoice])
    f.player.preview(otherVoice.voiceURI)
    f.voices([local])
    expect(f.state()?.previewing).toBe(false)
    expect(f.state()?.message).toContain('重新选择')
    f.voices(unavailable)
    f.player.preview(otherVoice.voiceURI)
    expect(f.state()?.message).toContain('安装中文语音')
    f.voices([local])
    f.player.previewPassage(2)
    f.revoke()
    f.queued[1].onend()
    expect(f.state()?.previewing).toBe(false)
    expect(f.queued).toHaveLength(2)
  })
})

describe('整章分配', () => {
  it('自动角色优先使用尚未分配的音色，用户指定相同声音时照选', () => {
    const config = options()
    config.roleVoices = { [NARRATOR_ROLE]: otherVoice.voiceURI }
    const f = fixture('', config)
    f.voices([local, otherVoice])
    f.player.start()
    expect(f.queued[0].voice).toBe(otherVoice)
    f.queued[0].onend()
    expect(f.queued[1].voice).toBe(local)
    config.roleVoices = { ...config.roleVoices, 阿云: otherVoice.voiceURI }
    f.queued[1].onend()
    expect(f.queued[2].voice).toBe(otherVoice)
  })

  it('同一声音下张三、李四、王五与旁白各有音高，试听与全章一致', () => {
    const names = ['张三', '李四', '王五']
    const script = [passages[0], ...names.map((speaker, i) => ({
      ...passages[1], text: '走。', start: 2 + 2 * i, end: 4 + 2 * i, speaker, emotion: 'neutral' as const,
    }))]
    const f = fixture('', { ...options(), script })
    const previews = names.map((name, i) => {
      f.player.preview(local.voiceURI, 'neutral', name)
      const sample = f.queued.at(-1)!
      sample.onend()
      f.player.previewPassage(script[i + 1].start)
      const cue = f.queued.at(-1)!
      expect(cue.pitch).toBe(sample.pitch)
      cue.onend()
      return sample.pitch
    })
    const index = f.queued.length
    f.player.start()
    for (let i = 0; i < script.length; i++) f.queued[index + i].onend()
    const pitches = f.queued.slice(index).map((item) => item.pitch)
    expect(pitches).toEqual([1, ...previews])
    expect(new Set(pitches).size).toBe(script.length)
  })
})
