import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Select, Skeleton, Space, Typography, theme } from 'antd'
import { useCallback, useState } from 'react'
import { agentApi } from '../../api/agent'
import { errorMessage } from '../../api/client'
import { useLoader } from '../../hooks/useLoader'
import AgentConversationView from './AgentConversationView'

export default function AgentPanel({ projectId }: { projectId: number }) {
  const { message, modal } = App.useApp()
  const { token } = theme.useToken()
  const loader = useCallback(() => agentApi.list(projectId), [projectId])
  const { data: conversations, loading, error, reload } = useLoader(loader)
  const [selected, setSelected] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [form] = Form.useForm<{ title: string }>()
  const current = conversations?.find((c) => c.id === selected) ?? conversations?.[0]
  const create = async () => {
    setBusy(true)
    try { const next = await agentApi.create(projectId); setSelected(next.id); reload() }
    catch (err) { message.error(errorMessage(err)) }
    finally { setBusy(false) }
  }
  const rename = async () => {
    if (!current) return
    const { title } = await form.validateFields()
    setBusy(true)
    try { await agentApi.update(projectId, current.id, { title: title.trim() }); setRenaming(false); reload() }
    catch (err) { message.error(errorMessage(err)) }
    finally { setBusy(false) }
  }
  const remove = () => {
    if (!current) return
    modal.confirm({ title: '删除这个会话？', content: '对话和修改记录会被删除，已经写入项目的内容会保留。', okText: '删除', okButtonProps: { danger: true }, onOk: async () => {
      try { await agentApi.remove(projectId, current.id); setSelected(null); reload() }
      catch (err) { message.error(errorMessage(err)); throw err }
    } })
  }
  return <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minWidth: 0, overflow: 'hidden' }}>
    <div style={{ padding: 12, borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
      <Space.Compact style={{ width: '100%' }}>
        <Select aria-label="智能体会话" style={{ flex: 1, minWidth: 0 }} value={current?.id} loading={loading} placeholder="选择会话" onChange={setSelected} options={conversations?.map((c) => ({ value: c.id, label: c.title || '新会话' }))} />
        <Button icon={<PlusOutlined />} aria-label="新建会话" title="新建会话" loading={busy} onClick={() => void create()} />
        <Button icon={<EditOutlined />} aria-label="重命名会话" title="重命名会话" disabled={!current || busy} onClick={() => { form.setFieldsValue({ title: current?.title }); setRenaming(true) }} />
        <Button icon={<DeleteOutlined />} aria-label="删除会话" title="删除会话" disabled={!current || busy} onClick={remove} />
      </Space.Compact>
    </div>
    {error && <Alert type="error" message={error} action={<Button onClick={reload}>重试</Button>} />}
    {!conversations && loading && <div style={{ padding: 16 }}><Skeleton active /></div>}
    {current ? <AgentConversationView key={current.id} pid={projectId} cid={current.id} onUpdated={reload} /> : !loading && <div style={{ padding: 24, textAlign: 'center' }}>
      <Typography.Paragraph>一起整理设定、检查剧情，或修改当前项目。</Typography.Paragraph>
      <Typography.Paragraph type="secondary">修改前会展示对比，确认后才写入项目。</Typography.Paragraph>
      <Button type="primary" icon={<PlusOutlined />} loading={busy} onClick={() => void create()}>开始新会话</Button>
    </div>}
    <Modal open={renaming} title="重命名会话" okText="保存" onCancel={() => setRenaming(false)} onOk={() => void rename()} confirmLoading={busy}>
      <Form form={form} layout="vertical"><Form.Item name="title" label="会话名称" rules={[{ required: true, whitespace: true, message: '请输入会话名称' }, { max: 200, message: '最多 200 字' }]}><Input maxLength={200} onPressEnter={() => void rename()} /></Form.Item></Form>
    </Modal>
  </div>
}
