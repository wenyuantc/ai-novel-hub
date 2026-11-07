import { Alert, Button, Checkbox, Descriptions, Form, Input, Modal, Select, Space, Switch, Typography } from 'antd'
import { useState } from 'react'
import { cleanAliases } from '../../api/characters'
import { ApiError, errorMessage } from '../../api/client'
import { seriesApi, type SeriesAiSettings, type SeriesCharacterData, type SeriesEntry, type SeriesEntryInput, type SeriesKind, type SeriesMutation, type SeriesWorldData } from '../../api/series'
import { WORLD_CATEGORIES, WORLD_CATEGORY_LABELS } from '../../utils/world'
import { normalizeExclusions } from '../settingAi/settingAiDraft'
import { SETTING_AI_MODES, SETTING_AI_MODE_OPTIONS, settingAiFieldLabel } from '../settingAi/settingAiOptions'
import { diffSeriesValues, rebaseSeriesAi, SERIES_AI_DEFAULTS, seriesEntryName, seriesNeedsReview } from './seriesState'
import { useSeriesAction } from './useSeriesAction'

type Draft = {
  data: Partial<SeriesCharacterData & SeriesWorldData>
  ai_settings: SeriesAiSettings
}

const CATEGORIES = WORLD_CATEGORIES.map((value) => ({ value, label: WORLD_CATEGORY_LABELS[value] }))
const TEXT_FIELDS = [['profile', '简介'], ['appearance', '外貌'], ['personality', '性格'], ['background', '背景']] as const
const HIDEABLE = {
  character: ['role_type', 'age', 'gender', 'profile', 'appearance', 'personality', 'background', 'current_state', 'life_status', 'relations_text'],
  world: ['content'],
} as const

function emptyEntry(kind: SeriesKind): Draft {
  return {
    data: kind === 'character'
      ? { name: '', aliases: [], gender: '', profile: '', appearance: '', personality: '', background: '' }
      : { title: '', aliases: [], category: 'other', content: '' },
    ai_settings: SERIES_AI_DEFAULTS,
  }
}

function requestBody(kind: SeriesKind, draft: Draft): SeriesEntryInput {
  const data = draft.data
  const aliases = cleanAliases(data.aliases ?? []).aliases
  const ai_settings = { ...draft.ai_settings, mention_exclusions: normalizeExclusions(draft.ai_settings.mention_exclusions) }
  return kind === 'character'
    ? { kind, data: { name: data.name?.trim() ?? '', aliases, gender: data.gender ?? '', profile: data.profile ?? '',
      appearance: data.appearance ?? '', personality: data.personality ?? '', background: data.background ?? '' }, ai_settings }
    : { kind, data: { title: data.title?.trim() ?? '', aliases, category: data.category ?? 'other', content: data.content ?? '' }, ai_settings }
}

interface Props {
  seriesId: number
  kind: SeriesKind
  entry: SeriesEntry | null
  revision: number
  onClose: () => void
  onDone: (result: SeriesMutation, previous: SeriesEntry | null) => void
}

