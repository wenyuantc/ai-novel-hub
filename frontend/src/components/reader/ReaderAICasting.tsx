import { Button, Space, Typography } from 'antd'
import { useLayoutEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { castingContentChanged, readerApi, type ReaderAICastingResult } from '../../api/reader'
import type { Project } from '../../api/types'
import { useAIModelSource } from '../../hooks/useAIModelSource'
import { DIALOGUE_ROLE, NARRATOR_ROLE, type ReaderVoice } from '../../utils/readerVoices'
import type { ReaderPassage } from '../../utils/readerDialogue'
import AIConfigSelect from '../AIConfigSelect'

interface Props {
  project: Project
  chapterId: number
  contentHash: string
  hasText: boolean
  cues: ReaderPassage[]
  voices: ReaderVoice[]
  sessionCurrent: () => boolean
  saveResult: (result: ReaderAICastingResult) => boolean
  reload: () => void
  recheckAccess: (current: () => boolean) => Promise<void>
}

export default function ReaderAICasting(props: Props) {
  const source = useAIModelSource('text', props.project)
  const [configId, setConfigId] = useState<number | null>(null)
  // 面板、授权、章节快照、模型或声音候选变化后销毁旧请求。声音列表异步加载时也能直接更新。
  const scope = JSON.stringify([props.project.id, props.project.access.authorization_version, props.chapterId,
    props.contentHash, props.voices, source, configId])
  return <CastingAction key={scope} {...props} source={source} configId={configId} setConfigId={setConfigId} />
}

function CastingAction({ project, chapterId, contentHash, hasText, cues, voices, sessionCurrent, saveResult, reload, recheckAccess, source, configId, setConfigId }: Props & {
  source: ReturnType<typeof useAIModelSource>
  configId: number | null
  setConfigId: (id: number | null) => void
}) {
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [stale, setStale] = useState(false)
  const pending = useRef<AbortController | null>(null)
  const cancel = () => {
    pending.current?.abort()
    pending.current = null
    setBusy(false)
  }
  useLayoutEffect(() => {
    const onPageHide = () => {
      pending.current?.abort()
      pending.current = null
      setBusy(false)
    }
    window.addEventListener('pagehide', onPageHide)
    return () => {
      window.removeEventListener('pagehide', onPageHide)
      pending.current?.abort()
      pending.current = null
    }
  }, [])
  const unavailable = !hasText ? '本章还没有可分析的正文。'
    : cues.length > 400 ? '本章超过单次可分析的 400 条台词。'
    : !voices.length ? '请先在系统语音设置中安装中文声音。' : null
  const enabled = !unavailable && (source === 'own' || source === 'owner') && !stale
  async function analyze() {
    if (!enabled || pending.current || !sessionCurrent()) return
    const controller = new AbortController()
    pending.current = controller
    const current = () => pending.current === controller && !controller.signal.aborted && sessionCurrent()
    setBusy(true)
    setFeedback('')
    try {
      const candidates = voices.slice(0, 128)
      const result = await readerApi.aiCasting(project.id, chapterId, { ai_config_id: source === 'owner' ? null : configId,
        content_hash: contentHash, cues: cues.map(({ start, end }) => ({ start, end })), voices: candidates }, controller.signal, current)
      if (!current()) return
      if (result.content_hash !== contentHash) {
        setStale(true)
        throw new Error('章节正文已变化，请刷新本章后重试。')
      }
      const starts = new Set(cues.map((cue) => cue.start))
      const ids = new Set(candidates.map((voice) => voice.id))
      const roles = new Set([NARRATOR_ROLE, ...result.cues.map((cue) => cue.speaker ?? DIALOGUE_ROLE)])
      if (!result.cues.every((cue) => starts.delete(cue.start)) || starts.size ||
        !result.voices.every((v) => roles.delete(v.speaker) && ids.has(v.voice_id)) || roles.size || !saveResult(result)) {
        throw new Error('模型返回的配音方案与本章台词或本机声音不匹配，请重试。')
      }
      setFeedback(result.cues.length ? `已为 ${result.cues.length} 条台词应用 AI 配音，手动设置继续优先。` : '已为本章旁白推荐音色，手动设置继续优先。')
    } catch (err) {
      if (current()) {
        setFeedback(errorMessage(err))
        if (castingContentChanged(err)) setStale(true)
        if (err instanceof ApiError && (err.status === 403 || err.status === 404 || err.body?.code === 'permission_denied')) await recheckAccess(current)
      }
    } finally {
      if (pending.current === controller) {
        pending.current = null
        setBusy(false)
      }
    }
  }
  return <section className="reader-ai-casting">
    <h3>AI 自动配音</h3>
    <Typography.Paragraph>点击分析会将本章已保存的正文和本机中文声音列表交给所选文本模型，用于判断说话人、情绪并分配声音。实际朗读仍在本机进行。</Typography.Paragraph>
    <label htmlFor="reader-casting-model">分析模型</label>
    <AIConfigSelect id="reader-casting-model" value={configId} onChange={setConfigId} projectSnapshot={project} disabled={busy} />
    {hasText && !cues.length && <Typography.Text type="secondary">本章没有引号台词，模型将为旁白选声。</Typography.Text>}
    {voices.length > 128 && <Typography.Text type="secondary">本次分析使用前 128 个本机中文声音。</Typography.Text>}
    {unavailable && <Typography.Text type="secondary">{unavailable}</Typography.Text>}
    <Space wrap>
      <Button type="primary" onClick={analyze} loading={busy} disabled={!enabled}>分析本章并配音</Button>
      {busy && <Button onClick={cancel}>取消等待</Button>}
      {stale && <Button onClick={reload}>刷新本章</Button>}
    </Space>
    <div role="status">{feedback}</div>
  </section>
}
