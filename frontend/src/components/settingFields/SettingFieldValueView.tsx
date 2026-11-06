import { Space, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { SettingEntry, SettingFieldOption, SettingFieldValue } from '../../api/types'
import { SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'
import { entityDetailHref } from '../../utils/entityLinks'
import { formatFieldValue } from '../settings/settingFieldDraft'

interface Props { projectId: number; value: SettingFieldValue; options?: readonly SettingFieldOption[]; reference?: SettingEntry | null }

/** 起始值、当前值与历史值共用名称展示，引用只链接身份，不读取目标正文。 */
export default function SettingFieldValueView({ projectId, value, options, reference }: Props) {
  const text = formatFieldValue(value, options, reference)
  if (value.field_type === 'reference' && value.value && reference?.reference.kind === value.value.kind && reference.reference.id === value.value.id) {
    return <Space wrap size={4}><Typography.Text type="secondary">{SETTING_ENTRY_TYPE_LABELS[reference.type]}</Typography.Text><Link to={entityDetailHref(projectId, reference.reference)}>{text}</Link></Space>
  }
  return <Typography.Text style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{text}</Typography.Text>
}
