import { PictureOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Space, Spin, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { coverPrompt } from '../../api/projects'
import type { CoverPreview, Project } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useAIConfigStore } from '../../stores/aiConfigs'
import { captureProjectScope, useProjectStore, type ProjectScope } from '../../stores/project'
import { useAuthStore } from '../../stores/auth'
import AIConfigSelect from '../AIConfigSelect'
import { coverAvailability, coverChoice, currentCoverScope, requestCoverForScope, sameCoverChoice, saveCoverForScope } from './coverGeneration'

interface Props {
  projectId: number
  title: string
  onClose: () => void
  onSaved: (project: Project, scope: ProjectScope) => void
}

/** 加载项目默认提示词后打开生成表单；重开时重新取当前项目信息。 */
export default function CoverGenerateModal(props: Props) {
  const loader = useCallback(async () => {
    const scope = captureProjectScope(props.projectId)
    if (!currentCoverScope(scope, props.projectId)) throw new Error('作品权限已变化，请重新打开。')
    const result = await coverPrompt(props.projectId)
    if (!currentCoverScope(scope, props.projectId)) throw new Error('作品权限已变化，请重新读取提示词。')
    return result
  }, [props.projectId])
  const { data, loading, error, reload } = useLoader(loader)
  if (data && !loading) return <CoverGenerateForm {...props} initialPrompt={data.prompt} initialWarning={data.warning} />
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

function CoverGenerateForm({ projectId, title, initialPrompt, initialWarning, onClose, onSaved }: Props & {
  initialPrompt: string; initialWarning: string | null
}) {
  const { message } = App.useApp()
  const [prompt, setPrompt] = useState(initialPrompt)
  const [configId, setConfigId] = useState<number | null>(null)
  const [preview, setPreview] = useState<{ value: CoverPreview; scope: ProjectScope } | null>(null)
  const [generating, setGenerating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const configs = useAIConfigStore((s) => s.configs)
  const configsLoaded = useAIConfigStore((s) => s.loaded)
  const project = useProjectStore((s) => s.project)
  const scopeVersion = useProjectStore((s) => s.scopeVersion)
  const user = useAuthStore((s) => s.user)
  const scope = captureProjectScope(projectId)
  const authorized = coverAvailability(project, user, projectId) && scope?.version === scopeVersion && currentCoverScope(scope, projectId)
  const choice = coverChoice(project?.access, configsLoaded, configs, configId)
  const visiblePreview = preview && currentCoverScope(preview.scope, projectId) ? preview.value : null
  const controller = useRef<AbortController | null>(null)
  const operation = useRef<'generate' | 'save' | null>(null)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    let previous = useProjectStore.getState().scopeVersion
    const unsubscribe = useProjectStore.subscribe((state) => {
      if (state.scopeVersion === previous) return
      previous = state.scopeVersion
      controller.current?.abort()
      setPreview((current) => current === null ? current : null)
    })
    return () => {
      mounted.current = false
      unsubscribe()
      controller.current?.abort()
    }
  }, [])

  const handleClose = () => {
    if (operation.current === 'save') return
    controller.current?.abort()
    onClose()
  }

  const handleGenerate = async () => {
    if (operation.current) return
    if (!prompt.trim()) return setError('请输入封面提示词')
    if (!scope || !currentCoverScope(scope, projectId)) return
    const currentConfigs = useAIConfigStore.getState()
    const selected = coverChoice(useProjectStore.getState().project?.access, currentConfigs.loaded, currentConfigs.configs, configId)
    if (!selected.allowed || !sameCoverChoice(choice, selected)) return setError('图片模型选择已变化，请重新确认。')
    const requestController = new AbortController()
    controller.current = requestController
    operation.current = 'generate'
    setGenerating(true)
    setError(null)
    try {
      const result = await requestCoverForScope(projectId, scope, {
        prompt: prompt.trim(),
        ...(selected.mode === 'actor' ? { config_id: selected.configId } : {}),
      }, selected.mode, requestController.signal)
      if (result && mounted.current && !requestController.signal.aborted && currentCoverScope(scope, projectId)) setPreview({ value: result, scope })
    } catch (err) {
      if (mounted.current && !requestController.signal.aborted && currentCoverScope(scope, projectId)) setError(errorMessage(err))
    } finally {
      operation.current = null
      if (mounted.current) setGenerating(false)
    }
  }

  const handleSave = async () => {
    if (!preview || !visiblePreview || operation.current) return
    const scope = preview.scope
    if (!currentCoverScope(scope, projectId)) return
    operation.current = 'save'
    setSaving(true)
    setError(null)
    try {
      const updated = await saveCoverForScope(projectId, scope, visiblePreview)
      if (updated && mounted.current && currentCoverScope(scope, projectId)) {
        onSaved(updated, scope)
        message.success('已设为项目封面')
        onClose()
      }
    } catch (err) {
      if (mounted.current && currentCoverScope(scope, projectId)) setError(errorMessage(err))
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
          icon={visiblePreview ? <ReloadOutlined /> : <PictureOutlined />}
          loading={generating}
          disabled={saving || !authorized || !choice.allowed || !prompt.trim()}
          onClick={() => void handleGenerate()}
        >
          {visiblePreview ? '重新生成' : '生成封面'}
        </Button>,
        <Button key="cancel" disabled={saving} onClick={handleClose}>取消</Button>,
        <Button key="save" type="primary" loading={saving} disabled={!authorized || !visiblePreview || generating} onClick={() => void handleSave()}>
          设为封面
        </Button>,
      ]}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">为《{title}》生成封面。可修改提示词，预览满意后再设为封面。</Typography.Text>
        {initialWarning && <Alert type="warning" showIcon message={initialWarning} />}
        {authorized && choice.mode === 'owner' && <Alert type="warning" showIcon message="可能使用作者授权的图片模型"
          description="你尚未配置图片模型；本次可能使用作者密钥并产生作者费用。实际来源以生成结果为准。" />}
        {choice.mode === 'invalid' && <Alert type="warning" showIcon message="所选图片模型已不可用，请重新选择。" />}
        <Form layout="vertical" disabled={generating || saving}>
          <Form.Item label="图片模型" htmlFor="cover-image-model">
            {choice.mode === 'owner' ? <Typography.Text>未选择本人模型，可使用作者授权的默认图片模型。</Typography.Text>
              : <AIConfigSelect id="cover-image-model" modelType="image" value={configId} onChange={setConfigId} disabled={generating || saving} />}
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
        {visiblePreview && (
          <Spin spinning={generating}>
            <div style={{ textAlign: 'center' }}>
              <img
                src={`data:${visiblePreview.mime_type};base64,${visiblePreview.image_base64}`}
                alt={`《${title}》的待确认封面`}
                style={{ display: 'block', maxWidth: '100%', maxHeight: 360, margin: '0 auto', borderRadius: 4 }}
              />
              <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                此为预览；点击「设为封面」后保存。
              </Typography.Text>
              <CoverModelSource source={visiblePreview.model_source} />
            </div>
          </Spin>
        )}
      </Space>
    </Modal>
  )
}

/** 缺少来源的旧作者响应不推断费用来源。 */
export function CoverModelSource({ source }: { source: CoverPreview['model_source'] }) {
  if (source === 'actor') return <Typography.Text type="secondary">本次使用你的图片模型。</Typography.Text>
  if (source === 'owner') return <Typography.Text type="warning">本次使用作者授权的图片模型，可能产生作者费用。</Typography.Text>
  return null
}
