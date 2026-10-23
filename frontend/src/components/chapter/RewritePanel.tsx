import { CheckOutlined, CloseOutlined, ReloadOutlined, StopOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Radio, Space, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { applyRewrite, REWRITE_OP_LABELS, rewriteChapter } from '../../api/chapterVersions'
import { countWords } from '../../api/chapters'
import { ApiError, errorMessage } from '../../api/client'
import type { Chapter, RewriteDone, RewriteOp, RewriteRequest } from '../../api/types'
import { useStreamTask } from '../../hooks/useStreamTask'
import { codePointLength, utf16ToCodePoint } from '../../utils/textOffset'
import StreamOutput from '../StreamOutput'
import TextDiff from '../TextDiff'

/** 单次选区上限（码点），与后端一致 */
export const MAX_REWRITE_CHARS = 5000

/** 编辑器文本域的选区，UTF-16 偏移（selectionStart / selectionEnd） */
export interface TextSelection {
  start: number
  end: number
}

interface Props {
  pid: number
  chapterId: number
  /** 编辑器当前正文 */
  content: string
  selection: TextSelection
  aiConfigId: number | null
  /** 编辑器其他任务进行中 / 章节卡在生成中 / 无 AI 配置 */
  disabled: boolean
  /** 有未保存修改时先保存；失败返回 false */
  ensureSaved: () => Promise<boolean>
  /** 预览中（流式或待采纳）为 true：编辑器需把正文设为只读 */
  onLockChange: (locked: boolean) => void
  onApplied: (chapter: Chapter) => void
}

const SELECTION_OPS: RewriteOp[] = ['rewrite', 'expand', 'condense', 'deslop', 'continue']

function preview(text: string, max = 40): string {
  const cps = Array.from(text.replace(/\s+/g, ' ').trim())
  return cps.length > max ? `${cps.slice(0, max).join('')}…` : cps.join('')
}

/** 局部改写：对选区改写/扩写/缩写/去 AI 味，或在光标处续写；先预览，确认后采纳。 */
export default function RewritePanel({
  pid,
  chapterId,
  content,
  selection,
  aiConfigId,
  disabled,
  ensureSaved,
  onLockChange,
  onApplied,
}: Props) {
  const { message } = App.useApp()
  const task = useStreamTask()
  const [op, setOp] = useState<RewriteOp>('rewrite')
  const [instruction, setInstruction] = useState('')
  const [result, setResult] = useState<RewriteDone | null>(null)
  const [applying, setApplying] = useState(false)
  const [lastRequest, setLastRequest] = useState<RewriteRequest | null>(null)

  const locked = task.running || result !== null
  useEffect(() => {
    onLockChange(locked)
  }, [locked, onLockChange])
  useEffect(() => () => onLockChange(false), [onLockChange])

  const hasSelection = selection.end > selection.start
  const effectiveOp: RewriteOp = hasSelection ? op : 'continue'
  const selectedText = content.slice(selection.start, selection.end)
  const selectedChars = codePointLength(selectedText)
  const tooLong = effectiveOp !== 'continue' && selectedChars > MAX_REWRITE_CHARS
  const blankSelection = effectiveOp !== 'continue' && !selectedText.trim()
  const cursorPrefixEmpty = effectiveOp === 'continue' && !content.slice(0, selection.end).trim()
  const canStart = !disabled && !locked && !tooLong && !blankSelection && !cursorPrefixEmpty

  const run = async (req: RewriteRequest) => {
    setLastRequest(req)
    setResult(null)
    const outcome = await task.start((onDelta, signal) => rewriteChapter(chapterId, req, onDelta, signal))
    if (outcome.status === 'done') setResult(outcome.value)
    else if (outcome.status === 'aborted') message.info('已停止，正文未改变')
  }

  const handleStart = async () => {
    if (!canStart) return
    // 先保存：后端按服务端正文计算选区；保存不改变正文，偏移仍然有效
    if (!(await ensureSaved())) return
    const cpEnd = utf16ToCodePoint(content, selection.end)
    const cpStart = effectiveOp === 'continue' ? cpEnd : utf16ToCodePoint(content, selection.start)
    await run({
      ai_config_id: aiConfigId,
      op: effectiveOp,
      start: cpStart,
      end: cpEnd,
      instruction: instruction.trim(),
    })
  }

  const handleRegenerate = () => {
    // 预览期间正文只读，沿用上一次的偏移（文本域里的选区可能已被改动）
    if (lastRequest) void run(lastRequest)
  }

  const handleDiscard = () => {
    setResult(null)
    task.reset()
  }

  const handleApply = async () => {
    if (!result) return
    setApplying(true)
    try {
      const chapter = await applyRewrite(pid, chapterId, {
        start: result.start,
        end: result.end,
        original: result.original,
        // 光标续写时 original 为空串，需靠选区前原文校验正文未变
        before: result.before,
        replacement: result.text,
        log_id: result.log_id,
      })
      setResult(null)
      task.reset()
      onApplied(chapter)
      message.success('已采纳，并存为新版本')
    } catch (err) {
      message.error(errorMessage(err))
      // 正文已变化：预览作废，需重新选择
      if (err instanceof ApiError && err.status === 409) handleDiscard()
    } finally {
      setApplying(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {hasSelection ? (
        <Alert
          type={tooLong ? 'error' : 'info'}
          showIcon
          message={`已选中 ${selectedChars} 字${tooLong ? `，超过单次上限 ${MAX_REWRITE_CHARS} 字` : ''}`}
          description={preview(selectedText) || '（选中的全是空白）'}
        />
      ) : (
        <Alert
          type="info"
          showIcon
          message="未选中文字：将在光标处续写"
          description="在正文中拖选一段文字，可改写、扩写、缩写或去 AI 味。"
        />
      )}
      <Form layout="vertical" disabled={locked || disabled}>
        <Form.Item label="操作" style={{ marginBottom: 12 }}>
          <Radio.Group
            value={effectiveOp}
            onChange={(e) => setOp(e.target.value as RewriteOp)}
            optionType="button"
            size="small"
            options={SELECTION_OPS.map((o) => ({
              value: o,
              label: REWRITE_OP_LABELS[o],
              disabled: !hasSelection && o !== 'continue',
            }))}
          />
        </Form.Item>
        <Form.Item label="补充要求（可选）" style={{ marginBottom: 0 }}>
          <Input.TextArea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            maxLength={2000}
            autoSize={{ minRows: 2, maxRows: 6 }}
            placeholder={effectiveOp === 'continue' ? '例如：接着写主角离开客栈后的遭遇' : '例如：语气更冷峻，保留对话'}
          />
        </Form.Item>
      </Form>
      {effectiveOp === 'continue' && hasSelection && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          续写将插入到选区末尾，不替换选中的文字。
        </Typography.Text>
      )}

      {task.running && (
        <>
          <StreamOutput text={task.text} running placeholder="改写结果将在这里实时显示…" maxHeight={320} />
          <Button danger block icon={<StopOutlined />} onClick={task.stop}>
            停止（已输出 {countWords(task.text)} 字）
          </Button>
        </>
      )}

      {result && !task.running && (
        <>
          <Typography.Text strong>
            {lastRequest?.op === 'continue' ? '续写内容（将插入光标处）' : '与原文对比'}
          </Typography.Text>
          <TextDiff a={result.original} b={result.text} maxHeight={320} />
          <Space wrap>
            <Button type="primary" icon={<CheckOutlined />} loading={applying} onClick={() => void handleApply()}>
              采纳
            </Button>
            <Button icon={<ReloadOutlined />} disabled={applying || disabled} onClick={handleRegenerate}>
              重新生成
            </Button>
            <Button icon={<CloseOutlined />} disabled={applying} onClick={handleDiscard}>
              放弃
            </Button>
          </Space>
        </>
      )}

      {!locked && (
        <Button
          type="primary"
          block
          icon={<ThunderboltOutlined />}
          disabled={!canStart}
          onClick={() => void handleStart()}
        >
          {effectiveOp === 'continue' ? '在光标处续写' : `${REWRITE_OP_LABELS[effectiveOp]}选中内容`}
        </Button>
      )}
      {cursorPrefixEmpty && !locked && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          光标前没有正文，无法续写；整章生成请用「AI 生成」。
        </Typography.Text>
      )}
      {task.error && !task.running && <Alert type="error" showIcon message="改写失败" description={task.error} />}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
        预览期间正文只读；采纳后只替换该段，替换前的正文自动存为版本。
      </Typography.Paragraph>
    </Space>
  )
}
