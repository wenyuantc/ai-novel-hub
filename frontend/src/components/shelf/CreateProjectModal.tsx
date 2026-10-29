import { App, Collapse, Form, Input, Modal, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { projectApi } from '../../api/projects'
import type { ProjectCreate } from '../../api/types'
import { projectInfoBody, type ProjectInfoValues } from '../../utils/projectFields'
import ProjectInfoFields from '../ProjectInfoFields'
import StyleSelect from '../styles/StyleSelect'

interface Props { open: boolean; onClose: () => void; onCreated: (id: number) => void }

/** 沿用原有立项字段、校验和创建接口，由侧栏与书库共同打开。 */
export default function CreateProjectModal({ open, onClose, onCreated }: Props) {
  const { message } = App.useApp()
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm<ProjectCreate & ProjectInfoValues>()
  const create = async () => {
    if (creating) return
    const values = await form.validateFields().catch(() => null)
    if (!values) return
    setCreating(true)
    try {
      const project = await projectApi.create({ title: values.title.trim(), genre: values.genre?.trim() ?? '', premise: values.premise?.trim() ?? '', default_style_id: values.default_style_id ?? null, ...projectInfoBody(values) })
      message.success('作品已创建，进入创作向导')
      form.resetFields()
      onCreated(project.id)
    } catch (err) { message.error(errorMessage(err)) } finally { setCreating(false) }
  }
  return (
    <Modal title="新建作品" open={open} onOk={() => void create()} confirmLoading={creating} onCancel={() => { if (!creating) onClose() }} okText="创建并进入向导" cancelText="取消" cancelButtonProps={{ disabled: creating }} closable={!creating} maskClosable={!creating} destroyOnHidden>
      <Form form={form} layout="vertical" preserve={false} disabled={creating}>
        <Form.Item name="title" label="书名" rules={[{ required: true, whitespace: true, message: '请输入书名' }, { max: 200, message: '不超过 200 字' }]}><Input placeholder="例如：星河尽头" /></Form.Item>
        <Form.Item name="genre" label="题材" rules={[{ max: 100, message: '不超过 100 字' }]}><Input placeholder="例如：玄幻 / 都市 / 科幻 / 悬疑" /></Form.Item>
        <Form.Item name="premise" label="一句话设定" rules={[{ max: 5000, message: '不超过 5000 字' }]}><Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="故事的核心点子、主角与冲突" /></Form.Item>
        <Form.Item name="default_style_id" label="默认写作风格" extra="可在风格库中新建或从样文提取风格，之后在作品概览里修改"><StyleSelect placeholder="不用风格" /></Form.Item>
        <Collapse ghost size="small" items={[{ key: 'more', label: '更多信息（简介、主题、类型标签、卖点、读者、目标字数，可稍后填写）', children: <ProjectInfoFields /> }]} />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>只有一个模糊的想法？试试 <Link to="/inspiration">灵感模式</Link>，让 AI 帮你一步步定下书名、简介等立项信息。</Typography.Text>
      </Form>
    </Modal>
  )
}
