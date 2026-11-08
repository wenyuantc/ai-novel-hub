import { App, Space, Tooltip, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import type { StyleBrief } from '../../api/types'
import StyleSelect from '../styles/StyleSelect'

interface Props {
  pid: number
  chapterId: number
  /** 本章风格覆盖（null = 继承项目默认） */
  value: number | null
  projectDefault: StyleBrief | null
  disabled?: boolean
  readOnly?: boolean
  hideLabel?: boolean
  style?: React.CSSProperties
  onChanged: (styleId: number | null) => void
}

/** 本章写作风格（继承项目默认或指定）：变更即 PATCH style_id，与正文保存分开、不影响 dirty。 */
export default function ChapterStyleSetting({ pid, chapterId, value, projectDefault, disabled, readOnly, hideLabel, style, onChanged }: Props) {
  const { message } = App.useApp()
  const [saving, setSaving] = useState(false)

  const handleChange = async (styleId: number | null) => {
    setSaving(true)
    try {
      // 只发 style_id：响应里的正文不回写，避免覆盖未保存的修改
      const saved = await chapterApi.update(pid, chapterId, { style_id: styleId })
      onChanged(saved.style_id)
      message.success(styleId === null ? '本章改为继承项目默认风格' : '已设置本章风格')
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const inherit = projectDefault ? `继承项目默认（${projectDefault.name}）` : '继承项目默认（不用风格）'
  if (readOnly) return <Typography.Paragraph type="secondary" style={{ marginBottom: hideLabel ? 0 : 8 }}>本章风格：{value === null ? inherit : `风格 #${value}`}（保留当前绑定）</Typography.Paragraph>

  const select = (
    <StyleSelect
      value={value}
      onChange={(v) => void handleChange(v)}
      disabled={disabled || saving}
      placeholder={inherit}
      style={{ width: hideLabel ? '100%' : undefined, minWidth: hideLabel ? 0 : 'min(320px, calc(100vw - 130px))', ...style }}
    />
  )

  if (hideLabel) {
    return select
  }

  return (
    <Space size={8} style={{ width: '100%', marginBottom: 8 }} align="center">
      <Tooltip title={<span>生成、润色、重写与局部改写默认使用本章风格；未设置时继承项目默认。<Link to="/styles">管理风格库</Link></span>}>
        <Typography.Text type="secondary" style={{ whiteSpace: 'nowrap' }}>
          本章风格
        </Typography.Text>
      </Tooltip>
      {select}
    </Space>
  )
}
