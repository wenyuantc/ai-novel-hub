import { Alert, Checkbox, Space, Typography } from 'antd'
import type { SceneExtractPreview, SceneRestorePreview } from '../../api/types'

type RichPreview = SceneExtractPreview | SceneRestorePreview

interface Props {
  preview: RichPreview | undefined
  confirmed: boolean
  disabled: boolean
  onConfirm: (value: boolean) => void
}

export default function SceneRichHistoryNotice({ preview, confirmed, disabled, onConfirm }: Props) {
  if (!preview) return null
  return <>
    {!!preview.rich_changes?.length && <Alert type="warning" showIcon message="富文档资料将随历史变化" description={<Space direction="vertical" size={4}>{preview.rich_changes.map((change) => <Typography.Text key={change.scene_id}>场景 #{change.scene_id}{change.will_retire ? ' 将退场' : ''}：区块 {change.before_sections} → {change.after_sections}{change.sections_changed ? '（内容或位置改变）' : ''}，标记 {change.before_markers} → {change.after_markers}{change.markers_changed ? '（标记改变）' : ''}，正文格式范围 {change.before_format_ranges} → {change.after_format_ranges}{change.format_changed ? '（格式改变）' : ''}</Typography.Text>)}<Typography.Text>区块文字、标记和格式按目标版本恢复；当前资料先存入可恢复历史，不会仅凭纯文本猜测。</Typography.Text></Space>} />}
    {preview.requires_rich_confirmation && <Checkbox checked={confirmed} disabled={disabled} onChange={(event) => onConfirm(event.target.checked)}>我已核对上述变化，确认恢复会改变当前区块、标记或格式</Checkbox>}
  </>
}
