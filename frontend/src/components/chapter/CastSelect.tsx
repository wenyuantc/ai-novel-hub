import { App, Select, Space, Tooltip, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { isProtagonist } from '../../api/characters'
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
 * 生成时与主角、自动匹配（梗概 + 上一章结尾）取并集注入完整角色卡。
 */
export default function CastSelect({ pid, chapterId, disabled }: Props) {
  const { message } = App.useApp()
  const charLoader = useCallback(() => characterApi.list(pid), [pid])
  const { data: characters = [] } = useLoader(charLoader)
  const castLoader = useCallback(() => castApi.get(pid, chapterId), [pid, chapterId])
  const { data: cast, loading, setData } = useLoader(castLoader)
  const [saving, setSaving] = useState(false)
  const value = cast?.character_ids ?? []

  const handleChange = async (ids: number[]) => {
    const prev = value
    setData(() => ({ character_ids: ids }))
    setSaving(true)
    try {
      const saved = await castApi.set(pid, chapterId, ids)
      setData(() => saved)
    } catch (err) {
      setData(() => ({ character_ids: prev }))
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const protagonists = characters.filter(isProtagonist).map((c) => c.name)
  return (
    <Space size={8} style={{ width: '100%', marginBottom: 8 }} align="center">
      <Tooltip
        title={`主角${protagonists.length ? `（${protagonists.join('、')}）` : ''}始终注入；本章梗概与上一章结尾提到的角色会自动匹配。这里可额外指定。`}
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
        disabled={disabled || loading}
        onChange={(ids: number[]) => void handleChange(ids)}
        options={characters.map((c) => ({ value: c.id, label: c.name }))}
        maxTagCount="responsive"
      />
    </Space>
  )
}
