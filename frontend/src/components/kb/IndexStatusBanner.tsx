import { Alert, App, Button } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { kbApi } from '../../api/kb'
import type { IndexStatus } from '../../api/types'
import { useTasksStore } from '../../stores/tasks'
import { indexBanner } from '../../utils/kb'

interface Props {
  projectId: number
  status: IndexStatus | undefined
  scope: 'kb' | 'memory'
  /** 重建任务创建后（刷新状态） */
  onChanged: () => void
}

/** 索引状态横幅：建索引中 / 未配置向量模型 / 需要重建（带「重建索引」按钮，创建 kb.index 重建任务）。 */
export default function IndexStatusBanner({ projectId, status, scope, onChanged }: Props) {
  const { message } = App.useApp()
  const track = useTasksStore((s) => s.track)
  const [starting, setStarting] = useState(false)
  const banner = indexBanner(status, scope)
  if (!banner) return null

  const rebuild = async () => {
    setStarting(true)
    try {
      const task = await kbApi.rebuild(projectId)
      track(task)
      message.success('已开始重建索引，可在后台任务中查看进度')
      onChanged()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setStarting(false)
    }
  }

  return (
    <Alert
      type={banner.type}
      showIcon
      style={{ marginBottom: 12 }}
      message={banner.message}
      description={
        status?.embedding_model === null ? (
          <>
            {banner.description} <Link to="/settings/ai">去模型配置</Link>
          </>
        ) : (
          banner.description
        )
      }
      action={
        banner.canRebuild ? (
          <Button size="small" type="primary" loading={starting} onClick={() => void rebuild()}>
            重建索引
          </Button>
        ) : undefined
      }
    />
  )
}
