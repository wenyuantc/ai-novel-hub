import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
import { App, Button, Card, Empty, Form, Input, InputNumber, List, Modal, Popconfirm, Space, Spin, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { worldApi } from '../../api/story'
import type { WorldSetting, WorldSettingInput } from '../../api/types'
import WizardModal from '../../components/WizardModal'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'

export default function WorldPanel() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const loader = useCallback(() => worldApi.list(pid), [pid])
  const { data: items = [], loading, reload } = useLoader(loader)
  const [editing, setEditing] = useState<WorldSetting | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<WorldSettingInput>()

  const openEdit = (item: WorldSetting | null) => {
    setEditing(item)
    form.setFieldsValue(item ? { title: item.title, content: item.content, sort: item.sort } : { title: '', content: '', sort: null })
    setModalOpen(true)
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    const body: WorldSettingInput = { title: v.title.trim(), content: v.content ?? '', sort: v.sort ?? undefined }
    setSaving(true)
    try {
      if (editing) await worldApi.update(pid, editing.id, body)
      else await worldApi.create(pid, body)
      message.success('已保存')
      setModalOpen(false)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (item: WorldSetting) => {
    try {
      await worldApi.remove(pid, item.id)
      message.success('已删除')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  return (
    <Card
      title="世界观设定"
      extra={
        <Space>
          <Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)}>
            AI 重新生成
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)}>
            新增条目
          </Button>
        </Space>
      }
    >
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <Empty description="暂无世界观设定，可手动新增或使用 AI 生成" />
        ) : (
          <List
            dataSource={items}
            renderItem={(w) => (
              <List.Item
                actions={[
                  <Button key="edit" type="link" size="small" onClick={() => openEdit(w)}>
                    编辑
                  </Button>,
                  <Popconfirm
                    key="del"
                    title="删除该条目？"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => handleDelete(w)}
                  >
                    <Button type="link" size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={w.title}
                  description={
                    <Typography.Paragraph
                      style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                      ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
                    >
                      {w.content || '（无内容）'}
                    </Typography.Paragraph>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Spin>

      <Modal
        title={editing ? '编辑世界观条目' : '新增世界观条目'}
        open={modalOpen}
        onOk={() => void handleSave()}
        onCancel={() => setModalOpen(false)}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width={720}
        forceRender
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="title"
            label="标题"
            rules={[
              { required: true, whitespace: true, message: '请输入标题' },
              { max: 200, message: '不超过 200 字' },
            ]}
          >
            <Input placeholder="例如：力量体系 / 地理 / 势力" />
          </Form.Item>
          <Form.Item name="content" label="内容">
            <Input.TextArea autoSize={{ minRows: 6, maxRows: 20 }} />
          </Form.Item>
          <Form.Item name="sort" label="排序（越小越靠前，可留空）">
            <InputNumber style={{ width: 160 }} />
          </Form.Item>
        </Form>
      </Modal>

      <WizardModal
        projectId={pid}
        kind="world"
        open={aiOpen}
        existingCount={items.length}
        onClose={() => setAiOpen(false)}
        onDone={() => reload()}
      />
    </Card>
  )
}
