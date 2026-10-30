import { CheckOutlined, CopyOutlined } from '@ant-design/icons'
import { useRef, useState } from 'react'
import { copyDeveloperCode } from '../../utils/developerDocs'

interface Props { label: string; code: string }

/** 可键盘滚动和复制的公开示例，复制失败时选择代码供手动操作。 */
export default function CodeBlock({ label, code }: Props) {
  const pre = useRef<HTMLPreElement>(null)
  const [status, setStatus] = useState<'idle' | 'copied' | 'manual'>('idle')

  const copy = async () => {
    const copied = await copyDeveloperCode(code)
    if (!pre.current) return
    if (copied) setStatus('copied')
    else {
      pre.current.focus()
      const selection = window.getSelection()
      if (selection) {
        const range = document.createRange()
        range.selectNodeContents(pre.current)
        selection.removeAllRanges()
        selection.addRange(range)
      }
      setStatus('manual')
    }
  }

  return (
    <figure className="developer-code">
      <figcaption>
        <span>{label}</span>
        <button type="button" aria-label={`复制${label}`} onClick={() => void copy()}>
          {status === 'copied' ? <CheckOutlined aria-hidden /> : <CopyOutlined aria-hidden />}
          {status === 'copied' ? '已复制' : '复制'}
        </button>
      </figcaption>
      <pre ref={pre} tabIndex={0} role="region" aria-label={`${label}代码，可选中复制`}><code>{code}</code></pre>
      <p className="developer-code-status" role="status" aria-live="polite">
        {status === 'copied' ? '示例已复制。' : status === 'manual' ? '自动复制受限，请选中代码后按 Ctrl / ⌘ + C 手动复制。' : '\u00a0'}
      </p>
    </figure>
  )
}
