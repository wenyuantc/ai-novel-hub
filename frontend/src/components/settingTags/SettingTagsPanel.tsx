import { Alert, Button, Select, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useId, useMemo, useSyncExternalStore } from 'react'
import type { SettingEntryRef } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { SettingTagsSession, tagsDraftDirty } from './settingTagsController'
import { preventTagFormSubmit } from './tagDraft'

interface Props { session: SettingTagsSession; entry: SettingEntryRef }

function Tags({ values }: { values: readonly string[] }) {
  return values.length ? <Space size={[4, 8]} wrap style={{ maxWidth: '100%' }}>{values.map((value) => (
    <Tag key={value} style={{ marginInlineEnd: 0, maxWidth: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{value}</Tag>
  ))}</Space> : <Typography.Text type="secondary">无标签</Typography.Text>
}

/** 独立保存当前整理标签，不进入外层基础表单和剧情字段的保存流程。 */
export default function SettingTagsPanel({ session, entry }: Props) {
  const controller = useMemo(() => session.get({ kind: entry.kind, id: entry.id }), [session, entry.kind, entry.id])
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const { canRead, canEdit } = useProjectAccess(session.projectId)
  const labelId = useId()
  const inputId = useId()
  const helpId = useId()
  const errorId = useId()
  const loaded = state.base !== null
  useEffect(() => { void controller.open(); return controller.close }, [controller])
  useEffect(() => {
    if (!canEdit || !loaded) return
    const timer = setTimeout(() => { void controller.suggest() }, 180)
    return () => { clearTimeout(timer) }
  }, [canEdit, controller, state.search, loaded])

  if (!canRead) return null
  const dirty = tagsDraftDirty(state)
  const busy = state.saving || state.reviewing
  const options = state.suggestions.filter((tag) => !state.draft.includes(tag)).map((value) => ({ label: value, value }))
  return (
    <section aria-labelledby={labelId} style={{ minWidth: 0, maxWidth: '100%', display: 'grid', gap: 16 }}>
      <div>
        <Typography.Title id={labelId} level={5} style={{ margin: '0 0 8px' }}>整理标签</Typography.Title>
        <Typography.Paragraph id={helpId} type="secondary" style={{ marginBottom: 0 }}>
          用于当前整理，不按章节回溯，与别名、基础设定和剧情变化分别保存。最多 50 个标签，每个最多 50 字。
        </Typography.Paragraph>
      </div>
      {state.notice && <Alert type="info" showIcon message={state.notice} />}
      {state.error && <Alert type="error" showIcon message={state.error} action={<Button size="small" onClick={() => void controller.refresh()} loading={state.loading} disabled={busy}>重试读取标签</Button>} />}
      {state.loading && !state.base && <Spin tip="正在读取整理标签"><div style={{ minHeight: 48 }} /></Spin>}
      {state.base && <>
        {!canEdit && <Alert type="info" showIcon message={dirty ? '当前为只读，未保存的标签草稿已保留。' : '当前为只读，可查看整理标签。'} />}
        <div style={{ minWidth: 0, display: 'grid', gap: 8 }}>
          {canEdit ? <label htmlFor={inputId}>本条目的整理标签</label> : <Typography.Text>当前已读的整理标签</Typography.Text>}
          {canEdit ? <Select<string[]>
            id={inputId}
            aria-label="本条目的整理标签"
            aria-describedby={`${helpId}${state.draftError ? ` ${errorId}` : ''}`}
            mode="tags"
            value={state.draft}
            searchValue={state.search}
            showSearch
            filterOption={false}
            options={options}
            onChange={controller.edit}
            onSearch={controller.setSearch}
            onInputKeyDown={preventTagFormSubmit}
            loading={state.suggesting}
            disabled={busy}
            status={state.draftError ? 'error' : undefined}
            placeholder="输入标签后按回车添加，也可选择建议"
            maxTagTextLength={24}
            optionRender={(option) => <span style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{option.label}</span>}
            style={{ width: '100%', minWidth: 0 }}
            notFoundContent={state.suggesting ? '正在加载建议…' : '可输入新标签，按回车添加'}
          /> : <Tags values={(state.latest ?? state.base).setting_tags} />}
          {state.draftError && <Typography.Text id={errorId} type="danger" role="alert">{state.draftError}</Typography.Text>}
          {canEdit && <Typography.Text type="secondary">标签区分大小写；重复标签保留首次出现的位置。保存时也会加入尚未按回车的输入。</Typography.Text>}
        </div>
        {!canEdit && dirty && <div><Typography.Paragraph style={{ marginBottom: 4 }}>已保留的标签草稿</Typography.Paragraph><Tags values={[...state.draft, ...(state.search ? [state.search] : [])]} /></div>}
        {canEdit && state.suggestionError && <Alert type="warning" showIcon message="标签建议暂不可用，仍可输入并保存新标签。" description={state.suggestionError}
          action={<Button size="small" onClick={() => void controller.suggest()} loading={state.suggesting}>重试建议</Button>} />}
        {state.reviewRequired && <Alert type="warning" showIcon message="保存前需要对照最新标签。"
          description={<div style={{ display: 'grid', gap: 12, minWidth: 0 }}>
            <Typography.Text>标签草稿与原保存版本已保留。读取最新标签后，明确采用对照版本，才可继续保存。</Typography.Text>
            <div><Typography.Paragraph style={{ marginBottom: 4 }}>编辑开始时 · 版本 {state.original?.categories_revision}</Typography.Paragraph><Tags values={state.original?.setting_tags ?? []} /></div>
            {state.latest && <div><Typography.Paragraph style={{ marginBottom: 4 }}>最新已读 · 版本 {state.latest.categories_revision}</Typography.Paragraph><Tags values={state.latest.setting_tags} /></div>}
            <div><Typography.Paragraph style={{ marginBottom: 4 }}>我的标签草稿</Typography.Paragraph><Tags values={[...state.draft, ...(state.search ? [state.search] : [])]} /></div>
            <Space wrap size={[8, 12]}>
              <Button onClick={() => void controller.compare()} loading={state.reviewing} disabled={state.saving}>读取最新标签并对照</Button>
              {canEdit && state.reviewed && <Button onClick={controller.acceptComparison} disabled={busy}>保留草稿，采用对照版本</Button>}
            </Space>
          </div>} />}
        {canEdit && <Space wrap size={[8, 16]}>
          <Button type="primary" htmlType="button" loading={state.saving} disabled={state.loading || state.reviewing || state.reviewRequired || !dirty || !!state.draftError} onClick={() => void controller.save()}>保存整理标签</Button>
          <Button htmlType="button" disabled={busy || (!state.draft.length && !state.search)} onClick={() => controller.edit([])}>清空整理标签</Button>
          {dirty && <Typography.Text type="secondary">标签有未保存的修改</Typography.Text>}
        </Space>}
      </>}
    </section>
  )
}
