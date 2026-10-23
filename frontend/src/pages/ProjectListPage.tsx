import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Empty, Form, Input, Modal, Row, Spin, Statistic, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { errorMessage } from '../api/client'
import { PROJECT_STATUS, projectApi } from '../api/projects'
import type { Project, ProjectCreate } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { formatTime } from '../utils/time'

export default function ProjectListPage() {
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const loader = useCallback(() => projectApi.list(), [])
  const { data: projects = [], loading, error, reload } = useLoader(loader)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm<ProjectCreate>()

  const handleCreate = async () => {
    const values = await form.validateFields()
    setCreating(true)
    try {
      const p = await projectApi.create({
        title: values.title.trim(),
        genre: values.genre?.trim() ?? '',
        premise: values.premise?.trim() ?? '',
        style: values.style?.trim() ?? '',
      })
      message.success('项目已创建，进入创作向导')
      setCreateOpen(false)
      form.resetFields()
      navigate(`/projects/${p.id}/wizard`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = (p: Project) => {
    modal.confirm({
      title: `删除《${p.title}》？`,
      content: '项目下的世界观、角色、大纲和全部章节都将被删除，且无法恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await projectApi.remove(p.id)
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
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          我的项目
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          新建项目
        </Button>
      </div>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Spin spinning={loading}>
        {projects.length === 0 && !loading ? (
          <Empty description="还没有项目，新建一个开始创作吧" style={{ padding: 48 }}>
            <Button type="primary" onClick={() => setCreateOpen(true)}>
              新建项目
            </Button>
          </Empty>
        ) : (
          <Row gutter={[16, 16]}>
            {projects.map((p) => (
              <Col key={p.id} xs={24} sm={12} lg={8}>
                <Card
                  hoverable
                  onClick={() => navigate(`/projects/${p.id}`)}
                  title={<span title={p.title}>{p.title}</span>}
                  extra={<Tag color={PROJECT_STATUS[p.status].color}>{PROJECT_STATUS[p.status].label}</Tag>}
                  actions={[
                    <Button
                      key="del"
                      type="text"
                      danger
                      size="small"
                      icon={<DeleteOutlined />}
                      onClick={(e) => {
                        e.stopPropagation()
                        handleDelete(p)
                      }}
                    >
                      删除
                    </Button>,
                  ]}
                >
                  <div style={{ marginBottom: 8 }}>
                    {p.genre ? <Tag>{p.genre}</Tag> : <Typography.Text type="secondary">未设置题材</Typography.Text>}
                  </div>
                  <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }} style={{ minHeight: 44 }}>
                    {p.premise || '暂无设定'}
                  </Typography.Paragraph>
                  <Row>
                    <Col span={12}>
                      <Statistic title="章节数" value={p.chapter_count} valueStyle={{ fontSize: 20 }} />
                    </Col>
                    <Col span={12}>
                      <Statistic title="总字数" value={p.total_words} valueStyle={{ fontSize: 20 }} />
                    </Col>
                  </Row>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    更新于 {formatTime(p.updated_at)}
                  </Typography.Text>
                </Card>
              </Col>
            ))}
          </Row>
        )}
      </Spin>

      <Modal
        title="新建项目"
        open={createOpen}
        onOk={() => void handleCreate()}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        okText="创建并进入向导"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="title"
            label="书名"
            rules={[
              { required: true, whitespace: true, message: '请输入书名' },
              { max: 200, message: '不超过 200 字' },
            ]}
          >
            <Input placeholder="例如：星河尽头" />
          </Form.Item>
          <Form.Item name="genre" label="题材" rules={[{ max: 100, message: '不超过 100 字' }]}>
            <Input placeholder="例如：玄幻 / 都市 / 科幻 / 悬疑" />
          </Form.Item>
          <Form.Item name="premise" label="一句话设定" rules={[{ max: 5000, message: '不超过 5000 字' }]}>
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="故事的核心点子、主角与冲突" />
          </Form.Item>
          <Form.Item name="style" label="写作风格" rules={[{ max: 5000, message: '不超过 5000 字' }]}>
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="例如：轻松幽默、节奏明快、第一人称" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
