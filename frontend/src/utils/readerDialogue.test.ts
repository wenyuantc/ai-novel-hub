import { describe, expect, it } from 'vitest'
import { readerParagraphs } from './reader'
import { buildReaderScript } from './readerDialogue'

const parse = (text: string, names?: string[]) => buildReaderScript(readerParagraphs(text), names)
const dialogue = (text: string, names?: string[]) => parse(text, names).filter((p) => p.kind === 'dialogue')

describe('本机对白分段与说话提示', () => {
  it('分开同段旁白、连续对白和引语后的角色提示', () => {
    const text = '🌙林夏走来。林夏压低声音说：“别出声。” “好。”周宁笑着答道。'
    const passages = parse(text)
    expect(passages.map((p) => p.text).join('')).toBe(text)
    expect(passages.filter((p) => p.kind === 'dialogue').map((p) => [p.speaker, p.emotion]))
      .toEqual([['林夏', 'whisper'], ['周宁', 'happy']])
    for (const p of passages) expect(Array.from(text).slice(p.start, p.end).join('')).toBe(p.text)
  })
  it('对话对象、代词和缺少提示的对白不凭空指定角色', () => {
    expect(dialogue('林夏对周宁轻声说：“到了。”\n她对林夏说：“好。”\n“坐。”', ['周宁', '林夏'])
      .map((p) => p.speaker)).toEqual(['林夏', null, null])
    expect(dialogue('“甲。”林夏怒喊：“乙！”').map((p) => [p.speaker, p.emotion]))
      .toEqual([[null, 'neutral'], ['林夏', 'angry']])
  })
  it('情绪提示取自说话语境，保留未指明或否定的情绪', () => {
    expect(dialogue('“开门。”林夏颤声道。\n“我很高兴！”\n“我不害怕。”\n“别走。”她哽咽着说。')
      .map((p) => p.emotion)).toEqual(['afraid', 'happy', 'neutral', 'sad'])
  })
  it('书中角色按最长名匹配，并允许姓名本身包含情绪词', () => {
    expect(dialogue('高兴说道：「出发。」\n欧阳灵雪儿开心地问：“去哪？”', ['高兴', '欧阳', '欧阳灵雪儿'])
      .map((p) => [p.speaker, p.emotion])).toEqual([['高兴', 'neutral'], ['欧阳灵雪儿', 'happy']])
  })
  it('传闻和泛指的说法不会产生虚构角色，未知归属可留给读者分配', () => {
    expect(dialogue('据说：“城里下雨了。”\n听说：“真的？”\n有人说：“很快。”\n大家都说：“会好起来的。”')
      .map((p) => p.speaker)).toEqual([null, null, null, null])
  })
  it('保留嵌套引号与跨段对白、各段重复台词的码点位置', () => {
    const text = '🌸。林夏说：\r\n「你听到『回来』了吗？\n回来。」\n"回来。"'
    const result = dialogue(text)
    expect(result.map((p) => [p.text, p.speaker])).toEqual([
      ['「你听到『回来』了吗？', '林夏'], ['回来。」', '林夏'], ['"回来。"', null],
    ])
    expect(new Set(result.map((p) => p.paragraphStart)).size).toBe(3)
    for (const p of result) expect(Array.from(text).slice(p.start, p.end).join('')).toBe(p.text)
    const repeated = dialogue('“好。”“好。”')
    expect(repeated.map((p) => p.start)).toEqual([0, 4])
  })
})
