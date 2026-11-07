import { Alert, App, Checkbox, Modal, Space, Spin, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { transferApi } from '../../api/transfer'
import type { BackupOptions } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { BACKUP_OPTION_ITEMS, backupSizeHint, DEFAULT_BACKUP_OPTIONS, estimateTotal, formatBytes } from '../../utils/transfer'

interface Props {
  projectId: number
  onClose: () => void
}

/**
 * 项目完整备份（ainovel.project JSON）：核心数据始终包含，四个可选项（默认只勾章节版本），
 * 按 backup/estimate 显示估算大小，下载为流式 JSON 附件。由调用方在打开时挂载（React.lazy）。
 */
export default function BackupModal({ projectId, onClose }: Props) {
  const { message } = App.useApp()
  const [options, setOptions] = useState<BackupOptions>(DEFAULT_BACKUP_OPTIONS)
  const [downloading, setDownloading] = useState(false)
  const loader = useCallback(() => transferApi.estimate(projectId), [projectId])
  const { data: estimate, loading, error } = useLoader(loader)

  const total = estimate ? estimateTotal(estimate, options) : null
  const hint = total === null ? null : backupSizeHint(total)

  const handleDownload = async () => {
    setDownloading(true)
    try {
      const filename = await transferApi.download(projectId, options)
      message.success(`已下载备份：${filename}`)
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setDownloading(false)
    }
  }

  return (
    <Modal
      open
      title="备份项目"
      okText="下载备份"
      cancelText="取消"
      onOk={() => void handleDownload()}
      confirmLoading={downloading}
      onCancel={onClose}
      width={560}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">
          备份为一个 ainovel.project v7 JSON 文件，可在项目列表页「导入备份」恢复为新项目。始终包含：项目信息、世界观、角色、组织、关系、职业、
          大纲与细纲、章节及独立场景（标题、正文、作者摘要、顺序、本地规划值及继承／覆盖状态）、设定演变及场景锚点、伏笔、章节分析、剧情记忆、知识库设置与文档文本{estimate ? `（约 ${formatBytes(estimate.core.bytes)}）` : ''}。
          章节版本历史（含场景结构快照）仍由下方可选项决定，不属于始终包含的数据。
          同时保留 AI 与提及设置及完整原内容；AI 隐藏不适用于备份文件，请勿将其当作公开分享文件。
          已在本书使用的系列设定会作为独立快照保存；不会导出系列归属或其他书的设定，恢复后不会自动接回原系列。
          后台任务、知识库分块与向量不导出，导入后会重新建立。
        </Typography.Text>
        {error && <Alert type="error" showIcon message={error} />}
        <Spin spinning={loading}>
          <Space direction="vertical">
            {BACKUP_OPTION_ITEMS.map((item) => (
              <Checkbox
                key={item.key}
                checked={options[item.key]}
                onChange={(e) => setOptions((o) => ({ ...o, [item.key]: e.target.checked }))}
              >
                {item.label}
                <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                  {item.hint}
                  {estimate ? `（${estimate[item.key].rows} 条，约 ${formatBytes(estimate[item.key].bytes)}）` : ''}
                </Typography.Text>
              </Checkbox>
            ))}
          </Space>
        </Spin>
        {total !== null && <Typography.Text>估算大小：约 {formatBytes(total)}</Typography.Text>}
        {hint && <Alert type={hint.type} showIcon message={hint.text} />}
      </Space>
    </Modal>
  )
}
