import type { ReaderParagraph } from './reader'

export const READER_EMOTIONS = ['neutral', 'happy', 'angry', 'afraid', 'sad', 'whisper'] as const
export type ReaderEmotion = (typeof READER_EMOTIONS)[number]
export const READER_EMOTION_LABELS: Record<ReaderEmotion, string> = {
  neutral: '平静', happy: '高兴', angry: '生气', afraid: '害怕', sad: '悲伤', whisper: '轻声',
}

export interface ReaderPassage {
  start: number
  end: number
  paragraphStart: number
  text: string
  kind: 'narration' | 'dialogue'
  speaker: string | null
  emotion: ReaderEmotion
}

const closeQuote = new Map([['“', '”'], ['「', '」'], ['『', '』'], ['‘', '’'], ['"', '"']])

const emotionPatterns: [ReaderEmotion, RegExp][] = [
  ['whisper', /压低(?:了)?(?:嗓音|声音)|轻声|悄声|低声|耳语|小声/],
  ['angry', /生气|愤怒|怒(?:气冲冲|吼|喝|斥|道|喊|骂)|恼火|气愤|气死我/],
  ['afraid', /害怕|惊恐|恐惧|惶恐|发抖|颤(?:声|抖)|胆战心惊/],
  ['sad', /悲伤|难过|伤心|哽咽|抽泣|啜泣|哭(?:着|喊|诉|道)/],
  ['happy', /高兴|开心|兴奋|欣喜|雀跃|欢呼|喜悦|笑(?:着|道)|太好[啦了]/],
]

function readEmotion(text: string): ReaderEmotion {
  for (const [emotion, pattern] of emotionPatterns) {
    const match = pattern.exec(text)
    if (match && !/(?:不|没|别|并非|不是|不用|并不|一点也不)$/.test(text.slice(0, match.index))) return emotion
  }
  return 'neutral'
}

// 把引语提示作为完整短语识别，避免将说话对象或一句普通叙述猜成角色名。
const speechVerb = /(?:回答(?:道)?|答(?:道)?|问(?:道)?|说(?:道)?|喊(?:道)?|吼(?:道)?|叫(?:道)?|嘀咕(?:道)?|提醒(?:道)?|反驳(?:道)?|回应(?:道)?|道)$/
const modifier = /(?:压低(?:了)?(?:嗓音|声音)|气冲冲|怒气冲冲|兴高采烈|胆战心惊|(?:生气|愤怒|恼火|气愤|高兴|开心|兴奋|欣喜|害怕|惊恐|惶恐|悲伤|难过|伤心|哽咽|颤抖|笑|哭|急切|平静|冷冷|缓缓|轻轻)(?:地|着)?|轻声|低声|小声|悄声|颤声|大声|厉声|怒)$/u
function removeModifiers(text: string) {
  let remaining = text
  for (;;) {
    const shorter = remaining.replace(modifier, '')
    if (shorter === remaining) return remaining
    remaining = shorter
  }
}

const unknownSubject = /^(?:[他她它你我您]|有人|众人|大家|人群|突然|忽然|只听|只见|听见|听到|看着|望着|那个|这个|对方|一个|只好|连忙|随即|随后|然后|又|也|终于|忍不住|不禁|一边|两人|两个人)/u
const nameForm = /^(?:[\p{Script=Han}]{1,4}|[A-Z][a-zA-Z'-]{1,24})$/u

function attribution(clause: string, names: readonly string[]) {
  if (clause.length > 100) return null
  if (/^(?:据|听|传|话|谁)说$/u.test(clause)) return null
  const verb = speechVerb.exec(clause)
  if (!verb) return null
  let subject = clause.slice(0, verb.index).trim()
  // 先取语境明确的书中名字，以免「高兴」等真实姓名被误当修饰词。
  const known = names.find((name) => subject.startsWith(name) && (
    subject === name || /^(?:对|向|冲|朝|跟)/.test(subject.slice(name.length)) ||
    removeModifiers(subject.slice(name.length)) === ''
  ))
  subject = removeModifiers(subject).replace(/(?:对|向|冲|朝|跟)[\p{Script=Han}]{1,8}$/u, '').trim()
  const speaker = known ?? (nameForm.test(subject) && !unknownSubject.test(subject) ? subject : null)
  return { speaker, emotion: readEmotion(known ? clause.slice(known.length) : clause) }
}

function beforeTag(text: string, names: readonly string[]) {
  const clause = text.trimEnd().replace(/[：:,，]\s*$/, '').split(/[，,。！？!?；;：:\n]/u).pop()?.trim()
  return clause ? attribution(clause, names) : null
}

function afterTag(text: string, names: readonly string[]) {
  // 「甲说：“下一句”」介绍的是后面的对白，不能归给前面的对白。
  const match = /^\s*[，,]?\s*([^，,。！？!?；;：:\n]+)([，,。！？!?；;：:\n]|$)/u.exec(text)
  return match && !/[：:]/.test(match[2]) ? attribution(match[1].trim(), names) : null
}

/** 每段保留自身的码点区间和段落锚点，旁白与对白拼接后逐字还原原文。 */
export function buildReaderScript(paragraphs: readonly ReaderParagraph[], characterNames: readonly string[] = []): ReaderPassage[] {
  const names = [...new Set(characterNames.map((name) => name.trim()).filter(Boolean))].sort((a, b) => b.length - a.length)
  const passages: ReaderPassage[] = []
  const closers: string[] = []
  const dialogueGroups: ReaderPassage[][] = []
  for (const paragraph of paragraphs) {
    const points = Array.from(paragraph.text)
    let start = 0
    const append = (end: number, kind: ReaderPassage['kind']) => {
      if (start === end) return
      const passage: ReaderPassage = {
        start: paragraph.start + start, end: paragraph.start + end, paragraphStart: paragraph.start,
        text: points.slice(start, end).join(''), kind, speaker: null, emotion: 'neutral',
      }
      passages.push(passage)
      if (kind === 'dialogue') dialogueGroups[dialogueGroups.length - 1].push(passage)
      start = end
    }
    for (let i = 0; i < points.length; i++) {
      const point = points[i]
      if (closers.length && closers[closers.length - 1] === point) {
        closers.pop()
        if (!closers.length) append(i + 1, 'dialogue')
      } else {
        const closer = closeQuote.get(point)
        if (closer) {
          if (!closers.length) {
            append(i, 'narration')
            dialogueGroups.push([])
          }
          closers.push(closer)
        }
      }
    }
    append(points.length, closers.length ? 'dialogue' : 'narration')
  }

  // 整组对白可跨段落；组内分段各自跟随正文高亮。
  const positions = new Map(passages.map((passage, i) => [passage.start, i]))
  for (const group of dialogueGroups) {
    const first = group[0], last = group[group.length - 1]
    const before = passages[positions.get(first.start)! - 1]
    const after = passages[positions.get(last.start)! + 1]
    const prefix = before?.kind === 'narration' && (before.paragraphStart === first.paragraphStart || /[：:]\s*$/.test(before.text))
      ? beforeTag(before.text, names) : null
    const suffix = after?.kind === 'narration' && after.paragraphStart === last.paragraphStart
      ? afterTag(after.text, names) : null
    const tag = prefix ?? suffix
    for (const passage of group) {
      passage.speaker = tag?.speaker ?? null
      passage.emotion = tag && tag.emotion !== 'neutral' ? tag.emotion : readEmotion(passage.text)
    }
  }
  return passages
}
