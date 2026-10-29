import { Collapse, Typography, theme } from 'antd'
import type { WorkshopKind } from '../../api/types'
import { STYLE_DIMENSIONS } from '../../utils/styles'

const styleLabels: Record<string, string> = {
  description: '风格描述', ...Object.fromEntries(STYLE_DIMENSIONS.map((d) => [d.key, d.label])),
  taboos: '禁用表达', sample: '示例段落', extra: '自由补充',
}
/** 正文一律纯文本展示；不执行模板、不渲染任意 HTML。 */
export default function WorkshopContent({ kind, content }: { kind: WorkshopKind; content: Record<string, string> }) {
  const { token } = theme.useToken()
  const fields = kind === 'prompt' ? [['system', '系统段'], ['user', '用户段']] : Object.entries(styleLabels)
  return <Collapse defaultActiveKey={fields.filter(([key]) => content[key]).map(([key]) => key)} items={fields
    .filter(([key]) => content[key])
    .map(([key, label]) => ({ key, label, children: <Typography.Paragraph style={{
      whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 440, overflow: 'auto',
      fontFamily: kind === 'prompt' ? 'monospace' : undefined, color: token.colorText,
    }}>{content[key]}</Typography.Paragraph> }))} />
}
