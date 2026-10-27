import { InboxOutlined } from '@ant-design/icons'
import { Alert, App, Button, Descriptions, Modal, Radio, Space, theme, Typography, Upload } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { MAX_OUTLINE_FILE_BYTES, outlineProApi, parseOutlineFileText } from '../../api/outlines'
import type { OutlineImportPreview, OutlineImportResult, OutlineMode } from '../../api/types'

interface Props {
  projectId: number
  open: boolean
  /** 当前已有大纲条数：0 时不提供「整体替换」 */
  existingCount: number
  onClose: () => void
  onImported: () => void
}

/** 大纲导入：本地读取 JSON（ainovel.outline v1）→ 预览统计与无法匹配的角色 → 追加到末尾 / 整体替换（替换前自动存快照）。 */
export default function OutlineImportModal(props: Props) {
  if (!props.open) return null
  return <Inner {...props} />
}

function Inner({ projectId, existingCount, onClose, onImported }: Props) {
  const { token } = theme.useToken()
  const { message } = App.useApp()
  const [data, setData] = useState<unknown>(null)
  const [fileName, setFileName] = useState('')
  const [preview, setPreview] = useState<OutlineImportPreview | null>(null)
  const [mode, setMode] = useState<OutlineMode>('append')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<OutlineImportResult | null>(null)

  const handleFile = async (file: File) => {
    if (file.size > MAX_OUTLINE_FILE_BYTES) {
      message.error('大纲文件过大，上限为 2MB')
      return
    }
    const parsed = parseOutlineFileText(await file.text())
    if (parsed.error) {
      message.error(parsed.error)
      return
    }
    setLoading(true)
    try {
      const p = await outlineProApi.importPreview(projectId, parsed.data)
      setData(parsed.data)
      setFileName(file.name)
      setPreview(p)
      setResult(null)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const handleImport = async () => {
    setLoading(true)
    try {
      const r = await outlineProApi.import(projectId, data, existingCount > 0 ? mode : 'append')
      setResult(r)
      message.success('导入完成')
      onImported()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal open title="导入大纲" width={720} onCancel={onClose} maskClosable={false} footer={null} destroyOnHidden>
      <Space direction="vertical" style={{ width: '100%' }}>
        {!result && (
          <Upload.Dragger
            accept=".json,application/json"
            showUploadList={false}
            disabled={loading}
            beforeUpload={(file) => {
              void handleFile(file)
              return false
            }}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p>{fileName ? `已选择：${fileName}（点击重新选择）` : '点击或拖拽大纲 JSON 文件到这里'}</p>
            <p style={{ color: token.colorTextTertiary }}>格式 ainovel.outline v1（含卷、章纲与细纲），上限 2MB</p>
          </Upload.Dragger>
        )}
        {preview && !result && (
          <>
            <Descriptions size="small" column={3} bordered>
              <Descriptions.Item label="卷">{preview.volumes}</Descriptions.Item>
              <Descriptions.Item label="章纲">{preview.outlines}</Descriptions.Item>
              <Descriptions.Item label="细纲">{preview.plans}</Descriptions.Item>
            </Descriptions>
            {preview.unresolved_characters.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`${preview.unresolved_characters.length} 个出场角色在本项目中找不到，导入时会忽略`}
                description={preview.unresolved_characters.join('、')}
              />
            )}
            {preview.warnings.map((w) => (
              <Alert key={w} type="warning" showIcon message={w} />
            ))}
            {existingCount > 0 ? (
              <Radio.Group value={mode} onChange={(e) => setMode(e.target.value as OutlineMode)}>
                <Space direction="vertical">
                  <Radio value="append">追加到末尾（卷号与章节号接在现有大纲之后）</Radio>
                  <Radio value="replace">整体替换现有 {existingCount} 条大纲</Radio>
                </Space>
              </Radio.Group>
            ) : (
              <Typography.Text type="secondary">当前项目没有大纲，将从第 1 卷第 1 章开始导入。</Typography.Text>
            )}
            {existingCount > 0 && mode === 'replace' && (
              <Alert
                type="info"
                showIcon
                message="替换前会自动保存大纲快照，可在「快照」中恢复；已写的章节不会删除，只解除与大纲的关联。"
              />
            )}
            <Space>
              <Button type="primary" loading={loading} onClick={() => void handleImport()}>
                {existingCount > 0 && mode === 'replace' ? '整体替换' : '导入'}
              </Button>
              <Button onClick={onClose}>取消</Button>
            </Space>
          </>
        )}
        {result && (
          <>
            <Alert
              type="success"
              showIcon
              message={`已${result.mode === 'replace' ? '替换' : '追加'}：${result.volumes} 卷、${result.outlines} 条章纲、${result.plans} 条细纲`}
              description={result.snapshot_id !== null ? `替换前的大纲已存为快照 #${result.snapshot_id}，可在「快照」中恢复。` : undefined}
            />
            {result.warnings.map((w) => (
              <Alert key={w} type="warning" showIcon message={w} />
            ))}
            <Button type="primary" onClick={onClose}>
              完成
            </Button>
          </>
        )}
      </Space>
    </Modal>
  )
}
