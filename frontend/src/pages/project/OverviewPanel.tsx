import { DownloadOutlined, ExperimentOutlined, RocketOutlined } from '@ant-design/icons'
import { App, Button, Card, Col, Descriptions, Form, Input, Row, Select, Space, Statistic, Switch, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { analysisApi } from '../../api/analysis'
import { errorMessage } from '../../api/client'
import { PROJECT_STATUS, projectApi } from '../../api/projects'
import type { Project, ProjectStatus, ProjectUpdate } from '../../api/types'
import ExportModal from '../../components/ExportModal'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { formatTime } from '../../utils/time'

export default function OverviewPanel() {
  const project = useProjectStore((s) => s.project)
  if (!project) return null
  // 以项目 id + 更新时间为 key，保存后表单以最新数据重建；分析设置不放在其中，切换开关不会重置未保存的表单
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <OverviewInner key={`${project.id}-${project.updated_at}`} project={project} />
      <AnalysisSettingsCard project={project} />
    </Space>
  )
}

/** 章节分析设置：自动分析开关（PATCH auto_analyze）与一键分析全部未分析 / 已过期章节。 */
function AnalysisSettingsCard({ project }: { project: Project }) {
  const { message } = App.useApp()
  const setProject = useProjectStore((s) => s.setProject)
  const refreshTasks = useTasksStore((s) => s.refresh)
  const [toggling, setToggling] = useState(false)
  const [queueing, setQueueing] = useState(false)

  const handleToggle = async (checked: boolean) => {
    setToggling(true)
    try {
      const updated = await projectApi.update(project.id, { auto_analyze: checked })
      // 只合并 auto_analyze：若整体替换，updated_at 变化会让以其为 key 的项目信息表单重建、丢掉未保存的修改
      const current = useProjectStore.getState().project
      if (current?.id === updated.id) setProject({ ...current, auto_analyze: updated.auto_analyze })
      message.success(checked ? '已开启自动分析' : '已关闭自动分析')
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setToggling(false)
    }
  }

  const handleAnalyzePending = async () => {
    setQueueing(true)
    try {
      const r = await analysisApi.analyzePending(project.id)
      if (r.queued > 0) message.success(`已加入 ${r.queued} 章分析，按章节顺序依次执行，可在后台任务中查看`)
      else message.info('没有需要分析的章节（未分析或已过期且正文非空的章节会被加入）')
      void refreshTasks()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setQueueing(false)
    }
  }

  return (
    <Card title="章节分析">
      <Space direction="vertical">
        <Space wrap>
          <Switch checked={project.auto_analyze} loading={toggling} onChange={(v) => void handleToggle(v)} />
          <Typography.Text>AI 生成 / 润色 / 按建议重写后自动分析本章</Typography.Text>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          开启后每章生成后会多一次 AI 调用：提炼摘要、情节点与钩子，回写角色 / 关系 / 境界 / 组织变化，给出评分与改进建议；
          下一章生成时用分析摘要作前情提要。手动编辑不会触发分析，只会把分析标记为「已过期」。
        </Typography.Text>
        <Button icon={<ExperimentOutlined />} loading={queueing} onClick={() => void handleAnalyzePending()}>
          分析全部未分析 / 已过期章节
        </Button>
      </Space>
    </Card>
  )
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
