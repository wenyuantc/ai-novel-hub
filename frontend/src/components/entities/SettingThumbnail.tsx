import { PictureOutlined, RobotOutlined } from '@ant-design/icons'
import { Alert, Button, Image, Popconfirm, Space, Typography, theme } from 'antd'
import { lazy, Suspense, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ApiError, currentRequestGeneration, errorMessage, request, watchRequestGeneration } from '../../api/client'
import { deleteThumbnail, putThumbnail, thumbnailFileError, thumbnailUrl, type ThumbnailTarget } from '../../api/settingThumbnails'
import type { SettingThumbnailInfo } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, type ProjectScope } from '../../stores/project'
import { invalidateProject, type ProjectResource } from '../../utils/projectRefresh'
import { writeResultUnknown } from '../../utils/writeResult'
import { coverAvailability, currentCoverScope } from '../project/coverGeneration'
import { settleThumbnailWrite } from './thumbnailSync'

const subscribeGeneration = (notify: () => void) => watchRequestGeneration(notify).dispose
const SettingThumbnailGenerateModal = lazy(() => import('./SettingThumbnailGenerateModal'))

interface ImageProps {
  target: ThumbnailTarget
  thumbnail?: SettingThumbnailInfo | null
  name: string
  size?: number
}

/** 图像地址始终经过当前作品或系列的授权接口。 */
export function SettingThumbnail({ target, thumbnail, name, size = 56 }: ImageProps) {
  const { token } = theme.useToken()
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const generation = useSyncExternalStore(subscribeGeneration, currentRequestGeneration, currentRequestGeneration)
  const url = thumbnail ? `${thumbnailUrl(target, thumbnail.version)}&g=${generation}` : null
  const style = { width: size, height: size, borderRadius: token.borderRadius, flexShrink: 0, background: token.colorFillAlter, objectFit: 'cover' as const }
  if (url && failedUrl !== url) return <>
    {/* 列表卡片和树节点自身可点击，预览不触发外层的选择或跳转。 */}
    <button type="button" aria-label={`预览${name}的缩略图`} title="点击预览" onClick={(event) => { event.stopPropagation(); setPreviewUrl(url) }}
      style={{ width: size, height: size, padding: 0, border: 0, background: 'none', borderRadius: token.borderRadius, flexShrink: 0, lineHeight: 0, cursor: 'zoom-in' }}>
      <img src={url} alt={`${name}的缩略图`} loading="lazy" onError={() => setFailedUrl(url)} style={style} />
    </button>
    {previewUrl === url && <span onClick={(event) => event.stopPropagation()}>
      <Image src={url} alt={`${name}的缩略图`} style={{ display: 'none' }}
        preview={{ visible: true, src: url, onVisibleChange: (visible) => { if (!visible) setPreviewUrl(null) } }} />
    </span>}
  </>
  return <span role="img" aria-label={url ? `${name}的缩略图加载失败` : `${name}暂无缩略图`} style={{ ...style, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', color: token.colorTextTertiary }}><PictureOutlined /></span>
}

interface ControlsProps extends ImageProps {
  editable: boolean
  busy: boolean
  error: string | null
  disabled?: boolean
  onFile: (file: File) => void
  onDelete: () => void
  onGenerate?: () => void
  onRefresh?: () => void
}

export function ThumbnailControls({ editable, busy, disabled, error, onFile, onDelete, onGenerate, onRefresh, ...image }: ControlsProps) {
  const input = useRef<HTMLInputElement>(null)
  return <Space direction="vertical" size="small" style={{ maxWidth: '100%', marginBottom: 16 }}>
    <Space wrap align="center">
      <SettingThumbnail {...image} size={80} />
      {editable && <Space wrap>
        <input ref={input} type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" aria-label="选择缩略图" hidden onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) onFile(file)
        }} />
        <Button loading={busy} disabled={disabled} onClick={() => input.current?.click()}>{image.thumbnail ? '替换图片' : '上传图片'}</Button>
        {onGenerate && <Button icon={<RobotOutlined />} disabled={busy || disabled} onClick={onGenerate}>AI 生成图片</Button>}
        {image.thumbnail && <Popconfirm title="删除这张缩略图？" onConfirm={onDelete} okText="删除" cancelText="取消"><Button danger disabled={busy || disabled}>删除图片</Button></Popconfirm>}
      </Space>}
    </Space>
    {editable && <Typography.Text type="secondary">支持 JPG、PNG、WebP 单帧，最大 10 MiB，单边不超过 8192 像素、总像素不超过 1600 万。图片独立保存。</Typography.Text>}
    {error && <Alert type="error" showIcon message={error} action={onRefresh && <Button size="small" loading={busy} onClick={onRefresh}>重新读取图片</Button>} />}
  </Space>
}

export function ThumbnailNewHint() {
  return <Typography.Paragraph type="secondary">保存条目后可上传或生成缩略图。</Typography.Paragraph>
}

type ProjectTarget = Extract<ThumbnailTarget, { scope: 'project' }>
const PROJECT_RESOURCE: Record<ProjectTarget['kind'], ProjectResource> = {
  character: 'characters', world: 'world', organization: 'organizations', career: 'careers',
}
type ProjectEditorProps = ImageProps & {
  target: ProjectTarget
  editable: boolean
  /** 角色和世界观必须使用服务端单独投影的图片权限，不推断隐藏的共享归属。 */
  thumbnailEditable?: boolean
  onChange: (value: SettingThumbnailInfo | null, thumbnailEditable?: boolean) => void
}
export function ProjectThumbnailEditor(props: ProjectEditorProps) {
  const scopeVersion = useProjectStore((state) => state.scopeVersion)
  const generation = useSyncExternalStore(subscribeGeneration, currentRequestGeneration, currentRequestGeneration)
  const editable = props.editable && (props.target.kind === 'character' || props.target.kind === 'world'
    ? props.thumbnailEditable === true : true)
  return <ProjectThumbnailEditorInner key={`${props.target.projectId}:${props.target.kind}:${props.target.entryId}:${scopeVersion}:${generation}:${editable}`} {...props} editable={editable} />
}

