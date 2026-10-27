import { DeleteOutlined, InboxOutlined } from '@ant-design/icons'
import { App, Button, Empty, List, Popconfirm, Space, Spin, Tag, Typography, Upload } from 'antd'
import { useState } from 'react'
import { bookImportApi } from '../../api/bookImport'
import { errorMessage } from '../../api/client'
import type { BookImport, BookImportBrief } from '../../api/types'
import { BOOK_ACCEPT, BOOK_MAX_PENDING, formatChars, precheckBookFile, remainingDays } from '../../utils/bookImport'
import { formatTime } from '../../utils/time'

interface Props {
  pending: BookImportBrief[]
  pendingLoading: boolean
  /** 当前正在校正的解析 id（列表中标注） */
  currentId: number | null
  onUploaded: (record: BookImport) => void
  onOpen: (id: number) => void
  onDeleted: (id: number) => void
}

/** 第 1 步：拖拽上传（解析在上传请求内同步完成）+ 未完成的解析列表（继续 / 删除 / 剩余天数）。 */
export default function UploadStep({ pending, pendingLoading, currentId, onUploaded, onOpen, onDeleted }: Props) {
  const { message } = App.useApp()
  const [uploading, setUploading] = useState(false)
  const [deleting, setDeleting] = useState<number | null>(null)

  const handleFile = async (file: File) => {
    const problem = precheckBookFile(file, pending.length)
    if (problem) {
      message.error(problem)
      return
    }
    setUploading(true)
    try {
      const record = await bookImportApi.upload(file)
      message.success(`解析完成：切出 ${record.chapter_count} 章`)
      onUploaded(record)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (id: number) => {
    setDeleting(id)
    try {
      await bookImportApi.remove(id)
      message.success('已删除')
      onDeleted(id)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setDeleting(null)
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Spin spinning={uploading} tip="正在解析并切章…">
        <Upload.Dragger
          accept={BOOK_ACCEPT}
          multiple={false}
          showUploadList={false}
          disabled={uploading}
          beforeUpload={(file) => {
            void handleFile(file)
            return Upload.LIST_IGNORE
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽小说文件到这里</p>
          <p className="ant-upload-hint">
            支持 txt（UTF-8 / GBK / Big5）、md、docx、epub，单个文件 ≤ 20MB、正文 ≤ 500 万字；不支持 PDF。
            解析结果保存 7 天，最多保留 {BOOK_MAX_PENDING} 个未导入的解析。
          </p>
        </Upload.Dragger>
      </Spin>
      <div>
        <Typography.Title level={5} style={{ marginTop: 0 }}>
          未完成的解析（{pending.length}/{BOOK_MAX_PENDING}）
        </Typography.Title>
        <List<BookImportBrief>
          loading={pendingLoading}
          dataSource={pending}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有未完成的解析" /> }}
          renderItem={(b) => (
            <List.Item
              key={b.id}
              style={{ flexWrap: 'wrap', gap: 8 }}
              actions={[
                <Button key="open" type="link" size="small" onClick={() => onOpen(b.id)}>
                  {currentId === b.id ? '回到校正' : '继续'}
                </Button>,
                <Popconfirm
                  key="del"
                  title="删除这条解析？"
                  description="切章结果与手工校正都会删除，需要时可重新上传。"
                  okText="删除"
                  cancelText="取消"
                  onConfirm={() => handleDelete(b.id)}
                >
                  <Button type="link" size="small" danger icon={<DeleteOutlined />} loading={deleting === b.id}>
                    删除
                  </Button>
                </Popconfirm>,
              ]}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <Space wrap size={4}>
                  <Typography.Text strong ellipsis={{ tooltip: b.filename }} style={{ maxWidth: '100%' }}>
                    《{b.book_title || b.filename}》
                  </Typography.Text>
                  {currentId === b.id && <Tag color="processing">校正中</Tag>}
                </Space>
                <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                  {b.filename} · {b.chapter_count} 章 · {formatChars(b.total_chars)} · 更新于 {formatTime(b.updated_at)} · 剩余{' '}
                  {remainingDays(b.expires_at)} 天
                </Typography.Text>
              </div>
            </List.Item>
          )}
        />
      </div>
    </Space>
  )
}
