import { Alert, Button, Input, Modal, Radio, Space, Typography } from 'antd'
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ApiError, currentRequestGeneration, errorMessage, watchRequestGeneration } from '../../api/client'
import { richDocumentApi, type RichDocumentOut, type SectionRewriteDone, type SectionRewriteRequest } from '../../api/richDocument'
import { isAbortError } from '../../api/sse'
import type { StyleChoice } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import { skillChoiceProblemText, type SkillRequestChoice } from '../../utils/skillInputs'
import StreamOutput from '../StreamOutput'
import TextDiff from '../TextDiff'
import { sectionDisclosure, sectionSelectionProblem, type SectionRewriteTarget } from './RichSectionRewriteModalModel'

export type { SectionRewriteTarget } from './RichSectionRewriteModalModel'

interface Props {
  target: SectionRewriteTarget | null
  aiConfigId: number | null
  styleChoice: StyleChoice
  /** 本次选用的 Skill 及输入项（章节编辑器 AI 助手提供） */
  skill: SkillRequestChoice
  onClose: () => void
  onApplied: (document: RichDocumentOut) => void
  onLockChange: (locked: boolean) => void
}

type RewriteOp = SectionRewriteRequest['op']
type Stage = 'idle' | 'sending' | 'applying'

interface ViewState {
  key: string
  stage: Stage
  confirmation: boolean
  output: string
  result: SectionRewriteDone | null
  error: string | null
  frozen: boolean
  formatRequired: boolean
  formatConfirmation: boolean
}

const initial = (key: string): ViewState => ({
  key, stage: 'idle', confirmation: false, output: '', result: null, error: null, frozen: false,
  formatRequired: false, formatConfirmation: false,
})

const options: { value: RewriteOp; label: string }[] = [
  { value: 'rewrite', label: '改写' }, { value: 'expand', label: '扩写' },
  { value: 'condense', label: '缩写' }, { value: 'deslop', label: '去 AI 味' },
]

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

async function selectedHash(text: string): Promise<string> {
  if (!globalThis.crypto?.subtle || typeof globalThis.crypto.randomUUID !== 'function') {
    throw new Error('当前浏览器环境无法安全确认选区，请使用 HTTPS 或本机地址重试。')
  }
  const bytes = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(bytes), (value) => value.toString(16).padStart(2, '0')).join('')
}

/**
 * 每次出站都重新取得明确确认；预览和采纳绑定同一身份、选区与章版本。
 * 服务端仍负责发送租约、身份复核和原子写入。
 */
