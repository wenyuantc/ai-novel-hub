import { Select, Space, Tag, Tooltip, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { promptComponentApi } from '../../api/promptComponents'
import type { PromptComponentBrief, PromptComponentRef } from '../../api/types'
import { componentRef } from '../../utils/promptComponents'

/** 「引用的组件」：每个组件一个标签，点击跳到组件库并打开该组件 */
export function ComponentRefTags({ components }: { components: PromptComponentRef[] }) {
  return (
    <Space size={4} wrap aria-label="引用的组件">
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        引用的组件：
      </Typography.Text>
      {components.length === 0 ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          无
        </Typography.Text>
      ) : (
        components.map((c) => (
          <Tooltip key={c.id} title={`${componentRef(c.slug)}，点击查看组件`}>
            <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>
              <Link to={`/prompt-components?id=${c.id}`}>{c.name}</Link>
            </Tag>
          </Tooltip>
        ))
      )}
    </Space>
  )
}

interface InsertProps {
  onInsert: (slug: string) => void
  disabled?: boolean
}

/** 插入组件引用的快捷选择：展开时加载我的组件，选中后插入 `{{> slug }}` 并清空选择 */
export function ComponentInsertSelect({ onInsert, disabled }: InsertProps) {
  const [items, setItems] = useState<PromptComponentBrief[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    if (loading) return
    setLoading(true)
    try {
      setItems(await promptComponentApi.list())
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Select<string>
      aria-label="插入组件引用"
      size="small"
      showSearch
      disabled={disabled}
      value={null}
      placeholder="插入组件引用"
      loading={loading}
      style={{ minWidth: 160, maxWidth: '100%' }}
      popupMatchSelectWidth={false}
      onOpenChange={(open) => {
        if (open) void load()
      }}
      notFoundContent={
        error ?? (loading ? '加载中…' : <Link to="/prompt-components">还没有组件，去组件库新建</Link>)
      }
      optionFilterProp="label"
      options={(items ?? []).map((c) => ({ value: c.slug, label: `${c.name}（${c.slug}）` }))}
      onChange={(slug) => {
        if (slug) onInsert(slug)
      }}
    />
  )
}
