import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { characterApi } from '../../api/story'
import type { Character, CharacterInput } from '../../api/types'
import WizardModal from '../../components/WizardModal'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'

const EMPTY: CharacterInput = { name: '', role_type: '', profile: '', relations_text: '', sort: null }

export default function CharactersPanel() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const loader = useCallback(() => characterApi.list(pid), [pid])
  const { data: items = [], loading, reload } = useLoader(loader)
  const [editing, setEditing] = useState<Character | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<CharacterInput>()

  const openEdit = (item: Character | null) => {
    setEditing(item)
    form.setFieldsValue(
      item
        ? {
            name: item.name,
            role_type: item.role_type,
            profile: item.profile,
            relations_text: item.relations_text,
            sort: item.sort,
          }
        : EMPTY,
    )
    setModalOpen(true)
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    const body: CharacterInput = {
      name: v.name.trim(),
      role_type: v.role_type?.trim() ?? '',
      profile: v.profile ?? '',
      relations_text: v.relations_text ?? '',
      sort: v.sort ?? undefined,
    }
    setSaving(true)
    try {
      if (editing) await characterApi.update(pid, editing.id, body)
      else await characterApi.create(pid, body)
      message.success('已保存')
      setModalOpen(false)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (item: Character) => {
    try {
      await characterApi.remove(pid, item.id)
      message.success('已删除')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  return (
    <Card
      title="角色"
      extra={
        <Space>
          <Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)}>
            AI 重新生成
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)}>
            新增角色
          </Button>
        </Space>
      }
    >
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <Empty description="暂无角色，可手动新增或使用 AI 生成" />
        ) : (
          <Row gutter={[16, 16]}>
            {items.map((c) => (
              <Col key={c.id} xs={24} md={12} xl={8}>
                <Card
                  size="small"
                  title={
                    <Space>
                      {c.name}
                      {c.role_type && <Tag color="purple">{c.role_type}</Tag>}
                    </Space>
                  }
                  extra={
                    <Space size={0}>
                      <Button type="link" size="small" onClick={() => openEdit(c)}>
                        编辑
                      </Button>
                      <Popconfirm
                        title="删除该角色？"
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                        onConfirm={() => handleDelete(c)}
                      >
                        <Button type="link" size="small" danger>
                          删除
                        </Button>
                      </Popconfirm>
                    </Space>
                  }
                >
                  <Descriptions size="small" column={1}>
                    <Descriptions.Item label="简介">
                      <Typography.Paragraph
                        style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                        ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
                      >
                        {c.profile || '-'}
                      </Typography.Paragraph>
                    </Descriptions.Item>
                    <Descriptions.Item label="关系">
                      <Typography.Paragraph
                        style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                        ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
                      >
                        {c.relations_text || '-'}
                      </Typography.Paragraph>
                    </Descriptions.Item>
                  </Descriptions>
                </Card>
              </Col>
            ))}
          </Row>
        )}
      </Spin>

      <Modal
        title={editing ? '编辑角色' : '新增角色'}
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
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="name"
                label="姓名"
                rules={[
                  { required: true, whitespace: true, message: '请输入姓名' },
                  { max: 100, message: '不超过 100 字' },
                ]}
              >
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="role_type" label="定位" rules={[{ max: 50, message: '不超过 50 字' }]}>
                <Input placeholder="例如：主角 / 反派 / 配角" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="profile" label="人物简介（外貌、性格、背景、能力等）">
            <Input.TextArea autoSize={{ minRows: 4, maxRows: 14 }} />
          </Form.Item>
          <Form.Item name="relations_text" label="人物关系">
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} placeholder="例如：与林晚是青梅竹马；视宗主为杀父仇人" />
          </Form.Item>
          <Form.Item name="sort" label="排序（越小越靠前，可留空）">
            <InputNumber style={{ width: 160 }} />
          </Form.Item>
        </Form>
      </Modal>

      <WizardModal
        projectId={pid}
        kind="characters"
        open={aiOpen}
        existingCount={items.length}
        onClose={() => setAiOpen(false)}
        onDone={() => reload()}
      />
    </Card>
  )
}
