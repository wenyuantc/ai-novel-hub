import { useProjectAccess } from '../../hooks/useProjectAccess'
import { App, Select, Space, Tooltip, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { castApi, characterApi } from '../../api/story'
import { useLoader } from '../../hooks/useLoader'

interface Props {
  pid: number
  chapterId: number
  disabled?: boolean
}

/**
 * 本章出场角色（可选，手动指定）：变更即 PUT /cast 整体替换，与正文保存分开、不影响 dirty。
 * 这是明确的人工引用，仍受条目策略与字段可见性限制。
 */
export default function CastSelect({ pid, chapterId, disabled }: Props) {
  const { canEdit, canEditNow } = useProjectAccess(pid)
  const { message } = App.useApp()
  const charLoader = useCallback(() => characterApi.list(pid), [pid])
  const { data: characters = [] } = useLoader(charLoader)
  const castLoader = useCallback(() => castApi.get(pid, chapterId), [pid, chapterId])
  const { data: cast, loading, setData } = useLoader(castLoader)
  const [saving, setSaving] = useState(false)
  const value = cast?.character_ids ?? []

  const handleChange = async (ids: number[]) => {
    if (!canEditNow()) return
    const prev = value
    setData(() => ({ character_ids: ids }))
    setSaving(true)
    try {
      if (!canEditNow()) return
      const saved = await castApi.set(pid, chapterId, ids)
      if (!canEditNow()) return
      setData(() => saved)
    } catch (err) {
      if (!canEditNow()) return
      setData(() => ({ character_ids: prev }))
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space size={8} style={{ width: '100%', marginBottom: 8 }} align="center">
      <Tooltip
        title="为本章手动指定出场角色。「永不加入」的角色和隐藏字段仍不会提供给 AI。"
      >
        <Typography.Text type="secondary" style={{ whiteSpace: 'nowrap' }}>
          出场角色
        </Typography.Text>
      </Tooltip>
      <Select
        mode="multiple"
        size="small"
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder="可选：手动指定本章出场角色"
        style={{ minWidth: 'min(320px, calc(100vw - 130px))' }}
        value={value}
        loading={loading || saving}
        disabled={!canEdit || (disabled || loading)}
        onChange={(ids: number[]) => void handleChange(ids)}
        options={characters.map((c) => ({ value: c.id, label: c.name }))}
        maxTagCount="responsive"
      />
    </Space>
  )
}
