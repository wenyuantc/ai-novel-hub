import { PictureOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Space, Spin, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { coverPrompt, generateCover, uploadCover } from '../../api/projects'
import type { CoverPreview, Project } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useAIConfigStore } from '../../stores/aiConfigs'
import { coverPreviewBlob } from '../../utils/projectCover'
import AIConfigSelect from '../AIConfigSelect'

interface Props {
  projectId: number
  title: string
  onClose: () => void
  onSaved: (project: Project) => void
}

/** 加载项目默认提示词后打开生成表单；重开时重新取当前项目信息。 */
export default function CoverGenerateModal(props: Props) {
  const loader = useCallback(() => coverPrompt(props.projectId), [props.projectId])
  const { data, loading, error, reload } = useLoader(loader)
  if (data && !loading) return <CoverGenerateForm {...props} initialPrompt={data.prompt} />
  return (
    <Modal open title="AI 生成封面" onCancel={props.onClose} footer={<Button onClick={props.onClose}>取消</Button>}>
      {error ? (
        <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重试</Button>} />
      ) : (
        <div style={{ textAlign: 'center', padding: 32 }}><Spin aria-label="正在准备封面提示词" /></div>
      )}
    </Modal>
  )
}

function CoverGenerateForm({ projectId, title, initialPrompt, onClose, onSaved }: Props & { initialPrompt: string }) {
  const { message } = App.useApp()
  const [prompt, setPrompt] = useState(initialPrompt)
  const [configId, setConfigId] = useState<number | null>(null)
  const [preview, setPreview] = useState<CoverPreview | null>(null)
  const [generating, setGenerating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const configs = useAIConfigStore((s) => s.configs)
  const configsLoaded = useAIConfigStore((s) => s.loaded)
  const hasImageConfig = configsLoaded && configs.some((c) => c.model_type === 'image')
  const controller = useRef<AbortController | null>(null)
  const operation = useRef<'generate' | 'save' | null>(null)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      controller.current?.abort()
    }
  }, [])

  const handleClose = () => {
    if (operation.current === 'save') return
    controller.current?.abort()
    onClose()
  }

  const handleGenerate = async () => {
    if (operation.current || !hasImageConfig) return
    if (!prompt.trim()) return setError('请输入封面提示词')
    const requestController = new AbortController()
    controller.current = requestController
    operation.current = 'generate'
    setGenerating(true)
    setError(null)
    try {
      const selected = configs.find((c) => c.id === configId && c.model_type === 'image')
      const result = await generateCover(projectId, {
        prompt: prompt.trim(),
        ...(selected ? { config_id: selected.id } : {}),
      }, requestController.signal)
      if (mounted.current && !requestController.signal.aborted) setPreview(result)
    } catch (err) {
      if (mounted.current && !requestController.signal.aborted) setError(errorMessage(err))
    } finally {
      operation.current = null
      if (mounted.current) setGenerating(false)
    }
  }

  const handleSave = async () => {
    if (!preview || operation.current) return
    operation.current = 'save'
    setSaving(true)
    setError(null)
    try {
      const updated = await uploadCover(projectId, coverPreviewBlob(preview))
      if (mounted.current) {
        onSaved(updated)
        message.success('已设为项目封面')
        onClose()
      }
    } catch (err) {
      if (mounted.current) setError(errorMessage(err))
    } finally {
      operation.current = null
      if (mounted.current) setSaving(false)
    }
  }

  return (
    <Modal
      open
      title="AI 生成封面"
      width={640}
      onCancel={handleClose}
      closable={!saving}
      keyboard={!saving}
      maskClosable={!saving}
      footer={[
        <Button
          key="generate"
          icon={preview ? <ReloadOutlined /> : <PictureOutlined />}
          loading={generating}
          disabled={saving || !hasImageConfig || !prompt.trim()}
          onClick={() => void handleGenerate()}
        >
          {preview ? '重新生成' : '生成封面'}
        </Button>,
        <Button key="cancel" disabled={saving} onClick={handleClose}>取消</Button>,
        <Button key="save" type="primary" loading={saving} disabled={!preview || generating} onClick={() => void handleSave()}>
          设为封面
        </Button>,
      ]}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">为《{title}》生成封面。可修改提示词，预览满意后再设为封面。</Typography.Text>
        <Form layout="vertical" disabled={generating || saving}>
          <Form.Item label="图片模型" htmlFor="cover-image-model">
            <AIConfigSelect id="cover-image-model" modelType="image" value={configId} onChange={setConfigId} disabled={generating || saving} />
          </Form.Item>
          <Form.Item label="封面提示词" htmlFor="cover-prompt" style={{ marginBottom: 8 }}>
            <Input.TextArea
              id="cover-prompt"
              aria-label="封面提示词"
              aria-describedby="cover-prompt-count"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              maxLength={2000}
              autoSize={{ minRows: 5, maxRows: 10 }}
            />
            <Typography.Text
              id="cover-prompt-count"
              type="secondary"
              style={{ display: 'block', textAlign: 'right', marginTop: 4 }}
            >
              {prompt.length} / 2000
            </Typography.Text>
          </Form.Item>
        </Form>
        {error && <Alert type="error" showIcon message={error} />}
        {generating && <Typography.Text role="status">正在生成封面，可能需要一些时间…</Typography.Text>}
        {preview && (
          <Spin spinning={generating}>
            <div style={{ textAlign: 'center' }}>
              <img
                src={`data:${preview.mime_type};base64,${preview.image_base64}`}
                alt={`《${title}》的待确认封面`}
                style={{ display: 'block', maxWidth: '100%', maxHeight: 360, margin: '0 auto', borderRadius: 4 }}
              />
              <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                此为预览；点击「设为封面」后保存。
              </Typography.Text>
            </div>
          </Spin>
        )}
      </Space>
    </Modal>
  )
}
