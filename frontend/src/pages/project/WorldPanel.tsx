import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
import {
  App,
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { errorMessage } from '../../api/client'
import { cleanAliases } from '../../api/characters'
import { baselineEditorLabel } from '../../api/progressions'
import { worldApi } from '../../api/story'
import type { WorldSetting } from '../../api/types'
import WizardModal from '../../components/WizardModal'
import SettingProgressionsPanel from '../../components/progressions/SettingProgressionsPanel'
import { useWorldBaselineEditor } from '../../components/progressions/useWorldBaselineEditor'
import type { WorldFormValues } from '../../components/progressions/worldForm'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useEntityDeepLink } from '../../hooks/useEntityDeepLink'
import EntityMentions from '../../components/mentions/EntityMentions'
import { asWorldCategory, groupWorld, WORLD_CATEGORIES, WORLD_CATEGORY_LABELS } from '../../utils/world'

const CATEGORY_OPTIONS = WORLD_CATEGORIES.map((c) => ({ value: c, label: WORLD_CATEGORY_LABELS[c] }))

export default function WorldPanel() {
  const pid = useProjectId()
  const { canUseAI, canEdit, canEditNow, canReadNow } = useProjectAccess(pid)
  const { message } = App.useApp()
  const loader = useCallback(() => worldApi.list(pid), [pid])
  const { data: loadedItems, loading, error: listError, reload } = useLoader(loader, { projectId: pid, resources: ['world'] })
  const items = useMemo(() => loadedItems ?? [], [loadedItems])
  const [aiOpen, setAiOpen] = useState(false)
  const [tab, setTab] = useState('base')
  const [form] = Form.useForm<WorldFormValues>()
  const category = Form.useWatch('category', form)
  const editor = useWorldBaselineEditor(pid, form)
  const { editing, open, saving, baseline, baselineLoading, baselineError, baselineConflict } = editor
  const [filter, setFilter] = useState<string>('all')
  const groups = useMemo(
    () => groupWorld(items).filter((g) => filter === 'all' || g.category === filter),
    [items, filter],
  )

  const openEdit = (item: WorldSetting | null) => {
    setTab('base')
    editor.openEditor(item, filter === 'all' ? 'other' : asWorldCategory(filter))
  }
  useEntityDeepLink(pid, loadedItems, loading, openEdit, canReadNow)

  const handleDelete = async (item: WorldSetting) => {
    if (!canEditNow()) return
    try {
      if (!canEditNow()) return
      await worldApi.remove(pid, item.id)
      if (!canEditNow()) return
      message.success('已删除')
      reload()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    }
  }

  return (
    <Card
      title="世界观设定"
      extra={
        <Space>
          {canUseAI && (<Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)}>
            AI 重新生成
          </Button>)}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)} disabled={!canEdit}>
            新增条目
          </Button>
        </Space>
      }
    >
      {listError && <Alert type="error" showIcon message={listError} action={<Button onClick={reload}>重试</Button>} style={{ marginBottom: 12 }} />}
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <Empty description={canUseAI ? '暂无世界观设定，可手动新增或使用 AI 生成' : '暂无世界观设定，可手动新增'} />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Segmented
              value={filter}
              onChange={(v) => setFilter(String(v))}
              options={[
                { value: 'all', label: `全部（${items.length}）` },
                ...groupWorld(items).map((g) => ({
                  value: g.category,
                  label: `${WORLD_CATEGORY_LABELS[g.category]}（${g.items.length}）`,
                })),
              ]}
            />
            {groups.map((g) => (
              <div key={g.category}>
                <Typography.Title level={5} style={{ marginTop: 8 }}>
                  {WORLD_CATEGORY_LABELS[g.category]}
                </Typography.Title>
                <List
                  dataSource={g.items}
                  renderItem={(w) => (
                    <List.Item
                      actions={[
                        <Button key="edit" type="link" size="small" onClick={() => openEdit(w)}>
                          {canEdit ? '编辑' : '查看'}</Button>,
                        <Popconfirm
                          key="del"
                          title="删除该条目？"
                          okText="删除"
                          cancelText="取消"
                          okButtonProps={{ danger: true, disabled: !canEdit }}
                          onConfirm={() => handleDelete(w)}
                        >
                          <Button type="link" size="small" danger disabled={!canEdit}>
                            删除
                          </Button>
                        </Popconfirm>,
                      ]}
                    >
                      <List.Item.Meta
                        title={
                          <Space size={4}>
                            <Tag>{WORLD_CATEGORY_LABELS[asWorldCategory(w.category)]}</Tag>
                            {w.title}
                          </Space>
                        }
                        description={
                          <Typography.Paragraph
                            style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                            ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
                          >
                            {w.content || '（无内容）'}
                          </Typography.Paragraph>
                        }
                      />
                    </List.Item>
                  )}
                />
              </div>
            ))}
          </Space>
        )}
      </Spin>

      <Modal
        okButtonProps={{ disabled: !canEdit || (!!editing && (!baseline || baselineLoading || !!baselineError || baselineConflict)) }}
        title={editing ? (canEdit ? '编辑世界观条目' : '查看世界观条目') : '新增世界观条目'}
        open={open}
        onOk={() => void editor.save()}
        onCancel={() => { if (!saving) editor.closeEditor() }}
        confirmLoading={saving}
        closable={!saving}
        keyboard={!saving}
        maskClosable={!saving}
        cancelButtonProps={{ disabled: saving }}
        footer={tab === 'progression' || tab === 'mentions' ? <Button onClick={editor.closeEditor}>关闭</Button> : undefined}
        okText="保存"
        cancelText="取消"
        width={720}
        forceRender
      >
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'base',
              label: '基础设定',
              forceRender: true,
              children: (
                <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                  {baselineError && (
                    <Alert
                      type="warning"
                      showIcon
                      message={baselineError}
                      description="基础表单中的草稿仍保留在当前窗口。"
                      action={baselineConflict ? (
                        <Popconfirm
                          title="重新读取基础设定和条目信息？"
                          description="未保存的基础内容、标题、分类和排序会被替换；请先复制需要保留的草稿。"
                          okText="重新读取"
                          cancelText="取消"
                          onConfirm={() => editor.reloadBaseline(true)}
                        >
                          <Button size="small">重新读取</Button>
                        </Popconfirm>
                      ) : <Button size="small" onClick={() => editor.reloadBaseline()}>刷新基础设定</Button>}
                    />
                  )}
                  {editing && baseline && !baselineError && (
                    <Alert
                      type={baseline.baseline_status === 'legacy_unverified' ? 'warning' : 'info'}
                      showIcon
                      message={baselineEditorLabel(baseline.baseline_status, canEdit)}
                      description={baseline.baseline_status === 'legacy_unverified'
                        ? canEdit ? '请检查基础内容并按需补录变化。旧资料中未记录的剧情变化无法自动还原。' : '旧资料中未记录的剧情变化无法自动还原，早期设定可能不准确。'
                        : '这里的内容是章节变化应用前的基础值。'}
                      action={baseline.baseline_status === 'legacy_unverified' && canEdit ? (
                        <Popconfirm
                          title="确认基础设定已校准？"
                          description="请先检查并保存基础内容。"
                          okText="确认已校准"
                          cancelText="稍后"
                          okButtonProps={{ disabled: baselineLoading || saving }}
                          onConfirm={() => void editor.confirmBaseline()}
                        >
                          <Button size="small" disabled={baselineLoading || saving}>确认已校准</Button>
                        </Popconfirm>
                      ) : undefined}
                    />
                  )}
                  <Spin spinning={baselineLoading}>
                    <Form form={form} layout="vertical" disabled={saving || !canEdit || (!!editing && (!baseline || baselineLoading))}>
                      <Form.Item
                        name="title"
                        label="标题"
                        rules={[
                          { required: true, whitespace: true, message: '请输入标题' },
                          { max: 200, message: '不超过 200 字' },
                        ]}
                      >
                        <Input placeholder="例如：力量体系 / 青云山 / 天机阁" />
                      </Form.Item>
                      <Form.Item name="category" label="分类">
                        <Select options={CATEGORY_OPTIONS} style={{ width: 200 }} />
                      </Form.Item>
                      {(category === 'place' || category === 'item') && <Form.Item name="aliases" label="别名" extra="别名参与全文提及追踪；最多 10 个，每个最多 50 字。" rules={[{ validator: (_, value: string[] | undefined) => { const result = cleanAliases(value ?? []); return result.error ? Promise.reject(new Error(result.error)) : Promise.resolve() } }]}>
                        <Select mode="tags" tokenSeparators={['，', ',']} placeholder="输入别名后回车" />
                      </Form.Item>}
                      <Form.Item name="content" label={editing ? '基础内容' : '内容'}>
                        <Input.TextArea autoSize={{ minRows: 6, maxRows: 20 }} />
                      </Form.Item>
                      <Form.Item name="sort" label="排序（越小越靠前，可留空）">
                        <InputNumber style={{ width: 160 }} />
                      </Form.Item>
                    </Form>
                  </Spin>
                </Space>
              ),
            },
            ...(editing ? [{
              key: 'progression',
              label: '剧情变化',
              disabled: saving,
              children: tab === 'progression' && open ? (
                <SettingProgressionsPanel
                  key={`${pid}:world:${editing.id}`}
                  projectId={pid}
                  target={{ subject_kind: 'world', world_setting_id: editing.id }}
                  onChanged={editor.progressionChanged}
                  onRequestConfirmBaseline={() => void editor.confirmBaseline()}
                />
              ) : null,
            }] : []),
            ...(editing && (editing.category === 'place' || editing.category === 'item') ? [{ key: 'mentions', label: '提及', children: tab === 'mentions' && open ? <EntityMentions projectId={pid} entity={{ kind: 'world', id: editing.id }} /> : null }] : []),
          ]}
        />
      </Modal>

      {canUseAI && (<WizardModal
        projectId={pid}
        kind="world"
        open={aiOpen}
        existingCount={items.length}
        onClose={() => setAiOpen(false)}
        onDone={() => reload()}
      />)}
    </Card>
  )
}
