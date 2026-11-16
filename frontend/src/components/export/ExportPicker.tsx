import { Alert, Button, Checkbox, Empty, Input, Pagination, Select, Space, Spin, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { errorMessage } from '../../api/client'
import { updateSelection, type ExportItem } from './selection'

export interface ExportPickerPage { items: ExportItem[]; total: number }
export type ExportPickerLoader = (page: number, query: string, category: string, signal: AbortSignal) => Promise<ExportPickerPage>

interface Props {
  title: string
  selected: ExportItem[]
  onChange: (value: ExportItem[]) => void
  load: ExportPickerLoader
  isCurrent: () => boolean
  search?: boolean
  categories?: { value: string; label: string }[]
  disabled?: boolean
}

const PAGE_SIZE = 20
export const EXPORT_PICKER_PAGE_SIZE = PAGE_SIZE

/** 有界目录选择器：仅对本页操作，已选项跨查询、分类及翻页保留。 */
export default function ExportPicker({ title, selected, onChange, load, isCurrent, search, categories, disabled }: Props) {
  const [page, setPage] = useState(1)
  const [queryDraft, setQueryDraft] = useState('')
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const [version, setVersion] = useState(0)
  const [data, setData] = useState<ExportPickerPage>({ items: [], total: 0 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    void load(page, query, category, controller.signal).then((value) => {
      if (!active || !isCurrent()) return
      setData(value)
      setError(null)
      setLoading(false)
    }).catch((err: unknown) => {
      if (!active || !isCurrent() || controller.signal.aborted) return
      setError(errorMessage(err))
      setData({ items: [], total: 0 })
      setLoading(false)
    })
    return () => { active = false; controller.abort() }
  }, [page, query, category, load, version, isCurrent])

  const selectedKeys = new Set(selected.map((item) => item.key))
  const selectable = data.items.filter((item) => !item.disabled)
  const onPage = selectable.filter((item) => selectedKeys.has(item.key)).length
  const refresh = () => { setLoading(true); setVersion((value) => value + 1) }
  const setNextPage = (value: number) => { if (value !== page) { setLoading(true); setPage(value) } }
  const setFilter = (action: () => void) => { setLoading(true); setPage(1); action() }

  return (
    <section aria-label={`${title}选择`}>
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        {(search || categories) && <Space wrap style={{ width: '100%' }}>
          {categories && <Select
            aria-label={`${title}分类`}
            value={category}
            disabled={disabled}
            options={[{ value: '', label: '全部分类' }, ...categories]}
            style={{ minWidth: 145 }}
            onChange={(value: string) => setFilter(() => setCategory(value))}
          />}
          {search && <Input.Search
            aria-label={`搜索${title}`}
            value={queryDraft}
            onChange={(event) => setQueryDraft(event.target.value)}
            onSearch={(value) => {
              const next = value.trim()
              if (next === query && page === 1) { refresh(); return }
              setFilter(() => setQuery(next))
            }}
            placeholder={`搜索${title}`}
            allowClear
            disabled={disabled}
            style={{ width: 205, maxWidth: '100%' }}
          />}
        </Space>}
        <Space wrap>
          <Checkbox
            checked={selectable.length > 0 && onPage === selectable.length}
            indeterminate={onPage > 0 && onPage < selectable.length}
            disabled={disabled || loading || !!error || !selectable.length}
            onChange={(event) => onChange(updateSelection(selected, data.items, event.target.checked))}
          >本页全选</Checkbox>
          <Typography.Text>已选 {selected.length} 项</Typography.Text>
          <Button size="small" disabled={disabled || selected.length === 0} onClick={() => onChange([])}>清空已选</Button>
        </Space>
        {selected.length > 0 && <div aria-label={`已选${title}`} style={{ maxHeight: 110, overflowY: 'auto', overflowWrap: 'anywhere' }}>
          {selected.map((item) => <div key={item.key}>
            <Typography.Text>{item.label}</Typography.Text>
            <Button type="link" size="small" disabled={disabled} aria-label={`移除已选${item.label}`}
              onClick={() => onChange(selected.filter((entry) => entry.key !== item.key))}>移除</Button>
          </div>)}
        </div>}
        {error && <Alert type="error" showIcon message={error}
          action={<Button size="small" onClick={refresh} disabled={disabled}>重试</Button>} />}
        {!error && <Spin spinning={loading}>
          <div style={{ maxHeight: 220, minHeight: 50, overflowY: 'auto' }}>
            {data.items.length === 0 && !loading && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无条目" />}
            {data.items.map((item) => <div key={item.key} style={{ padding: '4px 0', overflowWrap: 'anywhere' }}>
              <Checkbox checked={selectedKeys.has(item.key)} disabled={disabled || item.disabled || loading}
                onChange={(event) => onChange(updateSelection(selected, [item], event.target.checked))}>
                {item.label}{item.description && <Typography.Text type="secondary">（{item.description}）</Typography.Text>}
              </Checkbox>
            </div>)}
          </div>
        </Spin>}
        {data.total > PAGE_SIZE && <Pagination simple size="small" current={page} total={data.total} pageSize={PAGE_SIZE}
          disabled={disabled || loading} onChange={setNextPage} showSizeChanger={false} />}
      </Space>
    </section>
  )
}
