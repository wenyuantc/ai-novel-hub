import { can } from '../../utils/permissions'
import { useAuthStore } from '../../stores/auth'
import { useAuthorizationScope } from '../../hooks/useAuthorizationScope'
import { ArrowDownOutlined, ArrowUpOutlined, CopyOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Drawer, Form, Input, Radio, Select, Skeleton, Space, Tabs, Tooltip, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { SKILL_LIMITS, SKILL_SLUG_RE, skillApi } from '../../api/skills'
import type { Skill, SkillReferenceInput, SkillScope } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'
import { normalizeTriggers, suggestSlug } from '../../utils/skills'
import { componentRef, insertSnippet } from '../../utils/promptComponents'
import AgentMarkdown from '../agent/AgentMarkdown'
import { ComponentInsertSelect, ComponentRefTags } from '../promptComponents/ComponentRefs'

interface Props {
  open: boolean
  /** null = 新建 */
  skillId: number | null
  /** 新建时的归属（global 仅管理员） */
  scope: SkillScope
  isAdmin: boolean
  onClose: () => void
  onSaved: (skill: Skill) => void
  /** 只读的全局 Skill：复制为我的 */
  onCopy: (skill: Skill) => void
}

interface FormValues {
  scope: SkillScope
  slug: string
  name: string
  description: string
  category: string
  triggers: string[]
  body: string
  references: SkillReferenceInput[]
}

/** Skill 编辑抽屉：基本信息、触发词、正文（编辑 / 预览）、参考资料（增删排序）。不可写的全局 Skill 只读，可复制为我的。 */
export default function SkillEditDrawer(props: Props) {
  const { open, skillId, onClose } = props
  const width = useModalWidth(760)
  return (
    <Drawer title={skillId ? 'Skill 详情' : '新建 Skill'} width={width} open={open} onClose={onClose} destroyOnHidden>
      {open && (skillId ? <Loaded key={skillId} {...props} skillId={skillId} /> : <SkillForm key="new" {...props} skill={null} />)}
    </Drawer>
  )
}

function Loaded(props: Props & { skillId: number }) {
  const loader = useCallback(() => skillApi.get(props.skillId), [props.skillId])
  const { data, error, reload } = useLoader(loader)
  if (error) return <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重试</Button>} />
  if (!data) return <Skeleton active paragraph={{ rows: 8 }} />
  return <SkillForm key={data.updated_at} {...props} skill={data} />
}

const refsKey = (refs: SkillReferenceInput[]) => JSON.stringify(refs.map((r) => [r.title.trim(), r.content]))

