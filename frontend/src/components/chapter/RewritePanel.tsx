import { CheckOutlined, CloseOutlined, ReloadOutlined, StopOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Radio, Space, Typography } from 'antd'
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { applyRewrite, REWRITE_OP_LABELS, rewriteChapter } from '../../api/chapterVersions'
import { countWords } from '../../api/chapters'
import { currentRequestGeneration, errorMessage, watchRequestGeneration } from '../../api/client'
import { sceneApi } from '../../api/scenes'
import type { Chapter, RewriteOp, RewriteRequest, Scene, SceneRewriteDone, StyleChoice } from '../../api/types'
import { useStreamTask } from '../../hooks/useStreamTask'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { codePointLength, utf16ToCodePoint } from '../../utils/textOffset'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import StreamOutput from '../StreamOutput'
import TextDiff from '../TextDiff'
import { classifyRewriteApplyIssue, rewriteApplyBody, rewritePreviewProblem, type RewritePreviewTarget } from './RewritePanelModel'
import { skillChoiceProblemText, type SkillRequestChoice } from '../../utils/skillInputs'

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
  /** 本次写作风格（由编辑器 AI 助手的「本次写作风格」提供） */
  styleChoice: StyleChoice
  /** 本次选用的 Skill 及输入项（由 AI 助手的「Skill（可选）」提供） */
  skill: SkillRequestChoice
  /** 编辑器其他任务进行中 / 章节卡在生成中 / 无 AI 配置 */
  disabled: boolean
  /** 有未保存修改时先保存；失败返回 false */
  ensureSaved: () => Promise<boolean>
  /** 预览中（流式或待采纳）为 true：编辑器需把正文设为只读 */
  onLockChange: (locked: boolean) => void
  onApplied: (chapter: Chapter) => void
  sceneId?: number
  getSceneVersions?: () => { content_revision: number; planning_revision: number } | null
  onSceneApplied?: (scene: Scene) => void
}

const SELECTION_OPS: RewriteOp[] = ['rewrite', 'expand', 'condense', 'deslop', 'continue']
const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

function currentIdentity(pid: number): string {
  const auth = useAuthStore.getState()
  const project = useProjectStore.getState().project
  return JSON.stringify([pid, auth.status, auth.user?.id, authorizationKey(auth.user), currentRequestGeneration(),
    project?.id, project?.owner?.id])
}

function authorizedForIdentity(pid: number, expected: string): boolean {
  const auth = useAuthStore.getState()
  const access = projectAccess(useProjectStore.getState().project, auth.user, pid)
  return auth.status === 'authenticated' && access.canUseAI && access.canEdit && currentIdentity(pid) === expected
}

