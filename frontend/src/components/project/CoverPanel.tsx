import { DeleteOutlined, PictureOutlined, UploadOutlined } from '@ant-design/icons'
import { App, Button, Popconfirm, Space, Typography, Upload } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { coverUrl, deleteCover, uploadCover } from '../../api/projects'
import type { Project } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, type ProjectScope } from '../../stores/project'
import { useAuthStore } from '../../stores/auth'
import { COVER_ACCEPT, precheckCoverUpload } from '../../utils/projectCover'
import BookCover from '../shelf/BookCover'
import { coverAvailability, currentCoverScope } from './coverGeneration'

const CoverGenerateModal = lazy(() => import('./CoverGenerateModal'))

interface Props {
  project: Project
}

/** 项目封面展示与管理：上传立即替换，AI 生成需预览确认。 */
export default function CoverPanel({ project }: Props) {
  const { canUseAIEntitled, canEdit, canEditNow } = useProjectAccess(project.id)
  const user = useAuthStore((state) => state.user)
  const canGenerateCover = canUseAIEntitled && canEdit && coverAvailability(project, user, project.id)
  const scope = captureProjectScope(project.id)
  const { message } = App.useApp()
  const [mutating, setMutating] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const busy = useRef(false)
  const imageUrl = coverUrl(project)

  useEffect(() => useProjectStore.subscribe(() => {
    if (!currentCoverScope(captureProjectScope(project.id), project.id)) setGenerateOpen(false)
  }), [project.id])

  const applyCover = (updated: Project, scope: ProjectScope) => {
    if (!isProjectScopeCurrent(scope)) return
    const current = useProjectStore.getState().project
    // 只合并封面版本，保留基本信息表单的 key 与尚未保存的编辑。
    if (current?.id === updated.id) {
      useProjectStore.getState().setProject({ ...current, cover_updated_at: updated.cover_updated_at }, scope)
    }
  }

  const handleUpload = async (file: File) => {
    if (!canEditNow()) return
    if (busy.current) return
    const error = precheckCoverUpload(file)
    if (error) return void message.error(error)
    const scope = captureProjectScope(project.id)
    if (!scope) return
    busy.current = true
    setMutating(true)
    try {
      if (!canEditNow()) return
      const updated = await uploadCover(project.id, file)
      if (!canEditNow() || !isProjectScopeCurrent(scope)) return
      applyCover(updated, scope)
      message.success('封面已更新')
    } catch (err) {
      if (!canEditNow() || !isProjectScopeCurrent(scope)) return
      message.error(errorMessage(err))
    } finally {
      busy.current = false
      setMutating(false)
    }
  }

  const handleDelete = async () => {
    if (!canEditNow()) return
    if (busy.current) return
    const scope = captureProjectScope(project.id)
    if (!scope) return
    busy.current = true
    setMutating(true)
    try {
      if (!canEditNow()) return
      const updated = await deleteCover(project.id)
      if (!canEditNow() || !isProjectScopeCurrent(scope)) return
      applyCover(updated, scope)
      message.success('封面已删除')
    } catch (err) {
      if (!canEditNow() || !isProjectScopeCurrent(scope)) return
      message.error(errorMessage(err))
    } finally {
      busy.current = false
      setMutating(false)
    }
  }

  return (
    <Space direction="vertical" size="small" style={{ width: 180, maxWidth: '100%' }}>
      <BookCover title={project.title} genre={project.genre} coverUrl={imageUrl} />
      <Space size="small" wrap>
        <Upload
          accept={COVER_ACCEPT}
          showUploadList={false}
          disabled={!canEdit || mutating || generateOpen}
          beforeUpload={(file) => {
            void handleUpload(file)
            return Upload.LIST_IGNORE
          }}
        >
          <Button icon={<UploadOutlined />} loading={mutating} disabled={!canEdit || generateOpen}>上传</Button>
        </Upload>
        {canGenerateCover && <Button icon={<PictureOutlined />} disabled={mutating} onClick={() => {
          if (currentCoverScope(scope, project.id)) setGenerateOpen(true)
        }}>AI 生成</Button>}
        {imageUrl && (
          <Popconfirm
            title="删除当前封面？"
            description="删除后将恢复文字占位封面。"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={handleDelete}
            disabled={!canEdit || mutating || generateOpen}
          >
            <Button danger size="small" type="text" icon={<DeleteOutlined />} disabled={!canEdit || (mutating || generateOpen)}>
              删除封面
            </Button>
          </Popconfirm>
        )}
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>JPG / PNG / WebP，最大 10MB</Typography.Text>
      {canGenerateCover && generateOpen && (
        <Suspense fallback={null}>
          <CoverGenerateModal
            projectId={project.id}
            title={project.title}
            onClose={() => setGenerateOpen(false)}
            onSaved={applyCover}
          />
        </Suspense>
      )}
    </Space>
  )
}