export function SkillForm({ skill, scope, isAdmin, onSaved, onCopy }: Props & { skill: Skill | null }) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)
  const [tab, setTab] = useState<'edit' | 'preview'>('edit')
  const actor = useAuthStore((state) => state.user)
  const selectedScope = Form.useWatch('scope', form) as SkillScope | undefined
  const global = (skill?.scope ?? selectedScope ?? scope) === 'global'
  const operation = useAuthorizationScope(undefined, false, global)
  const readOnly = skill !== null ? global ? !can(actor, 'global_skills.update') : !skill.editable : global && !can(actor, 'global_skills.create')
  const canReferences = skill ? !global || can(actor, 'global_skills.references') : !readOnly
  const body = Form.useWatch('body', form) ?? ''
  const initial: FormValues = skill
    ? {
        scope: skill.scope,
        slug: skill.slug,
        name: skill.name,
        description: skill.description,
        category: skill.category,
        triggers: skill.triggers,
        body: skill.body,
        references: skill.references.map((r) => ({ title: r.title, content: r.content })),
      }
    : { scope, slug: '', name: '', description: '', category: '', triggers: [], body: '', references: [] }
  // 上次保存的参考资料：没变化时不调用整体替换接口
  const savedRefs = useRef(refsKey(initial.references))
  const pending = useRef(false)
  // 引用的组件：保存后以服务端响应为准（未保存的改动不实时解析）
  const [components, setComponents] = useState(skill?.components ?? [])
  // 正文光标（UTF-16），插入组件引用时用；没有时插在末尾
  const bodyCaret = useRef<[number, number] | null>(null)
  const syncCaret = (el: HTMLTextAreaElement) => {
    bodyCaret.current = [el.selectionStart, el.selectionEnd]
  }
  const insertComponent = (slug: string) => {
    const next = insertSnippet(form.getFieldValue('body') ?? '', componentRef(slug), bodyCaret.current)
    form.setFieldValue('body', next.text)
    bodyCaret.current = [next.caret, next.caret]
    setTab('edit')
    requestAnimationFrame(() => {
      const el = document.getElementById('skill-body')
      if (!(el instanceof HTMLTextAreaElement)) return
      el.focus()
      el.setSelectionRange(next.caret, next.caret)
    })
  }

  const handleSave = async () => {
    if ((readOnly && !canReferences) || pending.current || !operation.allowed()) return
    pending.current = true
    const ticket = operation.begin()
    let v: FormValues
    try {
      v = await form.validateFields()
    } catch {
      if (operation.current(ticket)) pending.current = false
      return
    }
    if (!operation.current(ticket) || (!skill && v.scope === 'global' && !can(useAuthStore.getState().user, 'global_skills.create'))) return
    const fields = {
      slug: v.slug.trim(),
      name: v.name.trim(),
      description: v.description?.trim() ?? '',
      category: v.category?.trim() ?? '',
      triggers: normalizeTriggers(v.triggers ?? []),
      body: v.body ?? '',
    }
    const references = (v.references ?? []).map((r) => ({ title: r.title.trim(), content: r.content ?? '' }))
    setSaving(true)
    try {
      let saved: Skill
      if (!skill) {
        saved = await skillApi.create({ ...fields, scope: v.scope, references })
      } else {
        saved = readOnly ? skill : await skillApi.update(skill.id, fields)
        if (refsKey(references) !== savedRefs.current) {
          if (!canReferences || !operation.current(ticket)) return
          saved = await skillApi.replaceReferences(skill.id, references)
        }
      }
      if (!operation.current(ticket)) return
      savedRefs.current = refsKey(references)
      setComponents(saved.components ?? [])
      message.success('已保存')
      onSaved(saved)
    } catch (err) {
      if (operation.current(ticket)) message.error(errorMessage(err))
    } finally {
      if (operation.current(ticket)) { pending.current = false; setSaving(false) }
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {readOnly && skill && (
        <Alert
          type="info"
          showIcon
          message="全局 Skill 由管理员维护，只读"
          description="可以复制为我的 Skill 后修改。"
          action={
            <Button size="small" icon={<CopyOutlined />} onClick={() => onCopy(skill)}>
              复制为我的
            </Button>
          }
        />
      )}
      <Form form={form} layout="vertical" initialValues={initial}>
        {!skill && isAdmin && (
          <Form.Item name="scope" label="归属">
            <Radio.Group
              options={[
                { value: 'mine', label: '我的' },
                { value: 'global', label: '全局（所有用户可见）' },
              ]}
            />
          </Form.Item>
        )}
        <Form.Item
          name="name"
          label="名称"
          rules={[{ required: true, whitespace: true, message: '请输入名称' }]}
        >
          <Input disabled={readOnly}
            maxLength={SKILL_LIMITS.name}
            onBlur={(e) => {
              if (!skill && !form.getFieldValue('slug')) form.setFieldValue('slug', suggestSlug(e.target.value))
            }}
          />
        </Form.Item>
        <Form.Item
          name="slug"
          label="标识"
          tooltip="智能体用它加载 Skill，导出时作为 SKILL.md 的 name"
          rules={[
            { required: true, message: '请输入标识' },
            { pattern: SKILL_SLUG_RE, message: '只能使用小写字母、数字和连字符，以字母或数字开头，不超过 48 个字符' },
          ]}
        >
          <Input disabled={readOnly} maxLength={48} placeholder="例如 long-continue" />
        </Form.Item>
        <Form.Item name="description" label="说明" tooltip="展示在智能体的 Skill 清单里，帮助模型判断何时使用">
          <Input.TextArea disabled={readOnly} maxLength={SKILL_LIMITS.description} autoSize={{ minRows: 2, maxRows: 4 }} showCount />
        </Form.Item>
        <Form.Item name="category" label="分类">
          <Input disabled={readOnly} maxLength={SKILL_LIMITS.category} placeholder="例如 续写、润色、诊断" />
        </Form.Item>
        <Form.Item
          name="triggers"
          label="触发词"
          tooltip="在智能体输入框以 /触发词 开头，可为本轮强制启用该 Skill"
          normalize={(value: string[]) => normalizeTriggers(value ?? [])}
          rules={[
            {
              validator: (_, value: string[] = []) => {
                if (value.length > SKILL_LIMITS.triggers) return Promise.reject(new Error(`最多 ${SKILL_LIMITS.triggers} 个触发词`))
                if (value.some((t) => Array.from(t).length > SKILL_LIMITS.trigger))
                  return Promise.reject(new Error(`每个触发词不超过 ${SKILL_LIMITS.trigger} 字`))
                return Promise.resolve()
              },
            },
          ]}
        >
          <Select disabled={readOnly} mode="tags" tokenSeparators={[' ', ',', '，']} open={false} placeholder="输入后回车，例如 续写" />
        </Form.Item>
        <Form.Item label="工作流正文（Markdown）">
          {global ? (
            !readOnly && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 8 }}
                message="全局 Skill 不能引用提示词组件（组件属于个人），请把内容直接写进正文。"
              />
            )
          ) : (
            <Space wrap size={8} style={{ marginBottom: 8 }}>
              <ComponentRefTags components={components} />
              {!readOnly && (
                <Tooltip title="在正文光标处插入 {{> 标识 }}；生成任务与智能体读取时按原样展开组件内容">
                  <span>
                    <ComponentInsertSelect onInsert={insertComponent} />
                  </span>
                </Tooltip>
              )}
            </Space>
          )}
          <Tabs
            size="small"
            activeKey={tab}
            onChange={(k) => setTab(k as 'edit' | 'preview')}
            items={[
              {
                key: 'edit',
                label: '编辑',
                children: (
                  <Form.Item name="body" noStyle>
                    <Input.TextArea
                      id="skill-body"
                      disabled={readOnly}
                      maxLength={SKILL_LIMITS.body}
                      autoSize={{ minRows: 8, maxRows: 24 }}
                      showCount
                      onSelect={(e) => syncCaret(e.currentTarget)}
                      onKeyUp={(e) => syncCaret(e.currentTarget)}
                      onMouseUp={(e) => syncCaret(e.currentTarget)}
                    />
                  </Form.Item>
                ),
              },
              {
                key: 'preview',
                label: '预览',
                children: body.trim() ? <AgentMarkdown text={body} /> : <Typography.Text type="secondary">（正文为空）</Typography.Text>,
              },
            ]}
          />
        </Form.Item>
        <Typography.Title level={5}>参考资料</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          智能体按需逐篇读取，不会一次全部放进提示词。标题即导出时的文件名。
        </Typography.Paragraph>
        <fieldset disabled={!canReferences} style={{ border: 0, padding: 0, minWidth: 0 }}><Form.List name="references">
          {(fields, { add, remove, move }) => (
            <Space direction="vertical" style={{ width: '100%' }}>
              {fields.map((field, index) => (
                <Card
                  key={field.key}
                  size="small"
                  title={`第 ${index + 1} 篇`}
                  extra={
                    canReferences && (
                      <Space size={0}>
                        <Button type="text" size="small" aria-label="上移" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => move(index, index - 1)} />
                        <Button type="text" size="small" aria-label="下移" icon={<ArrowDownOutlined />} disabled={index === fields.length - 1} onClick={() => move(index, index + 1)} />
                        <Button type="text" size="small" danger aria-label="删除" icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
                      </Space>
                    )
                  }
                >
                  <Form.Item
                    name={[field.name, 'title']}
                    label="标题"
                    rules={[
                      { required: true, whitespace: true, message: '请输入标题' },
                      { pattern: /^[^./\\][^/\\]*$/, message: '不能包含 / \\，也不能以 . 开头' },
                    ]}
                  >
                    <Input maxLength={SKILL_LIMITS.referenceTitle} />
                  </Form.Item>
                  <Form.Item name={[field.name, 'content']} label="内容" style={{ marginBottom: 0 }}>
                    <Input.TextArea maxLength={SKILL_LIMITS.referenceContent} autoSize={{ minRows: 3, maxRows: 12 }} />
                  </Form.Item>
                </Card>
              ))}
              {canReferences && (
                <Button
                  icon={<PlusOutlined />}
                  disabled={fields.length >= SKILL_LIMITS.references}
                  onClick={() => add({ title: '', content: '' })}
                >
                  添加参考资料{fields.length >= SKILL_LIMITS.references ? `（最多 ${SKILL_LIMITS.references} 篇）` : ''}
                </Button>
              )}
            </Space>
          )}
        </Form.List></fieldset>
      </Form>
      {(!readOnly || canReferences) && (
        <Button type="primary" loading={saving} onClick={() => void handleSave()}>
          保存
        </Button>
      )}
    </Space>
  )
}
