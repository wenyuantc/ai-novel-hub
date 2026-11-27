import { Alert, Form, Input, Modal, Radio, Select } from 'antd'
import { useCallback, useState } from 'react'
import { chapterApi } from '../../../api/chapters'
import { errorMessage } from '../../../api/client'
import { outlineProApi } from '../../../api/outlines'
import { useLoader } from '../../../hooks/useLoader'
import { dramaApi } from '../api'
import { chapterSelectionError, orderedChapterIds } from '../chapters'
import { LoadError } from './PageScope'

export default function NewEpisode({
  pid,
  sid,
  close,
  created,
  current,
}: {
  pid: number
  sid: number
  close: () => void
  created: (id: number) => void
  current: () => boolean
}) {
  const { data, error, reload } = useLoader(
    useCallback(() => Promise.all([chapterApi.list(pid), outlineProApi.tree(pid)]), [pid]),
  )
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [selected, setSelected] = useState<number[]>([])
  const [mode, setMode] = useState<'chapter' | 'manual'>('chapter')
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState('')
  const chapters = data?.[0] ?? []
  const volumes = new Map<number, string>()
  const planVolumes = new Map<number, string>()
  for (const outline of data?.[1] ?? []) {
    const name = outline.volume_title || '第 ' + outline.volume_no + ' 卷'
    volumes.set(outline.id, name)
    for (const plan of outline.plans) planVolumes.set(plan.id, name)
  }
  const options = new Map<string, { value: number; label: string }[]>()
  for (const chapter of chapters) {
    const group = volumes.get(chapter.outline_id ?? -1) ?? planVolumes.get(chapter.plan_id ?? -1) ?? '未分卷章节'
    if (!options.has(group)) options.set(group, [])
    options
      .get(group)!
      .push({
        value: chapter.id,
        label: '第 ' + chapter.chapter_no + ' 章 ' + chapter.title + '（' + chapter.word_count + ' 字）',
      })
  }
  const invalid = mode === 'chapter' ? chapterSelectionError(chapters, selected) : null
  const save = async () => {
    if (!current() || saving || invalid || !title.trim()) return
    setSaving(true)
    try {
      const episode = await dramaApi.createEpisode(sid, {
        title: title.trim(),
        ...(mode === 'chapter' ? { chapter_ids: orderedChapterIds(chapters, selected) } : { source_text: text }),
      })
      if (current()) created(episode.id)
    } catch (e) {
      if (current()) setFailure(errorMessage(e))
    } finally {
      setSaving(false)
    }
  }
  return (
    <Modal
      open
      title="新建剧集"
      onCancel={close}
      onOk={save}
      okText="建集"
      confirmLoading={saving}
      okButtonProps={{
        disabled: !title.trim() || Boolean(invalid) || (mode === 'chapter' ? !selected.length || !data : !text.trim()),
      }}
    >
      <Form layout="vertical">
        <Form.Item label="集名" htmlFor="drama-episode-title">
          <Input
            id="drama-episode-title"
            maxLength={200}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            autoFocus
          />
        </Form.Item>
        <Form.Item label="原始内容来源">
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            options={[
              { label: '从章节选取', value: 'chapter' },
              { label: '手动输入', value: 'manual' },
            ]}
          />
        </Form.Item>
        {mode === 'chapter' ? (
          <Form.Item
            label="选取连续章节"
            validateStatus={invalid ? 'error' : undefined}
            help={invalid || '建集时保存正文快照，之后可在工作台修改。'}
          >
            {error ? (
              <LoadError error={error} reload={reload} />
            ) : (
              <Select
                aria-label="选取连续章节"
                mode="multiple"
                showSearch
                optionFilterProp="label"
                loading={!data}
                options={[...options].map(([label, options]) => ({ label, options }))}
                value={selected}
                onChange={setSelected}
              />
            )}
          </Form.Item>
        ) : (
          <Form.Item label="原始内容" htmlFor="drama-source">
            <Input.TextArea
              id="drama-source"
              rows={6}
              maxLength={60000}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </Form.Item>
        )}
      </Form>
      {failure && <Alert type="error" message={failure} />}
    </Modal>
  )
}
