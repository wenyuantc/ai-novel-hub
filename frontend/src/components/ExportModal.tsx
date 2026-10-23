import { App, Modal, Radio, Space, Table, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { chapterApi, CHAPTER_STATUS } from '../api/chapters'
import { errorMessage } from '../api/client'
import { exportProject, type ExportFormat } from '../api/projects'
import type { ChapterBrief } from '../api/types'
import { useLoader } from '../hooks/useLoader'

interface Props {
  projectId: number
  open: boolean
  onClose: () => void
}

/** 导出 TXT / Markdown，可勾选章节（不勾选则导出全部）。 */
export default function ExportModal({ projectId, open, onClose }: Props) {
  return open ? <ExportModalInner projectId={projectId} onClose={onClose} /> : null
}

function ExportModalInner({ projectId, onClose }: Omit<Props, 'open'>) {
  const { message } = App.useApp()
  const [format, setFormat] = useState<ExportFormat>('txt')
  const [selected, setSelected] = useState<number[]>([])
  const [exporting, setExporting] = useState(false)
  const loader = useCallback(() => chapterApi.list(projectId), [projectId])
  const { data: chapters = [], loading } = useLoader(loader)

  const handleExport = async () => {
    setExporting(true)
    try {
      const filename = await exportProject(projectId, format, selected)
      message.success(`已导出：${filename}`)
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setExporting(false)
    }
  }

  return (
    <Modal
      open
      title="导出小说"
      okText="导出"
      cancelText="取消"
      onOk={() => void handleExport()}
      confirmLoading={exporting}
      onCancel={onClose}
      width={640}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Space>
          <span>格式：</span>
          <Radio.Group value={format} onChange={(e) => setFormat(e.target.value as ExportFormat)}>
            <Radio.Button value="txt">TXT</Radio.Button>
            <Radio.Button value="md">Markdown</Radio.Button>
          </Radio.Group>
        </Space>
        <Typography.Text type="secondary">
          勾选要导出的章节；不勾选则导出全部章节（生成中的章节不会被导出）。
        </Typography.Text>
        <Table<ChapterBrief>
          size="small"
          rowKey="id"
          loading={loading}
          dataSource={chapters}
          pagination={false}
          scroll={{ y: 320 }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys.map(Number)),
            getCheckboxProps: (r) => ({ disabled: r.status === 'generating' }),
          }}
          columns={[
            { title: '章', dataIndex: 'chapter_no', width: 60 },
            { title: '标题', dataIndex: 'title', ellipsis: true },
            {
              title: '状态',
              dataIndex: 'status',
              width: 90,
              render: (s: ChapterBrief['status']) => <Tag color={CHAPTER_STATUS[s].color}>{CHAPTER_STATUS[s].label}</Tag>,
            },
            { title: '字数', dataIndex: 'word_count', width: 80 },
          ]}
        />
      </Space>
    </Modal>
  )
}
