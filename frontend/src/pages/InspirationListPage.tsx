import { BulbOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Empty, Form, Input, Modal, Progress, Space, Table, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { errorMessage } from '../api/client'
import { inspirationApi } from '../api/inspiration'
import type { InspirationSessionBrief } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { briefProgress, FLOW_LABELS, IDEA_MAX, SESSION_STATUS } from '../utils/inspiration'
import { formatTime } from '../utils/time'

/** 灵感会话列表（/inspiration）：新建（输入一个想法）、继续、删除；会话保存在服务端。 */
export default function InspirationListPage() {
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const loader = useCallback(() => inspirationApi.list(), [])
  const { data: sessions = [], loading, error, reload } = useLoader(loader)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm<{ idea: string }>()

  const handleCreate = async () => {
    const { idea } = await form.validateFields()
    setCreating(true)
    try {
      const s = await inspirationApi.create(idea.trim())
      setCreateOpen(false)
      navigate(`/inspiration/${s.id}`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = (s: InspirationSessionBrief) => {
    modal.confirm({
      title: `删除灵感会话「${s.title}」？`,
      content: '会话中的选项与已定内容都会删除；已经创建的项目不受影响。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await inspirationApi.remove(s.id)
          message.success('已删除')
          reload()
        } catch (err) {
          message.error(errorMessage(err))
        }
      },
    })
  }

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          <BulbOutlined /> 灵感模式
        </Typography.Title>
        <Space>
          <Button onClick={() => navigate('/projects')}>返回项目</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建
          </Button>
        </Space>
      </div>
      <Typography.Paragraph type="secondary">
        只有一个模糊的想法？AI 会按 书名 → 简介 → 主题 → 类型标签 → 核心卖点 → 目标读者 的顺序给出选项，帮你把立项信息逐步定下来，最后创建项目并进入创作向导。
      </Typography.Paragraph>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Table<InspirationSessionBrief>
        rowKey="id"
        loading={loading}
        dataSource={sessions}
        pagination={false}
        locale={{
          emptyText: (
            <Empty description="还没有灵感会话">
              <Button type="primary" onClick={() => setCreateOpen(true)}>
                从一个想法开始
              </Button>
            </Empty>
          ),
        }}
        onRow={(s) => ({ onClick: () => navigate(`/inspiration/${s.id}`), style: { cursor: 'pointer' } })}
        columns={[
          {
            title: '标题',
            dataIndex: 'title',
            render: (title: string) => <Typography.Text strong>{title || '（未命名）'}</Typography.Text>,
          },
          {
            title: '状态',
            width: 120,
            render: (_, s) => <Tag color={SESSION_STATUS[s.status].color}>{SESSION_STATUS[s.status].label}</Tag>,
          },
          {
            title: '进度',
            width: 260,
            render: (_, s) => (
              <Space direction="vertical" size={0} style={{ width: '100%' }}>
                <Progress percent={briefProgress(s)} size="small" format={() => `${s.done_steps}/${s.total_steps}`} />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  当前：{FLOW_LABELS[s.current] ?? s.current}
                  {s.stale_steps > 0 && (
                    <Tag color="warning" style={{ marginLeft: 8 }}>
                      {s.stale_steps} 步可能需要更新
                    </Tag>
                  )}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: '项目',
            width: 160,
            render: (_, s) => (
              <Space direction="vertical" size={0} onClick={(e) => e.stopPropagation()}>
                {s.converted_project_id !== null && <Link to={`/projects/${s.converted_project_id}`}>转为的项目</Link>}
                {s.project_id !== null && <Link to={`/projects/${s.project_id}`}>补全自项目</Link>}
                {s.converted_project_id === null && s.project_id === null && (
                  <Typography.Text type="secondary">—</Typography.Text>
                )}
              </Space>
            ),
          },
          { title: '更新时间', width: 170, render: (_, s) => formatTime(s.updated_at) },
          {
            title: '操作',
            width: 140,
            render: (_, s) => (
              <Space onClick={(e) => e.stopPropagation()}>
                <Button size="small" onClick={() => navigate(`/inspiration/${s.id}`)}>
                  继续
                </Button>
                <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={() => handleDelete(s)} />
              </Space>
            ),
          },
        ]}
      />

      <Modal
        title="新建灵感会话"
        open={createOpen}
        onOk={() => void handleCreate()}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        okText="开始"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="idea"
            label="你的想法"
            extra="一句话就够，比如题材、主角、一个画面或一个反转；之后可以随时补充"
            rules={[
              { required: true, whitespace: true, message: '请写下你的想法' },
              { max: IDEA_MAX, message: `不超过 ${IDEA_MAX} 字` },
            ]}
          >
            <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} placeholder="例如：一个只能看见别人剩余寿命的外卖员，卷进了城市连环失踪案" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
