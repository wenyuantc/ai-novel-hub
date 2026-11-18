import { Button, Checkbox, Pagination, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { HitPage, SearchHit, SelectedHit } from '../../api/bookSearch'
import { bookSearchHref } from '../../utils/mentions'
import { hitKey, selectable, type SearchMode } from './selection'

interface Props {
  projectId: number
  mode: SearchMode
  result: HitPage
  selected: ReadonlyMap<string, SelectedHit>
  disabled: boolean
  onSelect: (items: SearchHit[], checked: boolean) => void
  onClear: () => void
  onPage: (page: number) => void
}

export default function HitList({ projectId, mode, result, selected, disabled, onSelect, onClear, onPage }: Props) {
  const preview = mode.replacement !== undefined
  const chapters = new Map<number, SearchHit[]>()
  for (const hit of result.items) {
    const group = chapters.get(hit.chapter_id)
    if (group) group.push(hit)
    else chapters.set(hit.chapter_id, [hit])
  }
  const chapterSelections = new Map<number, number>()
  for (const hit of selected.values()) chapterSelections.set(hit.chapter_id, (chapterSelections.get(hit.chapter_id) ?? 0) + 1)
  const eligible = result.items.filter(selectable)
  const checkedOnPage = eligible.filter((hit) => selected.has(hitKey(hit))).length

  return <section aria-label={preview ? '替换预览' : '搜索结果'} className="book-search-results">
    <div className="book-search-bar">
      <Typography.Title level={2}>{preview ? '替换预览' : '搜索结果'} <span>共 {result.total} 处</span></Typography.Title>
      {preview && eligible.length > 0 && <Checkbox disabled={disabled} checked={checkedOnPage === eligible.length}
        indeterminate={checkedOnPage > 0 && checkedOnPage < eligible.length} onChange={(e) => onSelect(eligible, e.target.checked)}>
        勾选本页
      </Checkbox>}
    </div>
    {!result.total && <p className="book-search-empty">所选章节中未找到该原文。</p>}
    {[...chapters].map(([id, hits]) => <article key={id} className="book-search-chapter">
      <div className="book-search-chapter-title"><h3>第 {hits[0].chapter_no} 章 · {hits[0].chapter_title}</h3>
        {preview && <span>本章已选 {chapterSelections.get(id) ?? 0} 处</span>}
      </div>
      <ul>{hits.map((hit) => <li key={hitKey(hit)}>
        {preview && <Checkbox aria-label={`选择第 ${hit.chapter_no} 章位置 ${hit.start + 1} 的命中`}
          disabled={disabled || !selectable(hit)} checked={selected.has(hitKey(hit))}
          onChange={(e) => onSelect([hit], e.target.checked)} />}
        <div className="book-search-hit">
          <div className="book-search-excerpt">{hit.before}<mark>{mode.query}</mark>{hit.after}</div>
          {preview && !hit.unavailable_reason && <div className="book-search-excerpt book-search-after" aria-label="替换后">
            {hit.before}{mode.replacement === '' ? <em>〔删除〕</em> : <ins>{mode.replacement}</ins>}{hit.after}
          </div>}
          {preview && hit.unavailable_reason && <div className="book-search-warning">无法批改 · {hit.unavailable_reason}</div>}
          {preview && hit.warning && <div className="book-search-warning">{hit.warning}</div>}
        </div>
        <div className="book-search-location">
          <small>位置 {hit.start + 1}</small>
          <Link target="_blank" rel="noopener noreferrer"
          aria-label={`定位第 ${hit.chapter_no} 章位置 ${hit.start + 1} 的原文（新标签页）`}
          to={bookSearchHref(projectId, hit.chapter_id, hit)}>定位原文</Link>
        </div>
      </li>)}</ul>
    </article>)}
    {result.total > result.page_size && <Pagination aria-label="命中页码" disabled={disabled} current={result.page}
      total={result.total} pageSize={result.page_size} showSizeChanger={false} onChange={onPage} />}
    {preview && selected.size > 0 && <Button type="link" disabled={disabled} onClick={onClear}>清空全部勾选</Button>}
  </section>
}
