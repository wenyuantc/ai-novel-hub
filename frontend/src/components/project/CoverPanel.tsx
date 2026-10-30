import { DeleteOutlined, PictureOutlined, UploadOutlined } from '@ant-design/icons'
import { App, Button, Popconfirm, Space, Typography, Upload } from 'antd'
import { lazy, Suspense, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { coverUrl, deleteCover, uploadCover } from '../../api/projects'
import type { Project } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectStore } from '../../stores/project'
import { COVER_ACCEPT, precheckCoverUpload } from '../../utils/projectCover'
import BookCover from '../shelf/BookCover'

const CoverGenerateModal = lazy(() => import('./CoverGenerateModal'))

interface Props {
  project: Project
}

/** 项目封面展示与管理：上传立即替换，AI 生成需预览确认。 */
export default function CoverPanel({ project }: Props) {
  const { canUseAI } = useProjectAccess(project.id)
  const { message } = App.useApp()
  const [mutating, setMutating] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const busy = useRef(false)
  const imageUrl = coverUrl(project)

  const applyCover = (updated: Project) => {
    const current = useProjectStore.getState().project
    // 只合并封面版本，保留基本信息表单的 key 与尚未保存的编辑。
    if (current?.id === updated.id) {
      useProjectStore.getState().setProject({ ...current, cover_updated_at: updated.cover_updated_at })
    }
  }

  const handleUpload = async (file: File) => {
    if (busy.current) return
    const error = precheckCoverUpload(file)
    if (error) return void message.error(error)
    busy.current = true
    setMutating(true)
    try {
      applyCover(await uploadCover(project.id, file))
      message.success('封面已更新')
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      busy.current = false
      setMutating(false)
    }
  }

  const handleDelete = async () => {
    if (busy.current) return
    busy.current = true
    setMutating(true)
    try {
      applyCover(await deleteCover(project.id))
      message.success('封面已删除')
    } catch (err) {
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
          disabled={mutating || generateOpen}
          beforeUpload={(file) => {
            void handleUpload(file)
            return Upload.LIST_IGNORE
          }}
        >
          <Button icon={<UploadOutlined />} loading={mutating} disabled={generateOpen}>上传</Button>
        </Upload>
        {canUseAI && <Button icon={<PictureOutlined />} disabled={mutating} onClick={() => setGenerateOpen(true)}>AI 生成</Button>}
        {imageUrl && (
          <Popconfirm
            title="删除当前封面？"
            description="删除后将恢复文字占位封面。"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={handleDelete}
            disabled={mutating || generateOpen}
          >
            <Button danger size="small" type="text" icon={<DeleteOutlined />} disabled={mutating || generateOpen}>
              删除封面
            </Button>
          </Popconfirm>
        )}
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>JPG / PNG / WebP，最大 10MB</Typography.Text>
      {canUseAI && generateOpen && (
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
