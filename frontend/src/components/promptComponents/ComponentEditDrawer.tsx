import { Alert, App, Button, Descriptions, Drawer, Form, Input, Skeleton, Space, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { COMPONENT_LIMITS, COMPONENT_SLUG_RE, promptComponentApi } from '../../api/promptComponents'
import type { PromptComponent } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'
import { classifySaveError, componentRef, refSlugs, type ComponentRejection } from '../../utils/promptComponents'
import { suggestSlug } from '../../utils/skills'
import { formatTime } from '../../utils/time'
import { ComponentFailureList, ComponentUsageList } from './ComponentUsageList'

interface Props {
  open: boolean
  /** null = 新建 */
  componentId: number | null
  onClose: () => void
  onSaved: (component: PromptComponent) => void
}

interface FormValues {
  slug: string
  name: string
  description: string
  content: string
}

const valuesOf = (c: PromptComponent | null): FormValues =>
  c ? { slug: c.slug, name: c.name, description: c.description, content: c.content } : { slug: '', name: '', description: '', content: '' }

/** 组件编辑抽屉：标识 / 名称 / 说明 / 内容 + 使用关系；保存带修订号，冲突时保留草稿。 */
export default function ComponentEditDrawer(props: Props) {
  const { open, componentId, onClose } = props
  const width = useModalWidth(760)
  return (
    <Drawer title={componentId ? '编辑组件' : '新建组件'} width={width} open={open} onClose={onClose} destroyOnHidden>
      {open && (componentId ? <Loaded key={componentId} {...props} componentId={componentId} /> : <ComponentForm key="new" {...props} component={null} />)}
    </Drawer>
  )
}

function Loaded(props: Props & { componentId: number }) {
  const loader = useCallback(() => promptComponentApi.get(props.componentId), [props.componentId])
  const { data, error, reload } = useLoader(loader)
  if (error) return <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重试</Button>} />
  if (!data) return <Skeleton active paragraph={{ rows: 8 }} />
  return <ComponentForm {...props} component={data} />
}

