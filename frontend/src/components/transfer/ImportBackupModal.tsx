import { InboxOutlined } from '@ant-design/icons'
import { Alert, App, Button, Descriptions, Input, Modal, Space, Spin, Table, Tag, Typography, Upload } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { transferApi } from '../../api/transfer'
import type { ImportPreview, Project } from '../../api/types'
import { formatTime } from '../../utils/time'
import {
  BACKUP_MAX_BYTES,
  countRows,
  formatBytes,
  importFileErrors,
  includedOptionsText,
  isTokenExpired,
  precheckBackupFile,
  styleActionLabel,
  tokenExpiredLocally,
  type CountRow,
} from '../../utils/transfer'

interface Props {
  onClose: () => void
  /** 导入成功（调用方负责跳转到新项目） */
  onImported: (project: Project) => void
}

interface Loaded {
  preview: ImportPreview
  fileName: string
  fileSize: number
  /** 本地记录的令牌截止时间（ms） */
  expiresAt: number
}

const EXPIRED_TEXT = '导入凭据已过期，请重新选择备份文件'

/**
 * 导入项目备份（ainovel.project）：选择文件 → 预览（统计、版本、风格处理、警告；不合法时列出 400 的 errors）→
 * 可改书名 → 导入为新项目。令牌过期（本地计时或后端 400）时回到选择文件。由调用方在打开时挂载（React.lazy）。
 */
export default function ImportBackupModal({ onClose, onImported }: Props) {
  const { message, modal } = App.useApp()
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [title, setTitle] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [importing, setImporting] = useState(false)
  const [error, setError] = useState<{ detail: string; errors: string[] } | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => () => abortRef.current?.abort(), [])

  const reset = (reason?: string) => {
    setLoaded(null)
    setTitle('')
    setError(reason ? { detail: reason, errors: [] } : null)
  }

  const handleFile = async (file: File) => {
    const bad = precheckBackupFile(file)
    if (bad) {
      setLoaded(null)
      setError({ detail: bad, errors: [] })
      return
    }
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setPreviewing(true)
    setError(null)
    setLoaded(null)
    try {
      const preview = await transferApi.preview(file, ctrl.signal)
      setLoaded({ preview, fileName: file.name, fileSize: file.size, expiresAt: Date.now() + preview.expires_in * 1000 })
      setTitle(preview.final_title)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      setError({ detail: errorMessage(err), errors: importFileErrors(err) })
    } finally {
      if (abortRef.current === ctrl) setPreviewing(false)
    }
  }

  const handleImport = async () => {
    if (!loaded) return
    if (tokenExpiredLocally(loaded.expiresAt)) {
      reset(EXPIRED_TEXT)
      return
    }
    setImporting(true)
    try {
      const trimmed = title.trim()
      const result = await transferApi.importBackup({ token: loaded.preview.token, title: trimmed || null })
      message.success(`已导入为新项目《${result.project.title}》${result.task_id ? '，已排队重建检索索引' : ''}`)
      if (result.warnings.length > 0) {
        modal.warning({
          title: '导入完成，但有以下提示',
          width: 560,
          content: (
            <ul style={{ paddingLeft: 20, margin: 0 }}>
              {result.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          ),
        })
      }
      onImported(result.project)
    } catch (err) {
      if (isTokenExpired(err)) {
        reset(EXPIRED_TEXT)
      } else {
        setError({ detail: errorMessage(err), errors: importFileErrors(err) })
      }
    } finally {
      setImporting(false)
    }
  }

  const preview = loaded?.preview
  const titleInvalid = title.trim().length > 200

  return (
    <Modal
      open
      title="导入项目备份"
      width={680}
      onCancel={onClose}
      footer={[
        <Button key="cancel" onClick={onClose}>
          取消
        </Button>,
        loaded && (
          <Button key="repick" onClick={() => reset()} disabled={importing}>
            重新选择文件
          </Button>
        ),
        <Button
          key="ok"
          type="primary"
          disabled={!loaded || titleInvalid}
          loading={importing}
          onClick={() => void handleImport()}
        >
          导入为新项目
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        {!loaded && (
          <Spin spinning={previewing} tip="正在解析备份文件…">
            <Upload.Dragger
              accept=".json,application/json"
              showUploadList={false}
              multiple={false}
              disabled={previewing}
              beforeUpload={(file) => {
                void handleFile(file)
                return false
              }}
            >
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">点击或拖入备份文件（.json）</p>
              <p className="ant-upload-hint">
                由「备份项目」导出的 ainovel.project 文件，上限 {formatBytes(BACKUP_MAX_BYTES)}；导入总是新建一个项目。
              </p>
            </Upload.Dragger>
          </Spin>
        )}
        {error && (
          <Alert
            type="error"
            showIcon
            message={error.detail}
            description={
              error.errors.length > 0 ? (
                <ul style={{ paddingLeft: 20, margin: 0 }}>
                  {error.errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              ) : undefined
            }
          />
        )}
        {loaded && preview && (
          <>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="文件" span={2}>
                {loaded.fileName}（{formatBytes(loaded.fileSize)}）
              </Descriptions.Item>
              <Descriptions.Item label="原书名">{preview.title}</Descriptions.Item>
              <Descriptions.Item label="备份版本">
                v{preview.version}
                {preview.app_schema ? `（数据库 ${preview.app_schema}）` : ''}
              </Descriptions.Item>
              <Descriptions.Item label="导出时间">
                {preview.exported_at ? formatTime(preview.exported_at) : '未知'}
              </Descriptions.Item>
              <Descriptions.Item label="可选数据">{includedOptionsText(preview.options)}</Descriptions.Item>
            </Descriptions>
            <div>
              <Typography.Text>导入后的书名</Typography.Text>
              <Input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={200}
                status={titleInvalid ? 'error' : undefined}
                placeholder={preview.final_title}
                style={{ marginTop: 4 }}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                与已有项目重名时会自动加「（导入）」后缀；留空则使用原书名。
              </Typography.Text>
            </div>
            <Table<CountRow>
              size="small"
              rowKey="table"
              dataSource={countRows(preview.counts)}
              pagination={false}
              scroll={{ y: 240 }}
              locale={{ emptyText: '备份中只有项目信息' }}
              columns={[
                { title: '数据', dataIndex: 'label' },
                { title: '数量', dataIndex: 'count', width: 100, align: 'right' },
              ]}
            />
            {preview.styles.length > 0 && (
              <div>
                <Typography.Text>写作风格：</Typography.Text>
                <Space wrap size={[4, 4]} style={{ marginTop: 4 }}>
                  {preview.styles.map((s, i) => {
                    const a = styleActionLabel(s.action)
                    return (
                      <Tag key={i} color={a.color}>
                        {s.name} · {a.label}
                      </Tag>
                    )
                  })}
                </Space>
              </div>
            )}
            {preview.warnings.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`${preview.warnings.length} 条提示`}
                description={
                  <ul style={{ paddingLeft: 20, margin: 0 }}>
                    {preview.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                }
              />
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              知识库文档会重新分块，导入后即可用关键词检索；配置了向量模型时会自动排队重建向量索引。
              预览结果 {Math.round(preview.expires_in / 60)} 分钟内有效，过期后需重新选择文件。
            </Typography.Text>
          </>
        )}
      </Space>
    </Modal>
  )
}
