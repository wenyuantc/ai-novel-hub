import { DownloadOutlined, RocketOutlined } from '@ant-design/icons'
import { App, Button, Card, Col, Descriptions, Form, Input, Row, Select, Space, Statistic } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { PROJECT_STATUS, projectApi } from '../../api/projects'
import type { Project, ProjectStatus, ProjectUpdate } from '../../api/types'
import ExportModal from '../../components/ExportModal'
import { useProjectStore } from '../../stores/project'
import { formatTime } from '../../utils/time'

export default function OverviewPanel() {
  const project = useProjectStore((s) => s.project)
  if (!project) return null
  // 以项目 id + 更新时间为 key，保存后表单以最新数据重建
  return <OverviewInner key={`${project.id}-${project.updated_at}`} project={project} />
}

function OverviewInner({ project }: { project: Project }) {
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const setProject = useProjectStore((s) => s.setProject)
  const [form] = Form.useForm<ProjectUpdate>()
  const [saving, setSaving] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)

  const handleSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const updated = await projectApi.update(project.id, {
        title: v.title?.trim(),
        genre: v.genre?.trim() ?? '',
        premise: v.premise?.trim() ?? '',
        style: v.style?.trim() ?? '',
        status: v.status,
      })
      setProject(updated)
      message.success('已保存')
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = () => {
    modal.confirm({
      title: `删除《${project.title}》？`,
      content: '项目下的世界观、角色、大纲和全部章节都将被删除，且无法恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await projectApi.remove(project.id)
          message.success('已删除')
          navigate('/projects', { replace: true })
        } catch (err) {
          message.error(errorMessage(err))
        }
      },
    })
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="章节数" value={project.chapter_count} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="总字数" value={project.total_words} />
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card>
            <Space wrap>
              <Button icon={<RocketOutlined />} onClick={() => navigate(`/projects/${project.id}/wizard`)}>
                创作向导
              </Button>
              <Button type="primary" icon={<DownloadOutlined />} onClick={() => setExportOpen(true)}>
                导出 TXT / Markdown
              </Button>
            </Space>
          </Card>
        </Col>
      </Row>
      <Card title="基本信息">
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            title: project.title,
            genre: project.genre,
            premise: project.premise,
            style: project.style,
            status: project.status,
          }}
          style={{ maxWidth: 760 }}
        >
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item
                name="title"
                label="书名"
                rules={[
                  { required: true, whitespace: true, message: '请输入书名' },
                  { max: 200, message: '不超过 200 字' },
                ]}
              >
                <Input />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="genre" label="题材" rules={[{ max: 100, message: '不超过 100 字' }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="status" label="状态">
                <Select<ProjectStatus>
                  options={(Object.keys(PROJECT_STATUS) as ProjectStatus[]).map((s) => ({
                    value: s,
                    label: PROJECT_STATUS[s].label,
                  }))}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="premise" label="一句话设定" rules={[{ max: 5000, message: '不超过 5000 字' }]}>
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} />
          </Form.Item>
          <Form.Item name="style" label="写作风格" rules={[{ max: 5000, message: '不超过 5000 字' }]}>
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} />
          </Form.Item>
          <Space>
            <Button type="primary" loading={saving} onClick={() => void handleSave()}>
              保存
            </Button>
            <Button danger onClick={handleDelete}>
              删除项目
            </Button>
          </Space>
        </Form>
        <Descriptions size="small" style={{ marginTop: 24 }} column={2}>
          <Descriptions.Item label="创建时间">{formatTime(project.created_at)}</Descriptions.Item>
          <Descriptions.Item label="更新时间">{formatTime(project.updated_at)}</Descriptions.Item>
        </Descriptions>
      </Card>
      <ExportModal projectId={project.id} open={exportOpen} onClose={() => setExportOpen(false)} />
    </Space>
  )
}