/** 局部改写：对选区改写/扩写/缩写/去 AI 味，或在光标处续写；先预览，确认后采纳。 */
export default function RewritePanel({
  pid,
  chapterId,
  content,
  selection,
  aiConfigId,
  styleChoice,
  skill,
  disabled,
  ensureSaved,
  onLockChange,
  onApplied,
  sceneId,
  getSceneVersions,
  onSceneApplied,
}: Props) {
  const { message } = App.useApp()
  const { canUseAI, canEditNow } = useProjectAccess(pid)
  const user = useAuthStore((state) => state.user)
  const authStatus = useAuthStore((state) => state.status)
  const project = useProjectStore((state) => state.project)
  const session = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const identity = JSON.stringify([pid, authStatus, user?.id, authorizationKey(user), session, project?.id, project?.owner?.id])
  const task = useStreamTask()
  const [op, setOp] = useState<RewriteOp>('rewrite')
  const [instruction, setInstruction] = useState('')
  const [result, setResult] = useState<SceneRewriteDone | null>(null)
  const [resultIdentity, setResultIdentity] = useState<string | null>(null)
  const [runIdentity, setRunIdentity] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [formatRequired, setFormatRequired] = useState(false)
  const [formatConfirmation, setFormatConfirmation] = useState(false)
  const [applying, setApplying] = useState(false)
  const [lastRequest, setLastRequest] = useState<RewritePreviewTarget | null>(null)
  const latest = useRef({ content, selection, sceneId })
  const running = useRef(false)
  const starting = useRef(false)
  const applyBusy = useRef(false)
  const epoch = useRef(0)

  useLayoutEffect(() => { latest.current = { content, selection, sceneId } }, [content, selection, sceneId])
  const stopTask = task.stop
  useEffect(() => {
    epoch.current += 1
    running.current = false
    starting.current = false
    applyBusy.current = false
    stopTask()
    // 身份变化要主动清除内存中的旧作者预览，而不是仅靠派生值隐藏。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setResult(null); setResultIdentity(null); setRunIdentity(null); setLastRequest(null)
    setConflict(false); setProblem(null); setFormatRequired(false); setFormatConfirmation(false); setApplying(false)
    return () => { epoch.current += 1; stopTask() }
  }, [identity, chapterId, sceneId, stopTask])

  const currentResult = resultIdentity === identity ? result : null
  const locked = task.running || currentResult !== null
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
  const skillProblem = skillChoiceProblemText(skill)
  const canStart = !disabled && !locked && !tooLong && !blankSelection && !cursorPrefixEmpty && !skillProblem

  const run = async (target: RewritePreviewTarget) => {
    if (running.current || !canEditNow() || !authorizedForIdentity(pid, target.identity)) return
    const ticket = epoch.current
    running.current = true
    setRunIdentity(target.identity)
    try {
      const outcome = await task.start<SceneRewriteDone>((onDelta, signal) => target.sceneId !== undefined && target.revisions
        ? sceneApi.rewrite(pid, target.chapterId, target.sceneId, { ...target.request,
          expected_content_revision: target.revisions.content_revision, expected_planning_revision: target.revisions.planning_revision }, onDelta, signal)
        : rewriteChapter(target.chapterId, target.request, onDelta, signal))
      if (ticket !== epoch.current || !canEditNow() || !authorizedForIdentity(pid, target.identity)) return
      if (outcome.status === 'done') {
        const mismatch = rewritePreviewProblem(outcome.value, target)
        if (mismatch) { message.error(mismatch); return }
        setResult(outcome.value); setResultIdentity(target.identity); setLastRequest(target)
        setConflict(false); setProblem(null); setFormatRequired(false); setFormatConfirmation(false)
      } else if (outcome.status === 'aborted') message.info('已停止，正文未改变')
    } finally {
      if (ticket === epoch.current) running.current = false
    }
  }

  const handleStart = async () => {
    if (!canStart || starting.current || !canUseAI || !canEditNow() || !authorizedForIdentity(pid, identity)) return
    const ticket = epoch.current
    starting.current = true
    try {
      // 先保存；若等待期间选区或草稿改变，不能把新正文误当成原选择发送。
      if (!(await ensureSaved()) || ticket !== epoch.current || !canEditNow() || !authorizedForIdentity(pid, identity)) return
      const current = latest.current
      if (current.content !== content || current.selection.start !== selection.start
          || current.selection.end !== selection.end || current.sceneId !== sceneId) {
        message.warning('保存期间正文或选区已变化，请重新选择。')
        return
      }
      const cpEnd = utf16ToCodePoint(content, selection.end)
      const cpStart = effectiveOp === 'continue' ? cpEnd : utf16ToCodePoint(content, selection.start)
      const versions = sceneId !== undefined ? getSceneVersions?.() : undefined
      if (sceneId !== undefined && !versions) { message.warning('场景版本尚未加载，请重新选择。'); return }
      const request: RewriteRequest = {
        ai_config_id: aiConfigId, op: effectiveOp, start: cpStart, end: cpEnd,
        instruction: instruction.trim(), style: styleChoice, ...skill.payload,
      }
      const points = Array.from(content)
      await run({ chapterId, sceneId, request, original: points.slice(cpStart, cpEnd).join(''), identity,
        ...(sceneId !== undefined ? { revisions: versions!, before: points.slice(Math.max(0, cpStart - 200), cpStart).join('') } : {}) })
    } catch (error) {
      if (ticket === epoch.current && authorizedForIdentity(pid, identity)) message.error(errorMessage(error))
    } finally {
      if (ticket === epoch.current) starting.current = false
    }
  }

  const handleRegenerate = () => {
    if (disabled || !lastRequest || lastRequest.identity !== identity || !authorizedForIdentity(pid, identity)) return
    const versions = lastRequest.sceneId !== undefined ? getSceneVersions?.() : undefined
    if (lastRequest.revisions && (!versions || versions.content_revision !== lastRequest.revisions.content_revision
        || versions.planning_revision !== lastRequest.revisions.planning_revision)) {
      setConflict(true); setProblem('正文或规划版本已变化，预览已保留；请核对最新场景并重新选择。')
      return
    }
    void run(lastRequest)
  }

  const handleDiscard = () => {
    setResult(null); setResultIdentity(null); setLastRequest(null)
    setConflict(false); setProblem(null); setFormatRequired(false); setFormatConfirmation(false)
    task.reset()
  }

  const handleApply = async (confirmFormatLoss = false) => {
    if (disabled || !currentResult || !lastRequest || conflict || applyBusy.current || !canUseAI || !canEditNow()
        || !authorizedForIdentity(pid, lastRequest.identity) || (confirmFormatLoss && !formatConfirmation)) return
    const mismatch = rewritePreviewProblem(currentResult, lastRequest)
    if (mismatch) { setConflict(true); setProblem(mismatch); return }
    const ticket = epoch.current
    applyBusy.current = true
    setApplying(true); setProblem(null); setFormatConfirmation(false)
    try {
      const body = rewriteApplyBody(currentResult, confirmFormatLoss)
      if (lastRequest.sceneId !== undefined) {
        const scene = await sceneApi.applyRewrite(pid, chapterId, lastRequest.sceneId, { ...body,
          expected_content_revision: currentResult.content_revision, expected_planning_revision: currentResult.planning_revision })
        if (ticket !== epoch.current || !canEditNow() || !authorizedForIdentity(pid, lastRequest.identity)) return
        if (scene.id !== lastRequest.sceneId || scene.content_revision <= currentResult.content_revision) {
          throw new Error('服务器返回的场景版本无法核对')
        }
        onSceneApplied?.(scene)
      } else {
        const chapter = await applyRewrite(pid, chapterId, body)
        if (ticket !== epoch.current || !canEditNow() || !authorizedForIdentity(pid, lastRequest.identity)) return
        if (chapter.id !== chapterId || chapter.content_revision <= currentResult.content_revision) {
          throw new Error('服务器返回的章节版本无法核对')
        }
        onApplied(chapter)
      }
      handleDiscard()
      message.success('已采纳，并存为新版本')
    } catch (error) {
      if (ticket !== epoch.current || !canEditNow() || !authorizedForIdentity(pid, lastRequest.identity)) return
      const issue = classifyRewriteApplyIssue(error, confirmFormatLoss)
      if (issue === 'format') {
        setFormatRequired(true)
        setProblem('替换及其接缝处无法可靠保留的格式可能丢失；可定位的未改动格式仍保留。预览和原稿均已保留，采纳前须明确确认。')
      } else {
        setConflict(true)
        setProblem(issue === 'unmapped' ? '区块位置、标记或结构无法安全自动映射；预览和原稿均已保留，请手动调整。'
          : issue === 'conflict' ? '正文、规划或 AI 可见范围已变化，预览已保留但不能采纳；请核对最新内容并重新选择。'
            : `无法确认采纳结果：${errorMessage(error)}。请先刷新核对，不要重复采纳。`)
      }
    } finally {
      if (ticket === epoch.current) { applyBusy.current = false; setApplying(false) }
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {hasSelection ? (
        <Alert
          type={tooLong ? 'error' : 'info'}
          showIcon
          message={`已选中 ${selectedChars} 字${tooLong ? `，超过单次上限 ${MAX_REWRITE_CHARS} 字` : ''}`}
          description={blankSelection ? '选中的全是空白，请重新选择。' : '改写前会保存当前草稿，并核对目标和选区。'}
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

      {task.running && runIdentity === identity && (
        <>
          <StreamOutput text={task.text} running placeholder="改写结果将在这里实时显示…" maxHeight={320} />
          <Button danger block icon={<StopOutlined />} onClick={task.stop}>
            停止（已输出 {countWords(task.text)} 字）
          </Button>
        </>
      )}

      {currentResult && !task.running && (
        <>
          <Typography.Text strong>
            {lastRequest?.request.op === 'continue' ? '续写内容（将插入光标处）' : '与原文对比'}
          </Typography.Text>
          <TextDiff a={currentResult.original} b={currentResult.text} maxHeight={320} />
          <details><summary>查看完整生成文本</summary>
            <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 320, overflow: 'auto' }}>{currentResult.text}</pre>
          </details>
          <Space wrap>
            <Button type="primary" icon={<CheckOutlined />} loading={applying} disabled={disabled || conflict}
              onClick={() => formatRequired ? setFormatConfirmation(true) : void handleApply()}>
              {formatRequired ? '核对格式并采纳' : '采纳'}
            </Button>
            <Button icon={<ReloadOutlined />} disabled={applying || disabled || conflict} onClick={handleRegenerate}>
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
      {skillProblem && !locked && <Typography.Text type="warning" style={{ fontSize: 12 }}>{skillProblem}</Typography.Text>}
      {cursorPrefixEmpty && !locked && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          光标前没有正文，无法续写；整章生成请用「AI 生成」。
        </Typography.Text>
      )}
      {task.error && !task.running && runIdentity === identity && <Alert type="error" showIcon message="改写失败" description={task.error} />}
      {problem && resultIdentity === identity && <Alert type="warning" showIcon message={problem} />}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
        预览期间正文只读；采纳后只替换该段，替换前的正文自动存为版本。
      </Typography.Paragraph>
      {currentResult && !conflict && formatConfirmation && <Modal open title="确认格式变化" okText="确认格式变化并采纳" cancelText="取消"
        okButtonProps={{ disabled }} onCancel={() => setFormatConfirmation(false)} onOk={() => void handleApply(true)}>
        <Typography.Paragraph>
          替换及其接缝处无法可靠保留的格式可能丢失；可定位的未改动格式仍保留。确认后才会写入正文，取消不会修改原稿。
        </Typography.Paragraph>
      </Modal>}
    </Space>
  )
}
