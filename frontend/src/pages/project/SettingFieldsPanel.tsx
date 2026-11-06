import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Checkbox, Empty, Modal, Select, Skeleton, Space, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { useBlocker, useLocation } from 'react-router-dom'
import { SETTING_ENTRY_TYPE_OPTIONS } from '../../api/settingFields'
import type { SettingEntryType } from '../../api/types'
import SettingFieldConfigEditor from '../../components/settings/SettingFieldConfigEditor'
import SettingFieldDefinitionCard from '../../components/settings/SettingFieldDefinitionCard'
import { configurationDirty } from '../../components/settings/settingFieldsController'
import { useSettingFieldConfiguration } from '../../components/settings/useSettingFieldConfiguration'
import { settingFieldTypeFilter } from '../../components/settings/settingFieldDraft'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'

/** 字段定义属于作品，条目起始值和章节变化在各自档案中独立保存。 */
export default function SettingFieldsPanel() {
  const pid = useProjectId()
  const user = useAuthStore((state) => state.user)
  const { ready, canRead } = useProjectAccess(pid)
  if (!ready) return <Skeleton active />
  if (!canRead || !user) return <Alert type="warning" message="当前账号无权查看作品字段配置" />
  return <SettingFieldsWorkspace key={`${pid}:${user.id}`} projectId={pid} actorId={user.id} />
}

function SettingFieldsWorkspace({ projectId, actorId }: { projectId: number; actorId: number }) {
  const { state, controller } = useSettingFieldConfiguration(projectId, actorId)
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const user = useAuthStore((auth) => auth.user)
  const location = useLocation()
  const [filter, setFilter] = useState<{ search: string; types: SettingEntryType[] }>({ search: location.search, types: settingFieldTypeFilter(location.search) })
  const types = filter.search === location.search ? filter.types : settingFieldTypeFilter(location.search)
  const setTypes = (types: SettingEntryType[]) => setFilter({ search: location.search, types })
  const [includeArchived, setIncludeArchived] = useState(false)
  const dirty = configurationDirty(state.editor)
  const blocker = useBlocker(dirty)
  const busy = state.loading || state.saving || state.reviewing
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirty])
  const items = (state.data?.items ?? []).filter((item) => (includeArchived || !item.archived) && (!types.length || types.some((type) => item.applicable_types.includes(type))))
    .sort((a, b) => a.sort - b.sort || a.id - b.id)
  const editorKey = state.editor ? `${state.editor.kind}:${state.editor.original?.id ?? 'new'}:${state.editor.open}:${state.editor.expectedRevision}:${state.editor.reviewRequired}:${authorizationKey(user)}:${canEdit}` : 'none'
  return <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
    <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
      <div>
        <Typography.Title level={3} style={{ margin: 0 }}>设定字段</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ margin: '8px 0 0' }}>为本作品的角色、世界观、组织和职业配置可复用字段。具体内容及章节变化在各条目中填写。</Typography.Paragraph>
      </div>
      <Space wrap>
        <Button icon={<ReloadOutlined />} disabled={busy} onClick={() => { void controller.refresh() }}>刷新配置</Button>
        {canEdit && <Button type="primary" icon={<PlusOutlined />} disabled={busy || !state.data} onClick={() => { if (canEditNow()) controller.openDefinition(null) }}>新建字段</Button>}
      </Space>
    </Space>
    {!canEdit && <Alert type="info" showIcon message="当前为只读，可查看字段定义、选项和停用状态。" />}
    {state.notice && !state.editor?.open && <Alert type="info" showIcon message={state.notice} />}
    {dirty && !state.editor?.open && <Alert type="warning" showIcon message="你有未保存的字段配置草稿"
      action={<Button size="small" onClick={controller.resumeEditor}>继续查看草稿</Button>} />}
    {state.error && !state.editor?.open && <Alert type="error" showIcon message={state.error}
      action={<Button size="small" disabled={busy} onClick={() => { void controller.refresh() }}>重试读取</Button>} />}
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <div style={{ width: '100%', maxWidth: 560 }}>
        <Typography.Text>按适用类型查看</Typography.Text>
        <Select<SettingEntryType[]> mode="multiple" aria-label="筛选字段适用类型" placeholder="全部设定类型" value={types}
          allowClear options={SETTING_ENTRY_TYPE_OPTIONS} onChange={setTypes} style={{ width: '100%', marginTop: 8 }} />
      </div>
      <Checkbox checked={includeArchived} onChange={(event) => setIncludeArchived(event.target.checked)}>显示已停用字段</Checkbox>
    </Space>
    {state.loading && !state.data ? <Skeleton active paragraph={{ rows: 8 }} />
      : !items.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={state.data?.total ? '此筛选下没有字段，可调整适用类型或显示已停用字段。' : '还没有共享字段，创建后即可在适用的条目中填写。'} />
        : items.map((definition) => <SettingFieldDefinitionCard key={definition.id} definition={definition} editable={canEdit} disabled={busy}
          onEdit={() => { if (canEditNow()) controller.openDefinition(definition) }}
          onOption={(option) => { if (canEditNow()) controller.openOption(definition, option) }} />)}
    <SettingFieldConfigEditor key={editorKey} projectId={projectId} state={state} controller={controller} />
    <Modal title="字段配置有未保存修改" open={blocker.state === 'blocked'} okText="放弃草稿并离开" cancelText="继续编辑"
      okButtonProps={{ danger: true, disabled: busy }} onCancel={() => { if (blocker.state === 'blocked') blocker.reset() }}
      onOk={() => { if (!busy && blocker.state === 'blocked') blocker.proceed() }}>
      {busy ? '正在处理请求，请等待完成。' : '离开页面会丢弃当前未保存的字段配置草稿。'}
    </Modal>
  </Space>
}
