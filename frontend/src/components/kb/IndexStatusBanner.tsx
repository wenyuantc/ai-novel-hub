import { Alert, App, Button, Popconfirm, Tooltip } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { kbApi } from '../../api/kb'
import type { IndexStatus } from '../../api/types'
import { useTasksStore } from '../../stores/tasks'
import { collaboratorIndexBanner, COLLABORATOR_VECTOR_COST, indexBanner, kbActionError } from '../../utils/kb'

interface Props {
  projectId: number
  status: IndexStatus | undefined
  scope: 'kb' | 'memory'
  /** 重建任务创建后（刷新状态） */
  onChanged: () => void
  /**
   * collaborator：精简视图（不显示作者配置名、不出现「去模型配置」），全书重建先确认费用由作者承担。
   * 默认 owner（作者原行为）。
   */
  audience?: 'owner' | 'collaborator'
  /** 发起重建前实时复核（如编辑权限仍在）；返回 false 时不发请求 */
  canStart?: () => boolean
}

/** 索引状态横幅：建索引中 / 未配置向量模型 / 作者向量配置不可用（重建置灰）/ 需要重建（带「重建索引」按钮，创建 kb.index 重建任务）。 */
export default function IndexStatusBanner({ projectId, status, scope, onChanged, audience = 'owner', canStart }: Props) {
  const { message } = App.useApp()
  const track = useTasksStore((s) => s.track)
  const [starting, setStarting] = useState(false)
  const collaborator = audience === 'collaborator'
  const banner = collaborator ? collaboratorIndexBanner(status, scope) : indexBanner(status, scope)
  if (!banner) return null

  const rebuild = async () => {
    if (canStart && !canStart()) return
    setStarting(true)
    try {
      const task = await kbApi.rebuild(projectId)
      track(task)
      message.success('已开始重建索引，可在后台任务中查看进度')
      onChanged()
    } catch (err) {
      message.error(kbActionError(err))
      if (collaborator) onChanged()
    } finally {
      setStarting(false)
    }
  }

  const button = (
    <Button size="small" type="primary" loading={starting} onClick={collaborator ? undefined : () => void rebuild()}>
      {collaborator ? '全书重建' : '重建索引'}
    </Button>
  )

  return (
    <Alert
      type={banner.type}
      showIcon
      style={{ marginBottom: 12 }}
      message={banner.message}
      description={
        !collaborator && banner.configLink ? (
          <>
            {banner.description} <Link to="/settings/ai">去模型配置</Link>
          </>
        ) : (
          banner.description
        )
      }
      action={
        !collaborator && banner.rebuildDisabledReason ? (
          <Tooltip title={banner.rebuildDisabledReason}>
            <Button size="small" type="primary" disabled>重建索引</Button>
          </Tooltip>
        ) : banner.canRebuild ? (
          collaborator ? (
            <Popconfirm
              title="全书重建向量索引？"
              description={`会为全部待建资料生成向量，${COLLABORATOR_VECTOR_COST}；任务计入你的并发额度。`}
              okText="确认重建"
              cancelText="取消"
              onConfirm={() => rebuild()}
            >
              {button}
            </Popconfirm>
          ) : (
            button
          )
        ) : undefined
      }
    />
  )
}