export default function SeriesEntryEditor({ seriesId, kind, entry, revision, onClose, onDone }: Props) {
  const [form] = Form.useForm<Draft>()
  const [base, setBase] = useState(() => ({ revision, draft: entry ? { data: entry.data, ai_settings: entry.ai_settings } : emptyEntry(kind) }))
  const [review, setReview] = useState(false)
  const [latest, setLatest] = useState<{ revision: number; item: SeriesEntry | null } | null>(null)
  const action = useSeriesAction()
  const waitingForReview = review || (action.error !== null && seriesNeedsReview(action.error))
  const uncertainCreate = !entry && action.error !== null && seriesNeedsReview(action.error)
    && !(action.error instanceof ApiError && action.error.status === 409)
  const aiMode = Form.useWatch(['ai_settings', 'ai_mode'], form) ?? base.draft.ai_settings.ai_mode

  const save = () => {
    if (waitingForReview) return
    void action.run(async (current) => {
      const value = await form.validateFields().catch(() => null)
      if (!value || !current()) return null
      const input = requestBody(kind, value)
      if (!entry) return seriesApi.createEntry(seriesId, base.revision, input)
      return seriesApi.updateEntry(seriesId, entry.id, base.revision, {
        data: diffSeriesValues(base.draft.data, input.data),
        ai_settings: diffSeriesValues(base.draft.ai_settings, input.ai_settings),
      })
    }, (result) => { if (result) onDone(result, entry) })
  }

  const readLatest = () => {
    setReview(true)
    void action.run(async () => {
      const before = await seriesApi.get(seriesId)
      if (!entry) return { revision: before.revision, item: null }
      const item = await seriesApi.entry(seriesId, entry.id)
      const after = await seriesApi.get(seriesId)
      if (before.revision !== after.revision) throw new Error('共享库仍在变化，请再次读取最新版本。')
      return { revision: after.revision, item }
    }, setLatest)
  }

  const adoptLatest = () => {
    if (!latest) return
    if (latest.item) {
      const draft = form.getFieldsValue(true)
      const snapshot = { data: latest.item.data, ai_settings: latest.item.ai_settings }
      form.setFieldsValue({
        data: { ...snapshot.data, ...diffSeriesValues(base.draft.data, draft.data) },
        ai_settings: rebaseSeriesAi(base.draft.ai_settings, draft.ai_settings, snapshot.ai_settings),
      })
      setBase({ revision: latest.revision, draft: snapshot })
    } else setBase((old) => ({ ...old, revision: latest.revision }))
    setLatest(null)
    setReview(false)
    action.clearError()
  }

  return <Modal open title={entry ? `编辑共享基础 · ${seriesEntryName(entry)}` : kind === 'character' ? '新增共享角色' : '新增共享世界观'}
    onOk={save} onCancel={onClose} okText="保存共享基础" cancelText="取消" confirmLoading={action.busy}
    okButtonProps={{ disabled: waitingForReview }} maskClosable={false} closable={!action.busy}
    keyboard={!action.busy} cancelButtonProps={{ disabled: action.busy }} width={650}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert type="info" showIcon message="修改共享基础会同步到采用此条目的作品；各书的剧情变化仍各自保留。" />
      {!!action.error && <Alert type="error" showIcon message={errorMessage(action.error)} description="草稿已保留。" />}
      {waitingForReview && <Alert type="warning" showIcon message="共享库版本已更新或写入结果未确认"
        description={uncertainCreate ? '请先回到共享库核对该条目是否已创建，不要直接重复提交。' : '请读取最新设置并核对，再决定是否继续保存；不会自动重发。'}
        action={!uncertainCreate && <Button size="small" onClick={readLatest} loading={action.busy}>读取最新版本</Button>} />}
      {latest && <div>
        {latest.item && <Descriptions title="当前保存的共享档案" size="small" column={1} bordered>
          <Descriptions.Item label="名称">{seriesEntryName(latest.item)}</Descriptions.Item>
          <Descriptions.Item label="别名">{latest.item.data.aliases.join('、') || '无'}</Descriptions.Item>
          {latest.item.kind === 'character' ? Object.entries(latest.item.data).filter(([key]) => key !== 'name' && key !== 'aliases').map(([key, value]) =>
            <Descriptions.Item key={key} label={settingAiFieldLabel(key)}><Typography.Paragraph ellipsis={{ rows: 3, expandable: true }}>{value || '未填写'}</Typography.Paragraph></Descriptions.Item>)
            : <>
              <Descriptions.Item label="分类">{WORLD_CATEGORY_LABELS[latest.item.data.category]}</Descriptions.Item>
              <Descriptions.Item label="基础说明"><Typography.Paragraph ellipsis={{ rows: 3, expandable: true }}>{latest.item.data.content || '未填写'}</Typography.Paragraph></Descriptions.Item>
            </>}
          <Descriptions.Item label="AI 策略">{SETTING_AI_MODES[latest.item.ai_settings.ai_mode].label}</Descriptions.Item>
          <Descriptions.Item label="提及追踪">{latest.item.ai_settings.mention_enabled ? '开启' : '关闭'} · {latest.item.ai_settings.mention_case_sensitive ? '区分大小写' : '忽略大小写'}</Descriptions.Item>
          <Descriptions.Item label="排除短语">{latest.item.ai_settings.mention_exclusions.join('、') || '无'}</Descriptions.Item>
          <Descriptions.Item label="隐藏字段">{latest.item.ai_settings.ai_hidden_builtin_fields.map(settingAiFieldLabel).join('、') || '无'}</Descriptions.Item>
        </Descriptions>}
        <Button onClick={adoptLatest} disabled={action.busy} style={{ marginTop: 8 }}>保留我的修改并采用最新版本</Button>
      </div>}
      <Form form={form} layout="vertical" initialValues={base.draft} disabled={action.busy}>
        <Form.Item name={['data', kind === 'character' ? 'name' : 'title']} label={kind === 'character' ? '姓名' : '标题'}
          rules={[{ required: true, whitespace: true, max: kind === 'character' ? 100 : 200 }]}>
          <Input />
        </Form.Item>
        <Form.Item name={['data', 'aliases']} label="别名" extra="按回车添加；最多 10 项，每项 50 字。"
          rules={[{ validator: (_, value: string[] | undefined) => {
            const result = cleanAliases(value ?? [])
            return result.error ? Promise.reject(new Error(result.error)) : Promise.resolve()
          } }]}>
          <Select mode="tags" open={false} tokenSeparators={[',', '，']} />
        </Form.Item>
        {kind === 'character' ? <>
          <Form.Item name={['data', 'gender']} label="性别" rules={[{ max: 20 }]}><Input /></Form.Item>
          {TEXT_FIELDS.map(([name, label]) => <Form.Item key={name} name={['data', name]} label={label}>
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} />
          </Form.Item>)}
        </> : <>
          <Form.Item name={['data', 'category']} label="世界观分类"><Select options={CATEGORIES} /></Form.Item>
          <Form.Item name={['data', 'content']} label="基础说明"><Input.TextArea autoSize={{ minRows: 4, maxRows: 12 }} /></Form.Item>
        </>}
        <Typography.Title level={5}>AI 与提及</Typography.Title>
        <Form.Item name={['ai_settings', 'ai_mode']} label="条目策略" extra={SETTING_AI_MODES[aiMode].description}>
          <Select options={SETTING_AI_MODE_OPTIONS} />
        </Form.Item>
        <Space size="large" wrap>
          <Form.Item name={['ai_settings', 'mention_enabled']} label="提及追踪" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name={['ai_settings', 'mention_case_sensitive']} label="区分大小写" valuePropName="checked"><Switch /></Form.Item>
        </Space>
        <Form.Item name={['ai_settings', 'mention_exclusions']} label="排除短语" extra="按回车添加；最多 20 项，每项 80 字。"
          rules={[{ validator: (_, value: string[] | undefined) => {
            const items = normalizeExclusions(value ?? [])
            return items.length > 20 || items.some((item) => !item || Array.from(item).length > 80)
              ? Promise.reject(new Error('排除短语最多 20 项，每项 1–80 字。')) : Promise.resolve()
          } }]}>
          <Select mode="tags" open={false} />
        </Form.Item>
        <Form.Item name={['ai_settings', 'ai_hidden_builtin_fields']} label="对 AI 隐藏的字段">
          <Checkbox.Group options={HIDEABLE[kind].map((value) => ({ value, label: settingAiFieldLabel(value) }))} />
        </Form.Item>
      </Form>
    </Space>
  </Modal>
}
