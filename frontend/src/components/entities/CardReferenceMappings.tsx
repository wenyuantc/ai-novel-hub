import { Alert, Button, Collapse, Space, Tag, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { SETTING_ENTRY_TYPE_LABELS, settingFieldsApi } from '../../api/settingFields'
import type { CardPreview, CardPreviewReference, CardResolutions, SettingEntry, SettingEntryRef, SettingEntryType } from '../../api/types'
import EntrySelect from './EntrySelect'
import type { EntrySelectQuery } from './entrySelectState'
import { isInternalReference, referenceTypes, type CardMetadata } from './cardImportMapping'

interface Props {
  projectId: number
  preview: CardPreview
  metadata: CardMetadata
  choices: CardResolutions
  mappings: Record<string, SettingEntryRef>
  selected: Record<string, SettingEntry>
  disabled: boolean
  onChange: (ref: string, entry: SettingEntry | null) => void
}
const KIND = { character: '角色', world: '世界观', organization: '组织', career: '职业' }
const keyOf = (entry: SettingEntryRef) => `${entry.kind}:${entry.id}`
function typesFromKey(key: string): SettingEntryType[] { return Object.keys(SETTING_ENTRY_TYPE_LABELS).filter((value): value is SettingEntryType => key.split(',').includes(value) && Object.hasOwn(SETTING_ENTRY_TYPE_LABELS, value)) }

function ReferenceChoice({ projectId, item, typeKey, value, selected, disabled, onChange }: {
  projectId: number; item: CardPreviewReference; typeKey: string; value: SettingEntryRef | undefined; selected: SettingEntry | undefined
  disabled: boolean; onChange: (entry: SettingEntry | null) => void
}) {
  const cache = useRef(new Map<string, SettingEntry>())
  const load = useCallback(async (query: EntrySelectQuery, signal: AbortSignal) => {
    const entries = await settingFieldsApi.listEntries(projectId, { ...query, types: typesFromKey(typeKey) }, signal)
    for (const entry of entries.items) cache.current.set(keyOf(entry.reference), entry)
    return { total: entries.total, items: entries.items.map((entry) => ({ value: keyOf(entry.reference), label: `${entry.name} · ${SETTING_ENTRY_TYPE_LABELS[entry.type]}` })) }
  }, [projectId, typeKey])
  if (item.status === 'internal') return <Alert type="success" showIcon message="将引用卡片中本次新建的条目" description="包内身份会随新条目建立，无需选择已有目标。" />
  if (!typeKey) return <Alert type="warning" showIcon message="这个引用没有兼容的目标类型，请检查卡片字段与 AI 关联的目标范围。" />
  return <Space direction="vertical" style={{ width: '100%' }}>
    {item.reason && <Alert type="warning" showIcon message={item.reason} />}
    <Typography.Text>本作品中的具体{KIND[item.kind]}</Typography.Text>
    <EntrySelect<string> projectId={projectId} queryKey={`card-reference:${item.ref}:${typeKey}`} label={`选择「${item.name}」对应的${KIND[item.kind]}`}
      single disabled={disabled} loadEntries={load} value={value ? [keyOf(value)] : []}
      selectedOptions={value ? [{ value: keyOf(value), label: selected?.name ?? '原选择（请重新核对）' }] : []}
      onChange={(keys) => { if (!keys.length) onChange(null); else { const entry = cache.current.get(keys[0]); if (entry) onChange(entry) } }} />
    {selected && <Typography.Text type="secondary">已选：{selected.name} · {SETTING_ENTRY_TYPE_LABELS[selected.type]}</Typography.Text>}
  </Space>
}

/** 每次展开才加载一个目标选择器，避免为全部引用同时请求目录。 */
export default function CardReferenceMappings({ projectId, preview, metadata, choices, mappings, selected, disabled, onChange }: Props) {
  const [active, setActive] = useState<string | undefined>()
  if (!preview.references.length) return null
  const locate = (item: CardPreviewReference, index: number) => {
    setActive(item.ref)
    const target = document.getElementById(`card-reference-${index}`)
    target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    target?.focus({ preventScroll: true })
  }
  return <section aria-label="卡片引用目标映射">
    <Typography.Title level={5}>引用目标（{preview.references.length}）</Typography.Title>
    <Typography.Paragraph type="secondary">字段引用和 AI 关联的包外目标、跳过的同名条目需要明确选择本作品中的具体对象。世界观条目通过这里映射，不复制目标档案。</Typography.Paragraph>
    {preview.references.some((item) => item.status === 'unresolved') && <Alert type="warning" showIcon message="以下引用尚未解决，暂不能导入"
      description={<Space wrap>{preview.references.map((item, index) => item.status === 'unresolved' ? <Button key={item.ref} size="small" onClick={() => locate(item, index)}>定位：{item.name}</Button> : null)}</Space>}
      style={{ marginBottom: 12 }} />}
    <Collapse accordion destroyOnHidden activeKey={active} onChange={(keys) => setActive(Array.isArray(keys) ? keys[0] : keys)} items={preview.references.map((item, index) => ({ key: item.ref,
      label: <Space wrap><span id={`card-reference-${index}`} tabIndex={-1} style={{ overflowWrap: 'anywhere' }}>{item.name} · {KIND[item.kind]}</span>
        {!!item.source_links.length && <Tag>AI 关联</Tag>}
        <Tag>{metadata.entries.some((entry) => entry.ref === item.ref) ? '卡片内条目' : '卡片外目标'}</Tag><Tag>{item.status === 'internal' ? '随卡片新建' : item.status === 'mapped' ? '已明确选择' : '待选择'}</Tag></Space>,
      children: <Space direction="vertical" style={{ width: '100%' }}>
        {!!item.source_fields.length && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>字段引用：{item.source_fields.join('、')}</Typography.Text>}
        {!!item.source_links.length && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>AI 单向关联：{item.source_links.join('、')} → {item.name}</Typography.Text>}
        <ReferenceChoice projectId={projectId} item={isInternalReference(item.ref, metadata, choices, preview) ? { ...item, status: 'internal' } : item} typeKey={referenceTypes(item.ref, item.kind, metadata, choices, preview).join(',')}
          value={mappings[item.ref]} selected={selected[item.ref]} disabled={disabled} onChange={(entry) => onChange(item.ref, entry)} />
      </Space>,
    }))} />
  </section>
}
