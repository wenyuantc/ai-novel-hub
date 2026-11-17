import { PictureOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Space, Spin, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { applyThumbnailAI, generateThumbnailAI, loadThumbnailPrompt, type ThumbnailApplyResult, type ThumbnailPreview } from '../../api/settingThumbnailAi'
import type { ThumbnailTarget } from '../../api/settingThumbnails'
import { useAIConfigStore } from '../../stores/aiConfigs'
import { useProjectStore } from '../../stores/project'
import { writeResultUnknown } from '../../utils/writeResult'
import AIConfigSelect from '../AIConfigSelect'
import { sameCoverChoice } from '../project/coverGeneration'
import { CoverModelSource } from '../project/CoverGenerateModal'
import { thumbnailModelChoice } from './thumbnailGeneration'

interface Props {
  target: ThumbnailTarget
  name: string
  version: string | null
  revision?: number
  current: () => boolean
  onClose: () => void
  onApplied: (result: ThumbnailApplyResult) => void
  onConflict: (error: unknown) => void
  onRequestError?: (error: unknown) => void
  onOrphan?: () => void
}

/** 预览与最初的身份、主体和图片版本绑定；确认采用独立服务端凭据。 */
export default function SettingThumbnailGenerateModal(props: Props) {
  const [context] = useState(() => props)
  const { message } = App.useApp()
  const [prompt, setPrompt] = useState('')
  const [promptToken, setPromptToken] = useState('')
  const [configId, setConfigId] = useState<number | null>(null)
  const [preview, setPreview] = useState<ThumbnailPreview | null>(null)
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const configs = useAIConfigStore((s) => s.configs)
  const loaded = useAIConfigStore((s) => s.loaded)
  const project = useProjectStore((s) => s.project)
  const choice = thumbnailModelChoice(context.target, project?.access, loaded, configs, configId)
  const promptLength = Array.from(prompt).length
  const promptTooLong = promptLength > 2000
  const mounted = useRef(false)
  const serial = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const operation = useRef<'generate' | 'save' | null>(null)
  const conflict = useRef<{ error: unknown } | null>(null)

  useEffect(() => {
    mounted.current = true
    const ticket = ++serial.current
    const requestController = new AbortController()
    controller.current = requestController
    const current = () => mounted.current && serial.current === ticket && context.current() && !requestController.signal.aborted
    void loadThumbnailPrompt(context.target, current, requestController.signal).then((value) => {
      if (value && current()) { setPrompt(value.prompt); setPromptToken(value.prompt_token) }
    }).catch((err: unknown) => {
      if (current()) { context.onRequestError?.(err); setError(errorMessage(err)) }
    }).finally(() => { if (current()) setLoading(false) })
    return () => { mounted.current = false; serial.current += 1; requestController.abort(); controller.current?.abort() }
  }, [attempt, context])

  const close = () => {
    if (operation.current === 'save') return
    controller.current?.abort()
    if (conflict.current) context.onConflict(conflict.current.error)
    else context.onClose()
  }

  const generate = async () => {
    if (operation.current || !context.current() || !promptToken || !prompt.trim() || promptTooLong || stale) return
    const configsNow = useAIConfigStore.getState()
    const currentChoice = thumbnailModelChoice(context.target, useProjectStore.getState().project?.access,
      configsNow.loaded, configsNow.configs, configId)
    if (!choice.allowed || !currentChoice.allowed || !sameCoverChoice(choice, currentChoice)) {
      setError('图片模型选择已变化，请重新选择。')
      return
    }
    const requestController = new AbortController()
    controller.current = requestController
    const ticket = ++serial.current
    const current = () => mounted.current && serial.current === ticket && context.current() && !requestController.signal.aborted
    operation.current = 'generate'
    setGenerating(true)
    setPreview(null)
    setError(null)
    try {
      const result = await generateThumbnailAI(context.target, {
        prompt: prompt.trim(), prompt_token: promptToken,
        expected_version: context.version ?? '',
        ...(context.revision !== undefined ? { expected_revision: context.revision } : {}),
        ...(currentChoice.mode === 'actor' ? { config_id: currentChoice.configId } : {}),
      }, currentChoice.mode, current, requestController.signal)
      if (result && current()) setPreview(result)
    } catch (err) {
      if (current()) {
        context.onRequestError?.(err)
        const changed = err instanceof ApiError && [403, 404, 409].includes(err.status)
        if (changed) setStale(true)
        setError(changed ? `${errorMessage(err)}。请保留需要的提示词，关闭后重新打开以读取最新设定。`
          : `${errorMessage(err)}。请求可能已产生模型费用，请确认后再重试。`)
      }
    } finally {
      operation.current = null
      if (mounted.current && context.current()) setGenerating(false)
    }
  }

  const apply = async () => {
    if (!preview || operation.current || !context.current()) return
    const requestController = new AbortController()
    controller.current = requestController
    const ticket = ++serial.current
    const current = () => mounted.current && serial.current === ticket && context.current() && !requestController.signal.aborted
    operation.current = 'save'
    setSaving(true)
    setError(null)
    try {
      const result = await applyThumbnailAI(context.target,
        { image_base64: preview.image_base64, preview_token: preview.preview_token }, current, requestController.signal)
      if (result && current()) {
        context.onApplied(result)
        message.success('已更新设定图片')
      } else if (context.current()) context.onOrphan?.()
    } catch (err) {
      if (current()) {
        context.onRequestError?.(err)
        if (writeResultUnknown(err) || (err instanceof ApiError && [403, 404, 409].includes(err.status))) {
          conflict.current = { error: err }
          setStale(true)
          setPreview(null)
          setError(`${errorMessage(err)}。请先保留需要的提示词，关闭后重新读取图片或系列版本。`)
        } else setError(errorMessage(err))
      } else if (context.current()) context.onOrphan?.()
    } finally {
      operation.current = null
      if (mounted.current && context.current()) setSaving(false)
    }
  }

  return <Modal open title="AI 生成设定图片" width={640} onCancel={close} closable={!saving} keyboard={!saving} maskClosable={!saving}
    footer={[
      <Button key="generate" icon={preview ? <ReloadOutlined /> : <PictureOutlined />} loading={generating}
        disabled={loading || saving || stale || !context.current() || !choice.allowed || !promptToken || !prompt.trim() || promptTooLong}
        onClick={() => void generate()}>{preview ? '重新生成' : '生成图片'}</Button>,
      <Button key="cancel" disabled={saving} onClick={close}>取消</Button>,
      <Button key="apply" type="primary" loading={saving} disabled={!preview || generating || stale || !context.current()} onClick={() => void apply()}>使用这张图片</Button>,
    ]}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text type="secondary">为「{context.name}」生成图片。自动带入的设定仅包含已保存且允许发送给 AI 的字段；未保存的文字不会自动带入。预览后可决定是否使用。</Typography.Text>
      <Typography.Text type="secondary">
        <Typography.Link href="/prompt-templates?key=setting.thumbnail" target="_blank" rel="noopener noreferrer">配置默认提示词模板</Typography.Link>
        {' '}（保存后重新打开此弹窗生效；此处的修改仅用于本次生成）
      </Typography.Text>
      {choice.mode === 'owner' && <Alert type="warning" showIcon message="可能使用作者授权的图片模型" description="你尚未配置图片模型；本次可能使用作者密钥并产生作者费用。" />}
      {choice.mode === 'invalid' && <Alert type="warning" showIcon message="所选图片模型已不可用，请重新选择。" />}
      <Form layout="vertical" disabled={loading || generating || saving}>
        <Form.Item label="图片模型" htmlFor="setting-image-model">
          {choice.mode === 'owner' ? <Typography.Text>未选择本人模型，可使用作者授权的默认图片模型。</Typography.Text>
            : <AIConfigSelect id="setting-image-model" modelType="image" ownOnly={context.target.scope === 'series'} value={configId}
              onChange={(value) => { setConfigId(value); setPreview(null) }} disabled={generating || saving} />}
        </Form.Item>
        <Form.Item label="图片提示词" htmlFor="setting-image-prompt" style={{ marginBottom: 8 }}>
          <Input.TextArea id="setting-image-prompt" aria-label="图片提示词" aria-describedby="setting-image-prompt-count" aria-invalid={promptTooLong} value={prompt}
            onChange={(event) => { setPrompt(event.target.value); setPreview(null) }} autoSize={{ minRows: 5, maxRows: 10 }} />
          <Typography.Text id="setting-image-prompt-count" type={promptTooLong ? 'danger' : 'secondary'} style={{ display: 'block', textAlign: 'right', marginTop: 4 }}>
            {promptLength} / 2000{promptTooLong ? '，请缩短后生成' : ''}
          </Typography.Text>
        </Form.Item>
      </Form>
      {error && <Alert type="error" showIcon message={error} action={loading || promptToken || stale ? undefined : <Button size="small" onClick={() => { setLoading(true); setError(null); setAttempt((n) => n + 1) }}>重试</Button>} />}
      {loading && <Spin aria-label="正在准备图片提示词" />}
      {generating && <Typography.Text role="status">正在生成图片，可能需要一些时间…</Typography.Text>}
      {preview && <div style={{ textAlign: 'center' }}>
        <img src={`data:${preview.mime_type};base64,${preview.image_base64}`} alt={`${context.name}的待确认设定图片`}
          style={{ display: 'block', maxWidth: '100%', maxHeight: 360, margin: '0 auto', borderRadius: 4 }} />
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>此为预览，点击「使用这张图片」后保存。</Typography.Text>
        <CoverModelSource source={preview.model_source} />
      </div>}
    </Space>
  </Modal>
}
