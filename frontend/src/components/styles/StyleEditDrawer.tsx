import { CopyOutlined, ExperimentOutlined } from '@ant-design/icons'
import { Alert, App, Button, Drawer, Form, Space } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { emptyStyleDraft, styleApi } from '../../api/styles'
import type { StyleDraft, WritingStyle } from '../../api/types'
import { draftToStyleBody, styleToDraft } from '../../utils/styles'
import StyleFormFields from './StyleFormFields'

interface Props {
  open: boolean
  /** null = 新建 */
  style: WritingStyle | null
  /** 新建时的初始内容（如从样文提取的草稿） */
  initialDraft?: StyleDraft | null
  onClose: () => void
  onSaved: (style: WritingStyle) => void
  /** 预设：复制为我的风格 */
  onCopy?: (style: WritingStyle) => void
  /** 用当前表单内容试写（未保存的修改也生效） */
  onTrial?: (draft: StyleDraft) => void
}

/** 风格编辑抽屉：新建 / 编辑我的风格；预设只读，可复制后修改。 */
export default function StyleEditDrawer(props: Props) {
  const { open, style, onClose } = props
  return (
    <Drawer
      title={style ? (style.is_preset ? `预设风格：${style.name}` : `编辑风格：${style.name}`) : '新建风格'}
      width={760}
      open={open}
      onClose={onClose}
      destroyOnHidden
    >
      {open && <Inner key={style?.id ?? 'new'} {...props} />}
    </Drawer>
  )
}

function Inner({ style, initialDraft, onSaved, onCopy, onTrial }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<StyleDraft>()
  const [saving, setSaving] = useState(false)
  const readOnly = style?.is_preset ?? false
  const initial = style ? styleToDraft(style) : (initialDraft ?? emptyStyleDraft())

  const handleSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const body = draftToStyleBody({ ...emptyStyleDraft(), ...v })
      const saved = style ? await styleApi.update(style.id, body) : await styleApi.create(body)
      message.success('已保存')
      onSaved(saved)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {readOnly && (
        <Alert
          type="info"
          showIcon
          message="预设风格只读，可以复制为我的风格后修改"
          action={
            style &&
            onCopy && (
              <Button size="small" icon={<CopyOutlined />} onClick={() => onCopy(style)}>
                复制
              </Button>
            )
          }
        />
      )}
      {style && !readOnly && (style.references.projects > 0 || style.references.chapters > 0) && (
        <Alert
          type="info"
          showIcon
          message={`正在被 ${style.references.projects} 个项目、${style.references.chapters} 个章节使用，修改会影响它们之后的生成`}
        />
      )}
      <Form form={form} layout="vertical" initialValues={initial} disabled={readOnly}>
        <StyleFormFields disabled={readOnly} />
      </Form>
      <Space>
        {!readOnly && (
          <Button type="primary" loading={saving} onClick={() => void handleSave()}>
            保存
          </Button>
        )}
        {onTrial && (
          <Button
            icon={<ExperimentOutlined />}
            onClick={() => onTrial({ ...emptyStyleDraft(), ...form.getFieldsValue(true) })}
          >
            试写一段
          </Button>
        )}
      </Space>
    </Space>
  )
}
