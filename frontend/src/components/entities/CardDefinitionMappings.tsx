import { Alert, Collapse, Form, Input, Select, Space, Tag, Typography } from 'antd'
import { useId } from 'react'
import { SETTING_FIELD_TYPE_LABELS } from '../../api/settingFields'
import type { CardDefinitionMapping, CardPreviewFieldDefinition, SettingFieldDefinition } from '../../api/types'

interface Props {
  definitions: CardPreviewFieldDefinition[]
  mappings: Record<string, CardDefinitionMapping>
  selected: Record<string, SettingFieldDefinition>
  disabled: boolean
  onChange: (ref: string, mapping: CardDefinitionMapping | null, target?: SettingFieldDefinition) => void
}
const STATUS = { new: '新建', conflict: '待选择', reuse: '复用', rename: '改名新建', incompatible: '需要修正' }

function DefinitionChoice({ definition: item, mapping, selected, disabled, onChange }: {
  definition: CardPreviewFieldDefinition; mapping: CardDefinitionMapping | undefined; selected: SettingFieldDefinition | undefined
  disabled: boolean; onChange: (mapping: CardDefinitionMapping | null, target?: SettingFieldDefinition) => void
}) {
  const id = useId()
  const target = item.compatible_targets.find((entry) => entry.id === mapping?.target_definition_id) ?? selected
  const targets = item.compatible_targets.map((entry) => ({ value: entry.id, label: `${entry.label}${entry.archived ? '（已停用）' : ''}` }))
  if (target && !targets.some((entry) => entry.value === target.id)) targets.push({ value: target.id, label: `${target.label}（需重新核对）` })
  return <Form layout="vertical" disabled={disabled}>
    {item.reason && <Alert type="warning" message={item.reason} showIcon style={{ marginBottom: 12 }} />}
    <Form.Item label="字段处理方式" htmlFor={`${id}-action`}>
      <Select id={`${id}-action`} aria-label={`「${item.label}」字段处理方式`} value={mapping?.action ?? (item.status === 'new' ? 'new' : undefined)} placeholder="选择复用或改名新建"
        options={[{ value: 'new', label: '按原名新建（名称须未被使用）' }, { value: 'reuse', label: '复用本作品的兼容字段' }, { value: 'rename', label: '改名新建字段' }]}
        onChange={(value) => onChange(value === 'new' ? null : { action: value === 'reuse' ? 'reuse' : 'rename', option_mappings: {} })} />
    </Form.Item>
    {mapping?.action === 'rename' && <Form.Item label="新字段名称" htmlFor={`${id}-label`} extra={item.rename_to ? `本次预览名称：${item.rename_to}` : '留空时自动在名称后添加「（导入）」。选项会随新字段复制。'}>
      <Input id={`${id}-label`} aria-label={`「${item.label}」新字段名称`} value={mapping.label ?? ''} placeholder={`${item.label}（导入）`} onChange={(event) => onChange({ ...mapping, label: event.target.value })} />
    </Form.Item>}
    {mapping?.action === 'reuse' && <>
      <Form.Item label="复用的字段" htmlFor={`${id}-target`} extra="只列出类型、适用范围与 AI 可见性相容的字段；已有条目的设置和值不会被覆写。">
        <Select<number> id={`${id}-target`} aria-label={`「${item.label}」复用的字段`} allowClear value={mapping.target_definition_id ?? undefined} options={targets} placeholder="明确选择一个现有字段"
          onChange={(id) => { const next = item.compatible_targets.find((entry) => entry.id === id); onChange({ action: 'reuse', target_definition_id: id, option_mappings: {} }, next) }} />
      </Form.Item>
      {!!item.options.length && <Typography.Paragraph type="secondary">为每个卡片选项选择对应的本作品选项。可以明确将多个选项对应到同一选项。</Typography.Paragraph>}
      {item.options.map((option, index) => {
        const selectedId = mapping.option_mappings[option.ref]
        const options = (target?.options ?? []).map((entry) => ({ value: entry.id, label: `${entry.label}${entry.archived ? '（已停用）' : ''}` }))
        if (selectedId && !options.some((entry) => entry.value === selectedId)) options.push({ value: selectedId, label: '原选项（请重新选择核对）' })
        return <Form.Item key={option.ref} htmlFor={`${id}-option-${index}`} label={`卡片选项：${option.label}${option.archived ? '（已停用）' : ''}`}>
          <Select<number> id={`${id}-option-${index}`} aria-label={`「${item.label}」的卡片选项「${option.label}」对应的本作品选项`} disabled={disabled || !mapping.target_definition_id} allowClear value={selectedId} options={options} placeholder="选择本作品的对应选项"
            onChange={(id) => { const option_mappings = { ...mapping.option_mappings }; if (id === undefined) delete option_mappings[option.ref]; else option_mappings[option.ref] = id; onChange({ ...mapping, option_mappings }) }} />
        </Form.Item>
      })}
    </>}
  </Form>
}

/** 定义和选项均显式对应，不根据同名内容替作者决定。 */
export default function CardDefinitionMappings({ definitions, mappings, selected, disabled, onChange }: Props) {
  if (!definitions.length) return null
  return <section aria-label="卡片字段定义映射">
    <Typography.Title level={5}>字段定义（{definitions.length}）</Typography.Title>
    <Collapse accordion destroyOnHidden items={definitions.map((definition) => ({ key: definition.ref,
      label: <Space wrap><span style={{ overflowWrap: 'anywhere' }}>{definition.label}</span><Tag>{SETTING_FIELD_TYPE_LABELS[definition.field_type]}</Tag>{definition.ai_hidden && <Tag>AI 隐藏</Tag>}<Tag>{STATUS[definition.status]}</Tag></Space>,
      children: <DefinitionChoice definition={definition} mapping={mappings[definition.ref]} selected={selected[definition.ref]} disabled={disabled} onChange={(mapping, target) => onChange(definition.ref, mapping, target)} />,
    }))} />
  </section>
}
