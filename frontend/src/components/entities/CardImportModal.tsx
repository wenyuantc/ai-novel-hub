import { InboxOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, List, Modal, Space, Tag, theme, Typography, Upload } from 'antd'
import type { CardPreview } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import CardDefinitionMappings from './CardDefinitionMappings'
import CardImportEntities from './CardImportEntities'
import CardImportResultView from './CardImportResultView'
import CardReferenceMappings from './CardReferenceMappings'
import { cardTagImportSummary, type CardTagSummary } from './cardImportMapping'
import { useCardImport } from './useCardImport'

interface Props { projectId: number; open: boolean; onClose: () => void; onImported: () => void }

/** 保留旧版名称关联；新版字段和 AI 关联通过显式映射及相应目录版本确认。 */
export default function CardImportModal(props: Props) {
  const user = useAuthStore((state) => state.user)
  const { canEdit } = useProjectAccess(props.projectId)
  if (!props.open) return null
  if (!user || !canEdit) return <Modal open title="导入卡片" onCancel={props.onClose} footer={<Button onClick={props.onClose}>关闭</Button>}>
    <Alert type="info" showIcon message="当前为只读，不能导入卡片。" />
  </Modal>
  const identity = authorizationKey(user)
  return <Inner key={`${props.projectId}:${user.id}:${identity}`} {...props} actorId={user.id} identity={identity} />
}