export function ComponentForm({ component, onSaved }: Props & { component: PromptComponent | null }) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  // 服务端最新已知版本：保存用它的修订号；重新加载后更新它但不动草稿
  const [base, setBase] = useState<PromptComponent | null>(component)
  const [rejection, setRejection] = useState<ComponentRejection | null>(null)
  const [rebased, setRebased] = useState(false)
  const [saving, setSaving] = useState(false)
  const [reloading, setReloading] = useState(false)
  const pending = useRef(false)
  const slug = (Form.useWatch('slug', form) as string | undefined) ?? base?.slug ?? ''

  const handleSave = async () => {
    if (pending.current) return
    let v: FormValues
    try {
      v = await form.validateFields()
    } catch {
      return
    }
    const body = { slug: v.slug.trim(), name: v.name.trim(), description: v.description?.trim() ?? '', content: v.content ?? '' }
    pending.current = true
    setSaving(true)
    try {
      const saved = base ? await promptComponentApi.update(base.id, { ...body, revision: base.revision }) : await promptComponentApi.create(body)
      setBase(saved)
      setRejection(null)
      setRebased(false)
      message.success('已保存，引用它的 Skill 与模板覆盖将使用新内容')
      onSaved(saved)
    } catch (err) {
      const found = classifySaveError(err)
      if (found) setRejection(found)
      else message.error(errorMessage(err))
    } finally {
      pending.current = false
      setSaving(false)
    }
  }

  /** 修订冲突后加载最新版本：只更新修订号与使用关系，当前草稿保留 */
  const reloadLatest = async () => {
    if (!base) return
    setReloading(true)
    try {
      setBase(await promptComponentApi.get(base.id))
      setRejection(null)
      setRebased(true)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setReloading(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={
          <span style={{ overflowWrap: 'anywhere' }}>
            在自己的 Skill 正文或模板覆盖中写 <Typography.Text code copyable={{ text: componentRef(slug || '标识') }}>{componentRef(slug || '标识')}</Typography.Text> 引用，按原样展开；修改组件后所有引用方同时生效。
          </span>
        }
        description="组件内不能再引用组件；导出与分享到工坊时会替换为组件内容；全局 Skill 不能引用组件。"
      />
      {rejection && <ComponentRejectionAlert rejection={rejection} reloading={reloading} onReload={() => void reloadLatest()} />}
      {rebased && base && (
        <Alert
          type="info"
          showIcon
          closable
          onClose={() => setRebased(false)}
          message={`已加载最新版本（修订 ${base.revision}），当前草稿保留；再次保存会以草稿覆盖最新版本。`}
          action={
            <Button size="small" onClick={() => { form.setFieldsValue(valuesOf(base)); setRebased(false) }}>
              改用最新内容
            </Button>
          }
        />
      )}
      <Form form={form} layout="vertical" initialValues={valuesOf(component)}>
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: '请输入名称' }]}>
          <Input
            maxLength={COMPONENT_LIMITS.name}
            onBlur={(e) => {
              if (!base && !form.getFieldValue('slug')) form.setFieldValue('slug', suggestSlug(e.target.value))
            }}
          />
        </Form.Item>
        <Form.Item
          name="slug"
          label="标识"
          tooltip="引用时写 {{> 标识 }}；被引用时不能修改"
          rules={[
            { required: true, message: '请输入标识' },
            { pattern: COMPONENT_SLUG_RE, message: '只能使用小写字母、数字和连字符，以字母或数字开头，不超过 48 个字符' },
          ]}
        >
          <Input maxLength={COMPONENT_LIMITS.slug} placeholder="例如 banned-words" />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea maxLength={COMPONENT_LIMITS.description} autoSize={{ minRows: 2, maxRows: 4 }} showCount />
        </Form.Item>
        <Form.Item
          name="content"
          label="内容"
          tooltip="引用到模板覆盖时，可以使用该模板已声明的变量；保存时会对引用它的覆盖重新校验"
          rules={[
            {
              validator: (_, value: string = '') =>
                refSlugs(value).length ? Promise.reject(new Error('组件内不能引用其他组件')) : Promise.resolve(),
            },
          ]}
        >
          <Input.TextArea
            maxLength={COMPONENT_LIMITS.content}
            autoSize={{ minRows: 8, maxRows: 24 }}
            showCount
            spellCheck={false}
            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 13 }}
          />
        </Form.Item>
      </Form>
      <Button type="primary" loading={saving} onClick={() => void handleSave()}>
        保存
      </Button>
      {base && (
        <>
          <Descriptions size="small" column={1} items={[
            { key: 'rev', label: '修订号', children: base.revision },
            { key: 'updated', label: '更新于', children: formatTime(base.updated_at) },
          ]} />
          <Typography.Title level={5} style={{ marginBottom: 0 }}>
            使用关系（{base.usages.length}）
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            被引用时不能删除、不能修改标识。
          </Typography.Text>
          <ComponentUsageList usages={base.usages} empty="还没有 Skill 或模板覆盖引用它" />
        </>
      )}
    </Space>
  )
}

/** 保存被拒的提示：修订冲突（保留草稿，可加载最新版本）/ 被引用不能改标识 / 改内容会让覆盖校验失败 */
export function ComponentRejectionAlert({ rejection, reloading, onReload }: { rejection: ComponentRejection; reloading?: boolean; onReload: () => void }) {
  if (rejection.kind === 'stale')
    return (
      <Alert
        type="warning"
        showIcon
        message="组件已在别处被修改"
        description="当前草稿不会丢失。加载最新版本后可以对比，再决定保存草稿还是改用最新内容。"
        action={
          <Button size="small" loading={reloading} onClick={onReload}>
            加载最新版本
          </Button>
        }
      />
    )
  if (rejection.kind === 'in_use')
    return <Alert type="error" showIcon message={rejection.detail} description={<ComponentUsageList usages={rejection.usages} />} />
  return (
    <Alert
      type="error"
      showIcon
      message="修改后的内容会让以下模板覆盖校验失败，未保存"
      description={<ComponentFailureList failures={rejection.failures} />}
    />
  )
}
