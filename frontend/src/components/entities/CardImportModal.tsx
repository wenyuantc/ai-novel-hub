import { InboxOutlined } from '@ant-design/icons'
import { Alert, App, Button, Descriptions, Modal, Radio, Space, Table, Tag, theme, Typography, Upload } from 'antd'
import { useState } from 'react'
import { cardApi, defaultResolutions, importedName, MAX_CARD_FILE_BYTES, parseCardText, summarizeImport } from '../../api/cards'
import { errorMessage } from '../../api/client'
import type { CardImportResult, CardPreview, CardPreviewItem, CardResolution, CardResolutions } from '../../api/types'

interface Props {
  projectId: number
  open: boolean
  onClose: () => void
  onImported: () => void
}

/** 卡片导入：本地读取 JSON → 预览（新增 / 同名冲突 / 无法关联 / 职业新建或复用）→ 冲突逐项选择跳过或重命名 → 导入（单事务）。 */
export default function CardImportModal(props: Props) {
  if (!props.open) return null
  return <Inner {...props} />
}

function Inner({ projectId, onClose, onImported }: Props) {
  const { token } = theme.useToken()
  const { message } = App.useApp()
  const [data, setData] = useState<unknown>(null)
  const [fileName, setFileName] = useState('')
  const [preview, setPreview] = useState<CardPreview | null>(null)
  const [choices, setChoices] = useState<CardResolutions>({ characters: {}, organizations: {} })
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<CardImportResult | null>(null)

  const handleFile = async (file: File) => {
    if (file.size > MAX_CARD_FILE_BYTES) {
      message.error('卡片文件过大，上限为 2MB')
      return
    }
    const parsed = parseCardText(await file.text())
    if (parsed.error) {
      message.error(parsed.error)
      return
    }
    setLoading(true)
    try {
      const p = await cardApi.preview(projectId, parsed.data)
      setData(parsed.data)
      setFileName(file.name)
      setPreview(p)
      setChoices(defaultResolutions(p))
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
      const r = await cardApi.import(projectId, data, choices)
      setResult(r)
      message.success('导入完成')
      onImported()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const itemTable = (kind: 'characters' | 'organizations', items: CardPreviewItem[]) => (
    <Table<CardPreviewItem>
      size="small"
      rowKey="name"
      pagination={false}
      dataSource={items}
      columns={[
        { title: '名称', dataIndex: 'name' },
        {
          title: '状态',
          width: 90,
          render: (_, i) => (i.status === 'new' ? <Tag color="green">新增</Tag> : <Tag color="orange">同名冲突</Tag>),
        },
        {
          title: '处理',
          render: (_, i) =>
            i.status === 'new' ? (
              <Typography.Text type="secondary">直接导入</Typography.Text>
            ) : (
              <Radio.Group
                size="small"
                value={choices[kind][i.name] ?? 'skip'}
                onChange={(e) =>
                  setChoices((c) => ({ ...c, [kind]: { ...c[kind], [i.name]: e.target.value as CardResolution } }))
                }
              >
                <Radio value="skip">跳过（关联已有）</Radio>
                <Radio value="rename">重命名为「{i.rename_to ?? i.name}」</Radio>
              </Radio.Group>
            ),
        },
        {
          title: '导入后名称',
          render: (_, i) => importedName(i, choices[kind][i.name]),
        },
      ]}
    />
  )

  const charSum = preview ? summarizeImport(preview.characters, choices.characters) : null
  const orgSum = preview ? summarizeImport(preview.organizations, choices.organizations) : null

  return (
    <Modal open title="导入卡片" width={820} onCancel={onClose} maskClosable={false} footer={null} destroyOnHidden>
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
            <p>{fileName ? `已选择：${fileName}（点击重新选择）` : '点击或拖拽卡片 JSON 文件到这里'}</p>
            <p style={{ color: token.colorTextTertiary }}>格式 ainovel.cards v1 / v2（v2 含职业），上限 2MB</p>
          </Upload.Dragger>
        )}
        {preview && !result && (
          <>
            {preview.warnings.map((w) => (
              <Alert key={w} type="warning" showIcon message={w} />
            ))}
            <Typography.Text strong>
              角色（新建 {charSum?.created}，重命名 {charSum?.renamed}，跳过 {charSum?.skipped}）
            </Typography.Text>
            {itemTable('characters', preview.characters)}
            <Typography.Text strong>
              组织（新建 {orgSum?.created}，重命名 {orgSum?.renamed}，跳过 {orgSum?.skipped}）
            </Typography.Text>
            {itemTable('organizations', preview.organizations)}
            {(preview.careers ?? []).length > 0 && (
              <>
                <Typography.Text strong>
                  职业（新建 {preview.careers.filter((c) => c.status === 'new').length}，复用{' '}
                  {preview.careers.filter((c) => c.status === 'reuse').length}）
                </Typography.Text>
                <Table
                  size="small"
                  rowKey="name"
                  pagination={false}
                  dataSource={preview.careers}
                  columns={[
                    { title: '职业', dataIndex: 'name' },
                    {
                      title: '状态',
                      width: 110,
                      render: (_, c) =>
                        c.status === 'new' ? <Tag color="green">新建</Tag> : <Tag color="blue">复用同名职业</Tag>,
                    },
                    {
                      title: '阶段',
                      render: (_, c) =>
                        c.status === 'new' ? (
                          <Typography.Text type="secondary">按卡片阶梯新建</Typography.Text>
                        ) : c.stages_added.length > 0 ? (
                          `追加到阶梯末尾：${c.stages_added.join('、')}`
                        ) : (
                          <Typography.Text type="secondary">阶段均已存在</Typography.Text>
                        ),
                    },
                  ]}
                />
              </>
            )}
            <Typography.Text strong>人物关系（{preview.relationships.length}）</Typography.Text>
            <Table
              size="small"
              rowKey={(r) => `${r.a_name}-${r.b_name}`}
              pagination={{ pageSize: 8, hideOnSinglePage: true }}
              dataSource={preview.relationships}
              columns={[
                { title: '关系', render: (_, r) => `${r.a_name} 是 ${r.b_name} 的${r.role_a}` },
                {
                  title: '状态',
                  width: 100,
                  render: (_, r) => (r.status === 'ok' ? <Tag color="green">可关联</Tag> : <Tag color="red">无法关联</Tag>),
                },
              ]}
            />
            <Space>
              <Button type="primary" loading={loading} onClick={() => void handleImport()}>
                确认导入
              </Button>
              <Button onClick={onClose}>取消</Button>
            </Space>
          </>
        )}
        {result && (
          <>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="新建角色">{result.characters.filter((c) => c.action !== 'skipped').length}</Descriptions.Item>
              <Descriptions.Item label="跳过角色">{result.characters.filter((c) => c.action === 'skipped').length}</Descriptions.Item>
              <Descriptions.Item label="新建组织">{result.organizations.filter((c) => c.action !== 'skipped').length}</Descriptions.Item>
              <Descriptions.Item label="跳过组织">{result.organizations.filter((c) => c.action === 'skipped').length}</Descriptions.Item>
              <Descriptions.Item label="人物关系">{result.relationships_created}</Descriptions.Item>
              <Descriptions.Item label="组织成员">{result.members_created}</Descriptions.Item>
              {(result.careers ?? []).length > 0 && (
                <>
                  <Descriptions.Item label="新建职业">{result.careers.filter((c) => c.action === 'created').length}</Descriptions.Item>
                  <Descriptions.Item label="复用职业">
                    {result.careers.filter((c) => c.action === 'reused').length}
                    {result.careers.some((c) => c.stages_added > 0) &&
                      `（追加 ${result.careers.reduce((n, c) => n + c.stages_added, 0)} 个阶段）`}
                  </Descriptions.Item>
                </>
              )}
              <Descriptions.Item label="职业挂接">{result.careers_assigned ?? 0}</Descriptions.Item>
            </Descriptions>
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