function TagSnapshot({ items }: { items: CardTagSummary[] }) {
  if (!items.length) return null
  return <List size="small" dataSource={items} pagination={{ pageSize: 10, hideOnSinglePage: true }} renderItem={(item) => <List.Item>
    <Space direction="vertical" size={8} style={{ width: '100%', minWidth: 0, overflowWrap: 'anywhere' }}>
      <Typography.Text strong>{item.name} · {item.kind === 'character' ? '角色' : item.kind === 'organization' ? '组织' : '职业'}</Typography.Text>
      <div>卡片标签：{item.tags.length ? <Space wrap size={[4, 8]}>{item.tags.map((tag) => <Tag key={tag} style={{ maxWidth: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{tag}</Tag>)}</Space> : '无'}</div>
      <Typography.Text type="secondary">{item.action === 'skipped' ? '跳过内容，保留原有整理标签。' : item.action === 'renamed' ? '重命名新建时写入这些标签。' : '新建时写入这些标签。'}</Typography.Text>
    </Space>
  </List.Item>} />
}

function PreviewSummary({ preview, title, tags, showAiRevision }: { preview: CardPreview; title: string; tags: CardTagSummary[]; showAiRevision: boolean }) {
  return <Card size="small" title={title} styles={{ title: { whiteSpace: 'normal' } }}>
    <Typography.Paragraph>角色 {preview.characters.length} 个、组织 {preview.organizations.length} 个、职业 {preview.careers.length} 个；{preview.can_import ? '映射已通过校验' : '仍有未解决的映射'}。</Typography.Paragraph>
    {showAiRevision && <Typography.Paragraph type="secondary">AI 可见性版本：{preview.ai_visibility_revision}</Typography.Paragraph>}
    {preview.field_definitions.map((item) => <div key={item.ref}>{item.label}：{item.status === 'new' ? '新建' : item.status === 'rename' ? `新建为「${item.rename_to}」` : item.status === 'reuse' ? `复用「${item.compatible_targets.find((target) => target.id === item.target_definition_id)?.label ?? '原选择'}」` : item.reason ?? '需要明确选择'}</div>)}
    {preview.references.map((item) => <div key={item.ref}>{item.name}：{item.status === 'internal' ? '随卡片新建' : item.status === 'mapped' ? '已映射到明确目标' : item.reason ?? '尚未解决'}</div>)}
    <TagSnapshot items={tags} />
  </Card>
}

function Inner({ projectId, onClose, onImported, actorId, identity }: Props & { actorId: number; identity: string }) {
  const { token } = theme.useToken()
  const { canEditNow } = useProjectAccess(projectId)
  const { state, controller } = useCardImport(projectId, actorId, identity, onImported)
  const busy = state.loading || state.importing
  const preview = state.reviewPreview ?? state.preview
  const version = state.metadata.version ?? 0
  const typed = version >= 4
  const tags = preview ? cardTagImportSummary(state.metadata, state.choices, preview) : []
  return <Modal open title="导入卡片" width={820} onCancel={onClose} maskClosable={false} footer={null}
    closable={!state.importing} keyboard={!state.importing} destroyOnHidden>
    <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
      {!state.result && <Upload.Dragger accept=".json,application/json" showUploadList={false} disabled={busy}
        beforeUpload={(file) => { if (canEditNow()) void controller.selectFile(file); return false }}>
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p style={{ overflowWrap: 'anywhere' }}>{state.fileName ? `已选择：${state.fileName}（点击重新选择）` : '点击或拖拽卡片 JSON 文件到这里'}</p>
        <p style={{ color: token.colorTextTertiary }}>支持 ainovel.cards v1–v6，上限 2MB。</p>
      </Upload.Dragger>}
      {state.loading && !state.preview && <Typography.Text role="status">正在读取并校验卡片，请稍候……</Typography.Text>}
      {state.error && <Alert type="error" showIcon message={state.error} description={state.preview ? '文件与当前选择仍保留，未自动重提导入。' : undefined} />}
      {state.reviewRequired && <Alert type="warning" showIcon message="请重新预览并比较后确认" description="原预览和版本仍保留。查看最新预览后，需要你明确采用；系统不会自动覆盖更新。" />}
      {state.needsPreview && !state.reviewRequired && <Alert type="info" showIcon message="处理方式或映射已改变，请更新预览后再确认。" />}
      {state.reviewPreview && state.preview && <>
        <PreviewSummary title="原预览" preview={state.preview} tags={cardTagImportSummary(state.metadata, state.choices, state.preview)} showAiRevision={version >= 6} />
        <PreviewSummary title="最新预览" preview={state.reviewPreview} tags={cardTagImportSummary(state.metadata, state.choices, state.reviewPreview)} showAiRevision={version >= 6} />
        <Button disabled={busy} onClick={() => { if (canEditNow()) controller.acceptReview() }}>已核对，采用最新预览</Button>
      </>}
      {preview && !state.result && <>
        {preview.warnings.map((warning, index) => <Alert key={index} type="warning" showIcon message={warning} />)}
        {typed && <Alert type="info" showIcon message="导入当前快照，不复制章节历史"
          description="跳过的条目保留已有字段值与历史。包内引用按本次新建身份连接；包外引用须明确映射。世界观没有卡片容器，目标档案不会随引用自动复制。" />}
        {version >= 6 && <Alert type="info" showIcon message="卡片带有 AI 与提及设置"
          description="只有新建和重命名新建的条目会写入卡片中的 AI 设置、字段隐藏和单向关联。跳过条目、复用职业均保留本作品原有设置。" />}
        {version >= 5 && <Card size="small" title="整理标签快照"><Typography.Paragraph type="secondary">仅新建或重命名新建的条目写入卡片标签；跳过内容、复用职业均保留已有标签。自动分类规则不会随卡片导入。</Typography.Paragraph><TagSnapshot items={tags} /></Card>}
        <CardImportEntities preview={preview} choices={state.choices} v4={typed} disabled={busy} onChange={controller.setResolution} />
        {typed && <>
          <CardDefinitionMappings definitions={preview.field_definitions} mappings={state.mappings.definitions} selected={state.selectedDefinitions} disabled={busy} onChange={controller.setDefinition} />
          <CardReferenceMappings projectId={projectId} preview={preview} metadata={state.metadata} choices={state.choices} mappings={state.mappings.references} selected={state.selectedEntries} disabled={busy} onChange={controller.setReference} />
        </>}
        <Space wrap>
          <Button icon={<ReloadOutlined />} loading={state.loading} disabled={state.importing} onClick={() => { if (canEditNow()) void controller.refreshPreview() }}>更新预览</Button>
          <Button type="primary" loading={state.importing} disabled={busy || state.needsPreview || state.reviewRequired || !state.preview?.can_import}
            onClick={() => { if (canEditNow()) void controller.confirm() }}>确认导入</Button>
          <Button disabled={state.importing} onClick={onClose}>取消</Button>
        </Space>
      </>}
      {state.result && <><Alert type="success" showIcon message="卡片导入完成" /><CardImportResultView result={state.result} /><Button type="primary" onClick={onClose}>完成</Button></>}
    </Space>
  </Modal>
}
