import { Alert, Button, Empty, Pagination, Select, Space, Spin } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { useMentionQuery as useEntryQuery } from '../../hooks/useMentionQuery'
import { entrySelectOptions, entrySelectValues } from './entrySelectState'
import type { EntrySelectProps, EntrySelectValue } from './entrySelectState'

/** 通过注入的目录分页搜索；调用方负责领域身份和名称，组件只维护搜索与选择交互。 */
export default function EntrySelect<Value extends EntrySelectValue>({
  projectId, queryKey, loadEntries, value = [], onChange, selectedOptions, disabled, single, label, id,
}: EntrySelectProps<Value>) {
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const pager = useRef<HTMLDivElement>(null)
  const load = useCallback((signal: AbortSignal) => loadEntries({ q, page, page_size: 30 }, signal), [loadEntries, q, page])
  const result = useEntryQuery(projectId, JSON.stringify(['entry-select', queryKey, q, page]), load, true, 120)
  const options = entrySelectOptions(result.data?.items ?? [], value, selectedOptions)
  return <Space direction="vertical" size={4} style={{ width: '100%' }}>
    <div onBlurCapture={(event) => { if (pager.current?.contains(event.relatedTarget)) event.stopPropagation() }}>
    <Select<Value | Value[]> id={id} aria-label={label} style={{ width: '100%' }} mode={single ? undefined : 'multiple'} allowClear showSearch
      placeholder={label} value={single ? value[0] : value} disabled={disabled} filterOption={false} loading={result.loading}
      options={options} onSearch={(next) => { setQ(next); setPage(1) }}
      onChange={(next) => { if (!disabled) onChange?.(entrySelectValues(next)) }}
      popupRender={(menu) => <>{menu}<div ref={pager} style={{ padding: 8 }}
        onMouseDown={(event) => { if (!(event.target instanceof HTMLInputElement)) { event.preventDefault(); event.stopPropagation() } }}
        onKeyDown={(event) => { if (event.key !== 'Escape') event.stopPropagation() }}>
        <Pagination simple size="small" current={page} pageSize={30} total={result.data?.total ?? 0} onChange={setPage} />
      </div></>}
      notFoundContent={result.loading ? <Spin size="small" /> : result.error ? '加载失败，请重试' : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的设定" />} />
    </div>
    {result.error && <Alert type="error" showIcon message={result.error} action={<Button size="small" onClick={result.reload}>重试</Button>} />}
  </Space>
}
