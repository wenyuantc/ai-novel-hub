import { describe, expect, it } from 'vitest'
import { SSEParser, type SSEMessage } from './sse'

function feedAll(chunks: string[]): SSEMessage[] {
  const parser = new SSEParser()
  const out: SSEMessage[] = []
  for (const c of chunks) out.push(...parser.feed(c))
  out.push(...parser.flush())
  return out
}

describe('SSEParser', () => {
  it('解析标准帧并忽略注释帧', () => {
    const msgs = feedAll([': stream-start\n\nevent: delta\ndata: {"text":"你好"}\n\nevent: done\ndata: {}\n\n'])
    expect(msgs).toEqual([
      { event: 'delta', data: '{"text":"你好"}' },
      { event: 'done', data: '{}' },
    ])
  })

  it('处理跨 chunk 截断', () => {
    const full = 'event: delta\ndata: {"text":"第一章"}\n\nevent: delta\ndata: {"text":"续"}\n\n'
    const chunks: string[] = []
    for (let i = 0; i < full.length; i += 3) chunks.push(full.slice(i, i + 3))
    expect(feedAll(chunks)).toEqual([
      { event: 'delta', data: '{"text":"第一章"}' },
      { event: 'delta', data: '{"text":"续"}' },
    ])
  })

  it('处理 CRLF 且 \\r 与 \\n 被拆在两个 chunk', () => {
    const msgs = feedAll([': c\r\n\r', '\nevent: error\r', '\ndata: {"detail":"x"}\r\n\r', '\n'])
    expect(msgs).toEqual([{ event: 'error', data: '{"detail":"x"}' }])
  })

  it('多行 data 以换行拼接，缺省事件名为 message', () => {
    expect(feedAll(['data: a\ndata: b\n\n'])).toEqual([{ event: 'message', data: 'a\nb' }])
  })

  it('流结束时 flush 未以空行结尾的事件', () => {
    expect(feedAll(['event: done\ndata: {"ok":true}'])).toEqual([{ event: 'done', data: '{"ok":true}' }])
  })
})
