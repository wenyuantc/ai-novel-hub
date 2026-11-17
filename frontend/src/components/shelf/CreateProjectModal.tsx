import { App, Collapse, Form, Input, Modal, Radio, Typography } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { projectApi } from '../../api/projects'
import type { ProjectCreate } from '../../api/types'
import { capturePenNameActor, usePenNameIdentity } from '../../hooks/usePenNames'
import { useAuthStore } from '../../stores/auth'
import { readCreationIntent } from '../../utils/novelTemplateCreation'
import { projectInfoBody, type ProjectInfoValues } from '../../utils/projectFields'
import ProjectInfoFields from '../ProjectInfoFields'
import StyleSelect from '../styles/StyleSelect'
import PenNameSelect from './PenNameSelect'

const NovelTemplateCreateModal = lazy(() => import('../novelTemplates/NovelTemplateCreateModal'))
type CreatedDestination = 'wizard' | 'overview'
interface Props { open: boolean; onClose: () => void; onCreated: (id: number, destination: CreatedDestination) => void }

/** 选择空白建书或个人模板；模板独立按需加载。 */
export default function CreateProjectModal({ open, onClose, onCreated }: Props) {
  const identity = usePenNameIdentity()
  const actorId = useAuthStore((state) => state.user?.id)
  return open ? <CreateChoice key={identity} actorId={actorId} onClose={onClose} onCreated={onCreated} /> : null
}

function CreateChoice({ actorId, onClose, onCreated }: Omit<Props, 'open'> & { actorId?: number }) {
  const [mode, setMode] = useState<'blank' | 'template'>(() => actorId && readCreationIntent(actorId) ? 'template' : 'blank')
  if (mode === 'template' && actorId) return <Suspense fallback={null}>
    <NovelTemplateCreateModal actorId={actorId} onClose={onClose} onSwitchBlank={() => setMode('blank')} onCreated={(id) => onCreated(id, 'overview')} />
  </Suspense>
  return <BlankProjectModal onClose={onClose} onCreated={(id) => onCreated(id, 'wizard')} onSwitchTemplate={() => setMode('template')} />
}

function BlankProjectModal({ onClose, onCreated, onSwitchTemplate }: { onClose: () => void; onCreated: (id: number) => void; onSwitchTemplate: () => void }) {
  const { message } = App.useApp()
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm<ProjectCreate & ProjectInfoValues>()
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const create = async () => {
    if (creating) return
    const actorCurrent = capturePenNameActor()
    const isCurrent = () => mounted.current && actorCurrent()
    if (!isCurrent()) return
    const values = await form.validateFields().catch(() => null)
    if (!values || !isCurrent()) return
    setCreating(true)
    try {
      const project = await projectApi.create({ title: values.title.trim(), genre: values.genre?.trim() ?? '', premise: values.premise?.trim() ?? '', default_style_id: values.default_style_id ?? null, pen_name_id: values.pen_name_id ?? null, ...projectInfoBody(values) }, { isCurrent })
      if (!isCurrent()) return
      message.success('作品已创建，进入创作向导')
      form.resetFields()
      onCreated(project.id)
    } catch (err) { if (isCurrent()) message.error(errorMessage(err)) } finally { if (isCurrent()) setCreating(false) }
  }
  return <Modal title="新建作品" open onOk={() => void create()} confirmLoading={creating} onCancel={() => { if (!creating) onClose() }} okText="创建并进入向导" cancelText="取消" cancelButtonProps={{ disabled: creating }} closable={!creating} maskClosable={!creating} destroyOnHidden>
    <Form form={form} layout="vertical" preserve={false} disabled={creating}>
      <Form.Item label="创建方式"><Radio.Group value="blank" onChange={onSwitchTemplate} options={[{ label: '空白创建', value: 'blank' }, { label: '使用小说模板', value: 'template' }]} /></Form.Item>
      <Form.Item name="title" label="书名" rules={[{ required: true, whitespace: true, message: '请输入书名' }, { max: 200, message: '不超过 200 字' }]}><Input placeholder="例如：星河尽头" /></Form.Item>
      <Form.Item name="genre" label="题材" rules={[{ max: 100, message: '不超过 100 字' }]}><Input placeholder="例如：玄幻 / 都市 / 科幻 / 悬疑" /></Form.Item>
      <Form.Item name="pen_name_id" label="作品笔名" extra="可在我的作品中管理笔名；未设置时使用账号名"><PenNameSelect /></Form.Item>
      <Form.Item name="premise" label="一句话设定" rules={[{ max: 5000, message: '不超过 5000 字' }]}><Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="故事的核心点子、主角与冲突" /></Form.Item>
      <Form.Item name="default_style_id" label="默认写作风格" extra="可在风格库中新建或从样文提取风格，之后在作品概览里修改"><StyleSelect placeholder="不用风格" /></Form.Item>
      <Collapse ghost size="small" items={[{ key: 'more', label: '更多信息（简介、主题、类型标签、卖点、读者、目标字数，可稍后填写）', children: <ProjectInfoFields /> }]} />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>只有一个模糊的想法？试试 <Link to="/inspiration">灵感模式</Link>，让 AI 帮你一步步定下书名、简介等立项信息。</Typography.Text>
    </Form>
  </Modal>
}
