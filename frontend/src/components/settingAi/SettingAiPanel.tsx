import { Alert, Button, Checkbox, Descriptions, Divider, Select, Space, Spin, Switch, Tag, Typography } from 'antd'
import { useCallback, useEffect, useId, useMemo, useRef, useSyncExternalStore } from 'react'
import type { SettingAiMode } from '../../api/settingAiVisibility'
import { SETTING_ENTRY_TYPE_LABELS, settingFieldsApi } from '../../api/settingFields'
import type { SettingEntry, SettingEntryRef } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import EntrySelect from '../entities/EntrySelect'
import type { EntrySelectQuery } from '../entities/entrySelectState'
import type { SettingAiSession } from './settingAiController'
import { normalizeExclusions, settingAiDirty, settingAiDraftError, settingLinkKey, type SettingAiDraft } from './settingAiDraft'

const MODES: Record<SettingAiMode, { label: string; description: string }> = {
  always: { label: '始终加入', description: '有设定上下文的 AI 操作可使用此条目，不要求文本中提到它。' },
  detected: { label: '检测到才加入', description: '名称或别名被明确检测到时可加入，也可通过手动指定或合格的关联加入。' },
  manual_only: { label: '检测到不自动加入', description: '字面命中不会自动加入；仍允许手动指定或合格的关联加入。' },
  never: { label: '永不加入', description: '此条目不提供给 AI，手动指定和关联也不能绕过。人工查看与提及统计不受影响。' },
}
const MODE_OPTIONS = (Object.entries(MODES) as [SettingAiMode, typeof MODES[SettingAiMode]][]).map(([value, { label }]) => ({ value, label }))
const FIELD_LABELS: Record<string, string> = {
  role_type: '角色定位', age: '年龄', gender: '性别', profile: '简介', appearance: '外貌', personality: '性格',
  background: '背景', current_state: '当前处境', life_status: '生死状态', relations_text: '旧关系备注',
  content: '档案正文', org_type: '组织类型', purpose: '宗旨', location: '所在地', power_level: '势力值', description: '描述',
  requirements: '获得条件', abilities: '特殊能力', rules: '体系规则', stages: '职业阶段资料',
}
const fieldLabel = (key: string) => FIELD_LABELS[key] ?? key
const linkLabel = (target: SettingEntry) => `${target.name} · ${SETTING_ENTRY_TYPE_LABELS[target.type]}`
const modeLabel = (mode: SettingAiMode | null) => mode === null ? '沿用旧规则' : MODES[mode].label

function Summary({ title, value }: { title: string; value: SettingAiDraft }) {
  return <Descriptions title={title} size="small" column={1}>
    <Descriptions.Item label="条目策略">{modeLabel(value.ai_mode)}</Descriptions.Item>
    <Descriptions.Item label="提及追踪">{value.mention_enabled ? '开启' : '关闭'}</Descriptions.Item>
    <Descriptions.Item label="大小写">{value.mention_case_sensitive ? '区分' : '忽略'}</Descriptions.Item>
    <Descriptions.Item label="排除短语">{value.mention_exclusions.join('、') || '无'}</Descriptions.Item>
    <Descriptions.Item label="隐藏字段">{value.ai_hidden_builtin_fields.map(fieldLabel).join('、') || '无'}</Descriptions.Item>
    <Descriptions.Item label="带入目标">{value.links.map(linkLabel).join('、') || '无'}</Descriptions.Item>
  </Descriptions>
}

interface Props { session: SettingAiSession; entry: SettingEntryRef }