function ProjectThumbnailEditorInner({ target, thumbnail, name, editable, onChange }: ProjectEditorProps) {
  const { canEditNow } = useProjectAccess(target.projectId)
  const project = useProjectStore((state) => state.project)
  const user = useAuthStore((state) => state.user)
  const gate = useRef({ mounted: false, serial: 0, pending: false })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refreshRequired, setRefreshRequired] = useState(false)
  const [generation, setGeneration] = useState<{ scope: ProjectScope; version: string | null } | null>(null)
  const canGenerate = editable && coverAvailability(project, user, target.projectId)
  useLayoutEffect(() => {
    const state = gate.current
    state.mounted = true
    state.pending = false
    return () => { state.mounted = false; state.serial += 1 }
  }, [target.projectId, target.kind, target.entryId, canEditNow, editable])

  const run = async (file?: File) => {
    const state = gate.current
    if (!editable || refreshRequired || generation || !canEditNow() || state.pending || !state.mounted) return
    const fileError = file ? thumbnailFileError(file) : null
    if (fileError) { setError(fileError); return }
    const serial = ++state.serial
    const current = () => state.mounted && state.serial === serial && canEditNow()
    const scope = captureProjectScope(target.projectId)
    state.pending = true
    setBusy(true)
    setError(null)
    try {
      await settleThumbnailWrite(async () => {
        const result = file ? await putThumbnail(target, file, thumbnail?.version ?? null, current)
          : await deleteThumbnail(target, thumbnail?.version ?? null, current)
        if (file && (!result || !('version' in result))) throw new Error('图片保存响应无效')
        return file && result && 'version' in result ? result : null
      }, current, () => isProjectScopeCurrent(scope, 'canRead'), onChange,
      () => invalidateProject(target.projectId, [PROJECT_RESOURCE[target.kind]]))
    } catch (err) {
      if (current()) {
        const needsRefresh = writeResultUnknown(err) || (err instanceof ApiError && [403, 404, 409].includes(err.status))
        setRefreshRequired(needsRefresh)
        setError(needsRefresh ? `${errorMessage(err)}。请先读取当前图片，文字草稿会保留。` : errorMessage(err))
      }
    } finally {
      if (current()) { state.pending = false; setBusy(false) }
    }
  }
  const refresh = async () => {
    const state = gate.current
    if (!canEditNow() || state.pending || !state.mounted) return
    const serial = ++state.serial
    const current = () => state.mounted && state.serial === serial && canEditNow()
    state.pending = true
    setBusy(true)
    try {
      const segment = { character: 'characters', world: 'world', organization: 'organizations', career: 'careers' }[target.kind]
      const entries = await request<Array<{ id: number; thumbnail?: SettingThumbnailInfo | null; thumbnail_editable?: boolean }>>(`/projects/${target.projectId}/${segment}`, { isCurrent: current })
      const entry = entries.find((item) => item.id === target.entryId)
      if (!entry) throw new Error('条目已不存在或无权查看')
      if (current()) {
        const thumbnailEditable = target.kind === 'character' || target.kind === 'world' ? entry.thumbnail_editable === true : undefined
        onChange(entry.thumbnail ?? null, thumbnailEditable)
        setRefreshRequired(false)
        setError(null)
      }
    } catch (err) {
      if (current()) setError(`${errorMessage(err)}。请重新读取图片。`)
    } finally {
      if (current()) { state.pending = false; setBusy(false) }
    }
  }
  return <>
    <ThumbnailControls target={target} thumbnail={thumbnail} name={name} editable={editable && !refreshRequired} busy={busy} disabled={generation !== null} error={error}
      onFile={(file) => void run(file)} onDelete={() => void run()} onRefresh={refreshRequired ? () => void refresh() : undefined}
      onGenerate={canGenerate ? () => {
        const scope = captureProjectScope(target.projectId)
        if (scope && !busy && !gate.current.pending && !refreshRequired && currentCoverScope(scope, target.projectId)) {
          gate.current.pending = true
          setGeneration({ scope, version: thumbnail?.version ?? null })
        }
      } : undefined} />
    {generation && <Suspense fallback={null}><SettingThumbnailGenerateModal target={target} name={name} version={generation.version}
      current={() => currentCoverScope(generation.scope, target.projectId)} onClose={() => { gate.current.pending = false; setGeneration(null) }}
      onApplied={(result) => {
        if ('version' in result && currentCoverScope(generation.scope, target.projectId)) { gate.current.pending = false; onChange(result); setGeneration(null) }
      }}
      onConflict={(err) => {
        if (!currentCoverScope(generation.scope, target.projectId)) return
        gate.current.pending = false
        setGeneration(null)
        setRefreshRequired(true)
        setError(`${errorMessage(err)}。请先读取当前图片，文字草稿会保留。`)
      }}
      onOrphan={() => { if (isProjectScopeCurrent(generation.scope, 'canRead')) invalidateProject(target.projectId, [PROJECT_RESOURCE[target.kind]]) }}
    /></Suspense>}
  </>
}