export default function RichSectionRewriteModal({ target, aiConfigId, styleChoice, skill, onClose, onApplied, onLockChange }: Props) {
  const user = useAuthStore((state) => state.user)
  const authStatus = useAuthStore((state) => state.status)
  const project = useProjectStore((state) => state.project)
  const access = projectAccess(project, user, target?.projectId ?? -1)
  const actorId = user?.id ?? null
  const authKey = authorizationKey(user)
  const sessionGeneration = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const identity = JSON.stringify([authStatus, actorId, authKey, sessionGeneration, project?.id, project?.owner?.id])
  // target 包含旧作者选区，不能只清预览后继续交给后来登录的同作品用户。
  const [boundIdentity, setBoundIdentity] = useState<string | null>(() => target ? identity : null)
  const allowed = authStatus === 'authenticated' && access.canUseAI && access.canEdit && boundIdentity === identity
  // 选用的 Skill 有必填输入项未填时不能发送
  const skillProblem = skillChoiceProblemText(skill)
  const key = JSON.stringify([target && [target.projectId, target.chapterId, target.sceneId, target.sectionKey,
    target.sectionTitle, target.includeInAi, target.contentRevision, target.planningRevision,
    target.start, target.end, target.selectedText], identity])
  const [stored, setStored] = useState<ViewState>(() => initial(key))
  const state = stored.key === key ? stored : initial(key)
  const [op, setOp] = useState<RewriteOp>('rewrite')
  const [instruction, setInstruction] = useState('')
  const epoch = useRef(0)
  const busy = useRef(false)
  const stream = useRef<AbortController | null>(null)
  const committedKey = useRef(key)
  const callbacks = useRef({ onClose, onApplied, onLockChange })

  useLayoutEffect(() => {
    committedKey.current = key
    callbacks.current = { onClose, onApplied, onLockChange }
  }, [key, onClose, onApplied, onLockChange])

  useEffect(() => {
    if (!target) {
      setBoundIdentity(null)
    } else if (boundIdentity === null) {
      setBoundIdentity(identity)
    } else if (boundIdentity !== identity) {
      // 新身份必须由宿主重新选择并保存，不能继承旧身份的 target。
      callbacks.current.onClose()
    }
    // 目标内容变化不改变身份绑定；目标从 null 打开时才建立新绑定。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boundIdentity, identity, target !== null])

  useEffect(() => {
    setStored(initial(key))
    setOp('rewrite')
    setInstruction('')
    if (target && allowed) callbacks.current.onLockChange(true)
    return () => {
      epoch.current += 1
      stream.current?.abort()
      stream.current = null
      busy.current = false
      if (target && allowed) callbacks.current.onLockChange(false)
    }
    // target 的完整身份已编码进 key，避免父级每次创建对象就中断正在进行的请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, allowed])

  const isCurrent = (ticket: number, snapshot: SectionRewriteTarget) => {
    const auth = useAuthStore.getState()
    const currentProject = useProjectStore.getState().project
    const now = projectAccess(currentProject, auth.user, snapshot.projectId)
    return epoch.current === ticket && committedKey.current === key && auth.status === 'authenticated'
      && currentRequestGeneration() === sessionGeneration && auth.user?.id === actorId
      && authorizationKey(auth.user) === authKey && now.canUseAI && now.canEdit
  }

  const close = () => {
    if (state.stage === 'applying') return
    epoch.current += 1
    stream.current?.abort()
    stream.current = null
    busy.current = false
    callbacks.current.onClose()
  }

  const send = async () => {
    if (!target || !allowed || !state.confirmation || state.frozen || busy.current || sectionSelectionProblem(target) || skillProblem) return
    const snapshot = { ...target }
    const ticket = ++epoch.current
    if (!isCurrent(ticket, snapshot)) return
    busy.current = true
    const controller = new AbortController()
    stream.current = controller
    setStored((previous) => ({ ...(previous.key === key ? previous : initial(key)), stage: 'sending', confirmation: false,
      output: '', error: null }))
    try {
      const hash = await selectedHash(snapshot.selectedText)
      if (controller.signal.aborted) throw new DOMException('aborted', 'AbortError')
      if (!isCurrent(ticket, snapshot)) return
      // 每次明确确认都使用新 UUID；网络结果不确定时不自动重放旧确认。
      const body: SectionRewriteRequest = {
        expected_content_revision: snapshot.contentRevision, expected_planning_revision: snapshot.planningRevision,
        request_id: globalThis.crypto.randomUUID(), confirmed: true, op, start: snapshot.start, end: snapshot.end,
        selected_hash: hash, instruction, ai_config_id: aiConfigId, style: styleChoice, ...skill.payload,
      }
      const result = await richDocumentApi.rewriteSelection(snapshot.projectId, snapshot.chapterId, snapshot.sceneId,
        snapshot.sectionKey, body, snapshot.selectedText, (part) => {
          if (isCurrent(ticket, snapshot) && !controller.signal.aborted) {
            setStored((previous) => previous.key === key ? { ...previous, output: previous.output + part } : previous)
          }
        }, controller.signal)
      if (isCurrent(ticket, snapshot) && !controller.signal.aborted) {
        setStored((previous) => previous.key === key ? { ...previous, result, error: null,
          formatRequired: false, formatConfirmation: false } : previous)
      }
    } catch (error) {
      if (isCurrent(ticket, snapshot)) {
        const frozen = error instanceof ApiError && [401, 403, 404, 409].includes(error.status)
        setStored((previous) => previous.key === key ? { ...previous, frozen,
          error: isAbortError(error) ? '已停止显示；已发出的模型请求无法撤回，原区块未改变。' : errorMessage(error) } : previous)
      }
    } finally {
      if (epoch.current === ticket) {
        busy.current = false
        stream.current = null
        setStored((previous) => previous.key === key ? { ...previous, stage: 'idle' } : previous)
      }
    }
  }

  const apply = async (confirmFormatLoss = false) => {
    if (!target || !state.result || !allowed || state.frozen || busy.current
        || (state.formatRequired && (!confirmFormatLoss || !state.formatConfirmation))) return
    const snapshot = { ...target }
    const result = state.result
    const ticket = ++epoch.current
    if (!isCurrent(ticket, snapshot)) return
    busy.current = true
    setStored((previous) => ({ ...previous, stage: 'applying', formatConfirmation: false, error: null }))
    try {
      const document = await richDocumentApi.applySelection(snapshot.projectId, snapshot.chapterId, snapshot.sceneId,
        snapshot.sectionKey, {
          expected_content_revision: snapshot.contentRevision, expected_planning_revision: snapshot.planningRevision,
          request_id: result.request_id, log_id: result.log_id, start: result.start, end: result.end,
          original: result.original, replacement: result.text,
          ...(confirmFormatLoss ? { confirm_format_loss: true } : {}),
        })
      if (!isCurrent(ticket, snapshot)) return
      if (!Number.isSafeInteger(document.content_revision) || document.content_revision <= snapshot.contentRevision
          || !document.document?.sections?.some((section) => section.key === snapshot.sectionKey)) {
        throw new Error('服务器返回的采纳结果无法核对；请刷新后确认区块是否已更新。')
      }
      callbacks.current.onApplied(document)
      callbacks.current.onClose()
    } catch (error) {
      if (isCurrent(ticket, snapshot)) {
        const formatBlocked = error instanceof ApiError && error.body?.code === 'rich_document_confirmation_required'
        const unmapped = error instanceof ApiError && error.body?.code === 'rich_document_unmapped'
        const detail = formatBlocked && !confirmFormatLoss
          ? '替换及其接缝处无法可靠保留的格式可能丢失；可定位的未改动格式仍保留。预览已保留，原区块未改变；采纳前须明确确认。'
          : formatBlocked
            ? '服务器未能按确认处理格式；预览已保留，原区块未改变。请核对最新内容后重新选择。'
          : unmapped
            ? '该结果存在无法安全自动映射的结构；预览已保留，原区块未改变，请手动调整。'
          : error instanceof ApiError && error.status === 409
            ? '正文、规划或 AI 可见范围已变化；预览已保留但不能采纳，请核对最新区块并重新选择。'
            : `无法确认采纳结果：${errorMessage(error)}。请先刷新核对，不要重复采纳。`
        setStored((previous) => previous.key === key ? { ...previous, error: detail,
          frozen: !formatBlocked || confirmFormatLoss, formatRequired: formatBlocked && !confirmFormatLoss } : previous)
      }
    } finally {
      if (epoch.current === ticket) {
        busy.current = false
        setStored((previous) => previous.key === key ? { ...previous, stage: 'idle' } : previous)
      }
    }
  }

  const problem = target ? sectionSelectionProblem(target) : null
  return (
    <>
      <Modal open={target !== null} title="区块选区 AI 编辑" footer={null} onCancel={close}
        closable={state.stage !== 'applying'} maskClosable={state.stage !== 'applying'} keyboard={state.stage !== 'applying'}>
        {!allowed || !target ? <Alert type="warning" showIcon message="当前身份不能编辑此作品或使用 AI。" /> : (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text>区块：{target.sectionTitle}</Typography.Text>
            <Alert type={problem ? 'warning' : 'info'} showIcon message={problem ?? `已选择 ${Array.from(target.selectedText).length} 个字符`}
              description={sectionDisclosure(target.includeInAi)} />
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
              已开启区块、正式正文、规划、既有摘要或记忆等原本允许的背景仍可能被使用；关闭不会撤回历史内容。
            </Typography.Paragraph>
            <Radio.Group aria-label="AI 编辑操作" value={op} options={options} optionType="button" size="small"
              disabled={state.stage !== 'idle' || state.frozen} onChange={(event) => setOp(event.target.value as RewriteOp)} />
            <label htmlFor="rich-section-rewrite-instruction">补充要求（可选）</label>
            <Input.TextArea id="rich-section-rewrite-instruction" value={instruction} maxLength={2000} autoSize={{ minRows: 2, maxRows: 5 }}
              disabled={state.stage !== 'idle' || state.frozen} onChange={(event) => setInstruction(event.target.value)} />
            {state.stage === 'sending' && <>
              <StreamOutput text={state.output} running placeholder="改写结果将在这里实时显示…" maxHeight={240} />
              <Button danger onClick={() => stream.current?.abort()}>停止生成</Button>
            </>}
            {state.result && <>
              <Typography.Text strong>与原选区对比</Typography.Text>
              <TextDiff a={state.result.original} b={state.result.text} maxHeight={240} />
              <details><summary>查看完整生成文本</summary>
                <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 240, overflow: 'auto' }}>{state.result.text}</pre>
              </details>
            </>}
            {state.error && <Alert type="warning" showIcon message={state.error} />}
            {skillProblem && <Alert type="warning" showIcon message={skillProblem} description="请在 AI 助手的「Skill（可选）」下方填写。" />}
            <Space wrap>
              {state.result && <Button type="primary" loading={state.stage === 'applying'} disabled={state.frozen || state.stage !== 'idle'}
                onClick={() => state.formatRequired
                  ? setStored((previous) => ({ ...previous, formatConfirmation: true })) : void apply()}>
                {state.formatRequired ? '核对格式并采纳' : '采纳选区'}
              </Button>}
              <Button type={state.result ? 'default' : 'primary'} disabled={!!problem || !!skillProblem || state.frozen || state.stage !== 'idle'}
                onClick={() => setStored((previous) => ({ ...(previous.key === key ? previous : initial(key)), confirmation: true }))}>
                {state.result ? '重新生成' : '继续并确认发送'}
              </Button>
              <Button disabled={state.stage === 'applying'} onClick={close}>放弃并关闭</Button>
            </Space>
          </Space>
        )}
      </Modal>
      {target && allowed && state.confirmation && <Modal open title="确认本次发送" okText="确认并发送" cancelText="取消"
        onCancel={() => setStored((previous) => ({ ...previous, confirmation: false }))} onOk={() => void send()}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>{sectionDisclosure(target.includeInAi)}</Typography.Text>
          <Typography.Text>此确认仅用于本次请求，不改变区块开关；正式正文、其他已开启背景以及既有摘要或记忆仍可能被使用。</Typography.Text>
          <Typography.Text strong>本次所选文字</Typography.Text>
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 240, overflow: 'auto' }}>{target.selectedText}</pre>
        </Space>
      </Modal>}
      {target && allowed && state.formatConfirmation && <Modal open title="确认格式变化" okText="确认格式变化并采纳" cancelText="取消"
        onCancel={() => setStored((previous) => ({ ...previous, formatConfirmation: false }))} onOk={() => void apply(true)}>
        <Typography.Paragraph>
          替换及其接缝处无法可靠保留的格式可能丢失；可定位的未改动格式仍保留。确认后才会写入区块，取消不会修改原稿。
        </Typography.Paragraph>
      </Modal>}
    </>
  )
}