/** 独立于档案基础表单保存 AI 策略和提及规则。 */
export default function SettingAiPanel({ session, entry }: Props) {
  const { kind, id } = entry
  const controller = useMemo(() => session.forEntry({ kind, id }), [session, kind, id])
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const { canRead, canEdit } = useProjectAccess(session.projectId)
  const ids = useId()
  const { base, draft, latest, reviewRequired, pending, error, notice } = state
  useEffect(() => {
    void controller.open()
    return controller.close
  }, [controller])
  if (!canRead) return null
  if (!base || !draft) return error
    ? <Alert showIcon type="error" message={error} action={<Button size="small" onClick={() => void controller.refresh()}>重试</Button>} />
    : <Spin aria-label="正在读取 AI 与提及设置" />

  const disabled = !canEdit || !!pending
  const invalid = settingAiDraftError(draft)
  const dirty = settingAiDirty(base, draft)
  return <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text type="secondary">本区独立保存，不修改档案原文或剧情变化。收紧限制后，旧会话可能需新开，已发出的请求不能撤回。</Typography.Text>
    {!canEdit && <Alert type="info" showIcon message={dirty ? '当前为只读，未保存的设置草稿仍保留。' : '当前为只读，可查看 AI 与提及设置。'} />}
    {notice && <Alert type={reviewRequired ? 'warning' : 'info'} showIcon message={notice} />}
    {error && <Alert type="error" showIcon message={error} description="当前草稿已保留。" />}
    {reviewRequired && <Alert type="warning" showIcon message="请核对最新设置后再保存"
      description="不会自动替换保存版本或重发。读取后可以保留草稿中修改过的项，其他项采用最新设置。"
      action={!latest && <Button size="small" loading={pending === 'read'} onClick={() => void controller.refresh()}>读取最新设置</Button>} />}
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <label htmlFor={`${ids}-mode`}>条目策略</label>
      <Select<SettingAiMode> id={`${ids}-mode`} aria-label="条目策略" style={{ width: '100%' }} value={draft.ai_mode ?? undefined}
        disabled={disabled} placeholder="沿用旧规则" options={MODE_OPTIONS} onChange={(ai_mode) => controller.edit({ ai_mode })} />
      <Typography.Text type="secondary">{draft.ai_mode === null
        ? '此条目沿用原有的注入行为。首次保存明确的策略后，将采用所选规则。'
        : MODES[draft.ai_mode].description}</Typography.Text>
    </Space>
    <Space wrap>
      <Switch aria-label="提及追踪" checked={draft.mention_enabled} disabled={disabled}
        onChange={(mention_enabled) => controller.edit({ mention_enabled })} />
      <Typography.Text>提及追踪</Typography.Text>
      <Switch aria-label="区分大小写" checked={draft.mention_case_sensitive} disabled={disabled || !draft.mention_enabled}
        onChange={(mention_case_sensitive) => controller.edit({ mention_case_sensitive })} />
      <Typography.Text>区分大小写</Typography.Text>
    </Space>
    {!draft.mention_enabled && <Typography.Text type="secondary">关闭后不高亮、不统计，也不参与 AI 字面检测；不阻止合格的手动或关联引用。</Typography.Text>}
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <label htmlFor={`${ids}-exclude`}>排除短语</label>
      <Select<string[]> id={`${ids}-exclude`} aria-label="排除短语" mode="tags" open={false} style={{ width: '100%' }} value={draft.mention_exclusions}
        disabled={disabled || !draft.mention_enabled} placeholder="输入短语后按 Enter 添加" onChange={(items) => controller.edit({ mention_exclusions: normalizeExclusions(items) })} />
      <Typography.Text type="secondary">只忽略完整落在排除短语里的本条目提及。按字面匹配，不使用正则；最多 20 项，每项 80 字。</Typography.Text>
    </Space>
    <Space direction="vertical" size={4} role="group" aria-label="对 AI 隐藏的内建字段">
      <Typography.Text strong>对 AI 隐藏的内建字段</Typography.Text>
      <Typography.Text type="secondary">勾选后，字段及其剧情历史仍可在人工档案中查看和维护。</Typography.Text>
      <Space wrap>
        {base.hideable_builtin_fields.map((key) => <Checkbox key={key} checked={draft.ai_hidden_builtin_fields.includes(key)} disabled={disabled}
          onChange={(event) => controller.edit({ ai_hidden_builtin_fields: event.target.checked
            ? [...draft.ai_hidden_builtin_fields, key] : draft.ai_hidden_builtin_fields.filter((item) => item !== key) })}>{fieldLabel(key)}</Checkbox>)}
      </Space>
    </Space>
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <label htmlFor={`${ids}-links`}>随本条目带入的关联</label>
      <Typography.Text type="secondary">本条目直接入选 → 所选目标也有资格入选。只向这一方向展开一层，不能带入「永不加入」的目标。</Typography.Text>
      {canEdit ? <LinkPicker key={settingLinkKey(entry)} projectId={session.projectId} entry={entry} id={`${ids}-links`} disabled={disabled}
        value={draft.links} onChange={(links) => controller.edit({ links })} /> :
        <Space wrap>{draft.links.length ? draft.links.map((target) => <Tag key={settingLinkKey(target.reference)}>{linkLabel(target)}</Tag>) : <Typography.Text type="secondary">无关联</Typography.Text>}</Space>}
    </Space>
    {invalid && <Alert type="warning" showIcon message={invalid} />}
    {latest && <>
      <Divider style={{ margin: '4px 0' }} />
      <Summary title="开始编辑时的设置" value={base} />
      <Summary title="最新保存的设置" value={latest} />
      <Space wrap>
        <Button disabled={disabled} onClick={() => controller.acceptLatest(true)}>保留草稿并采用最新版本</Button>
        <Button disabled={disabled} onClick={() => controller.acceptLatest(false)}>放弃草稿，载入最新设置</Button>
      </Space>
    </>}
    <Space wrap>
      {canEdit && <Button type="primary" loading={pending === 'write'} disabled={disabled || !dirty || !!invalid || reviewRequired}
        onClick={() => void controller.save()}>保存 AI 与提及设置</Button>}
      {canEdit && dirty && !reviewRequired && <Button disabled={disabled} onClick={controller.reset}>放弃未保存改动</Button>}
      <Button loading={pending === 'read'} disabled={pending === 'write'} onClick={() => void controller.refresh()}>重新读取</Button>
    </Space>
  </Space>
}

function LinkPicker({ projectId, entry, id, disabled, value, onChange }: {
  projectId: number; entry: SettingEntryRef; id: string; disabled: boolean; value: SettingEntry[]; onChange: (items: SettingEntry[]) => void
}) {
  const scopeKey = settingLinkKey(entry)
  // 缓存只有最近一页名称；已选项本身保留名称，翻页不丢失。
  const candidates = useRef(new Map<string, SettingEntry>())
  const load = useCallback(async (query: EntrySelectQuery, signal: AbortSignal) => {
    const result = await settingFieldsApi.listEntries(projectId, query, signal)
    if (!signal.aborted) candidates.current = new Map(result.items.map((item) => [settingLinkKey(item.reference), item]))
    return { total: result.total, items: result.items.map((item) => {
      const value = settingLinkKey(item.reference)
      return { value, label: linkLabel(item), disabled: value === scopeKey }
    }) }
  }, [projectId, scopeKey])
  return <EntrySelect<string> projectId={projectId} queryKey={`setting-ai-link:${scopeKey}`} id={id} label="选择随本条目带入的设定" loadEntries={load}
    value={value.map((item) => settingLinkKey(item.reference))} disabled={disabled}
    selectedOptions={value.map((item) => ({ value: settingLinkKey(item.reference), label: linkLabel(item) }))}
    onChange={(keys) => {
      const selected = new Map(value.map((item) => [settingLinkKey(item.reference), item]))
      onChange(keys.filter((key) => key !== scopeKey).flatMap((key) => {
        const item = selected.get(key) ?? candidates.current.get(key)
        return item ? [item] : []
      }))
    }} />
}
