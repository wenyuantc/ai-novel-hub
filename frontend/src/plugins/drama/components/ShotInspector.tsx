import { useRef, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Select, Space } from 'antd'
import { ApiError } from '../../../api/client'
import type { DramaAssets } from '../assetTypes'
import { shotApi, splitPromptReferences, type Shot, type ShotFields } from '../shots'

interface Props {
  shot: Shot
  assets: DramaAssets
  canEditNow: () => boolean
  canEdit: boolean
  onDirty: (dirty: boolean) => void
  onSaved: (shot: Shot) => void
  onPrompt?: () => void
}
interface Edit {
  revision: number
  fields: Partial<ShotFields>
}

export default function ShotInspector({ shot, assets, canEdit, canEditNow, onDirty, onSaved, onPrompt }: Props) {
  const [edit, setEdit] = useState<Edit | null>(null)
  const [saving, setSaving] = useState(false)
  const writing = useRef(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState<Shot | null>(null)
  const values = { ...shot, ...edit?.fields }
  const looks = assets.characters.flatMap((c) => c.looks.map((l) => ({ value: l.id, label: c.name + ' · ' + l.name })))
  const scenes = assets.scenes.map((s) => ({
    value: s.id,
    label: [s.location, s.time_of_day].filter(Boolean).join(' · '),
  }))
  const props = assets.props.map((p) => ({ value: p.id, label: p.name }))
  const names = [
    ...assets.characters.flatMap((c) =>
      c.looks.filter((l) => values.look_ids.includes(l.id)).flatMap((l) => [c.name, l.name, c.name + ' · ' + l.name]),
    ),
    ...assets.scenes.filter((s) => s.id === values.scene_id).map((s) => s.location),
    ...assets.props.filter((p) => values.prop_ids.includes(p.id)).map((p) => p.name),
  ]
  const tokens = splitPromptReferences(values.video_prompt, names)

  function change<K extends keyof ShotFields>(key: K, value: ShotFields[K]) {
    if (!canEditNow() || !canEdit || writing.current) return
    setEdit((previous) => ({
      revision: previous?.revision ?? shot.revision,
      fields: { ...previous?.fields, [key]: value },
    }))
    onDirty(true)
  }

  async function save(overwrite = false) {
    if (!canEditNow() || !canEdit || !edit || writing.current || (conflict && !overwrite)) return
    writing.current = true
    setSaving(true)
    setError('')
    try {
      const updated = await shotApi.update(
        shot.id,
        overwrite && conflict ? conflict.revision : edit.revision,
        edit.fields,
      )
      if (!canEditNow()) return
      setEdit(null)
      setConflict(null)
      onDirty(false)
      onSaved(updated)
    } catch (err) {
      if (!canEditNow()) return
      if (err instanceof ApiError && err.status === 409) {
        try {
          const latest = (await shotApi.list(shot.episode_id)).find((s) => s.id === shot.id)
          if (!canEditNow()) return
          setConflict(latest ?? null)
          setError(latest ? '这条分镜已被修改，你的编辑仍保留。' : '此分镜已被删除。可以复制下方草稿。')
        } catch {
          setError('分镜版本已变化，无法读取最新版本。请重试保存。')
        }
      } else setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      writing.current = false
      setSaving(false)
    }
  }

  return (
    <Form layout="vertical" disabled={!canEdit || saving} onFinish={() => void save()}>
      {error && (
        <Alert
          type="warning"
          showIcon
          message={error}
          action={
            conflict ? (
              <Space wrap>
                <Button
                  size="small"
                  onClick={() => {
                    onSaved(conflict)
                    setConflict(null)
                    setError('')
                    setEdit(null)
                    onDirty(false)
                  }}
                >
                  重新载入
                </Button>
                <Button size="small" onClick={() => void save(true)}>
                  覆盖保存
                </Button>
              </Space>
            ) : (
              <Button
                size="small"
                onClick={() => {
                  setEdit(null)
                  setError('')
                  onDirty(false)
                }}
              >
                放弃草稿
              </Button>
            )
          }
        />
      )}
      <Form.Item label="分镜标题" htmlFor="drama-shot-title">
        <Input
          id="drama-shot-title"
          value={values.title}
          maxLength={200}
          onChange={(e) => change('title', e.target.value)}
          onBlur={() => void save()}
        />
      </Form.Item>
      <Form.Item label="画面描述与台词" htmlFor="drama-shot-description">
        <Input.TextArea
          id="drama-shot-description"
          rows={4}
          value={values.description}
          maxLength={60000}
          onChange={(e) => change('description', e.target.value)}
          onBlur={() => void save()}
        />
      </Form.Item>
      <Space wrap align="start">
        <Form.Item label="景别" htmlFor="drama-shot-size">
          <Input
            id="drama-shot-size"
            value={values.shot_size}
            maxLength={200}
            onChange={(e) => change('shot_size', e.target.value)}
            onBlur={() => void save()}
          />
        </Form.Item>
        <Form.Item label="时长（秒）" htmlFor="drama-shot-duration">
          <InputNumber
            id="drama-shot-duration"
            min={2}
            max={30}
            precision={0}
            value={values.duration_seconds}
            onChange={(n) => n !== null && change('duration_seconds', n)}
            onBlur={() => void save()}
          />
        </Form.Item>
      </Space>
      <Form.Item label="氛围" htmlFor="drama-shot-atmosphere">
        <Input
          id="drama-shot-atmosphere"
          value={values.atmosphere}
          maxLength={60000}
          onChange={(e) => change('atmosphere', e.target.value)}
          onBlur={() => void save()}
        />
      </Form.Item>
      <Form.Item label="场景" htmlFor="drama-shot-scene">
        <Select
          id="drama-shot-scene"
          allowClear
          showSearch
          optionFilterProp="label"
          options={scenes}
          value={values.scene_id ?? undefined}
          onChange={(id) => change('scene_id', id ?? null)}
        />
      </Form.Item>
      <Form.Item label="角色造型" htmlFor="drama-shot-looks">
        <Select
          id="drama-shot-looks"
          mode="multiple"
          optionFilterProp="label"
          options={looks}
          value={values.look_ids}
          onChange={(ids) => change('look_ids', ids)}
        />
      </Form.Item>
      <Form.Item label="道具" htmlFor="drama-shot-props">
        <Select
          id="drama-shot-props"
          mode="multiple"
          optionFilterProp="label"
          options={props}
          value={values.prop_ids}
          onChange={(ids) => change('prop_ids', ids)}
        />
      </Form.Item>
      <Form.Item label="视频提示词" htmlFor="drama-shot-prompt">
        <Input.TextArea
          id="drama-shot-prompt"
          rows={3}
          value={values.video_prompt}
          maxLength={60000}
          onChange={(e) => change('video_prompt', e.target.value)}
          onBlur={() => void save()}
        />
        {tokens.some((t) => t.linked) && (
          <div aria-label="素材引用预览" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {tokens.map((t, i) => (t.linked ? <mark key={i}>{t.value}</mark> : t.value))}
          </div>
        )}
      </Form.Item>
      {canEdit && (
        <Space wrap>
          <Button type="primary" htmlType="submit" disabled={!edit || !!conflict} loading={saving}>
            保存分镜
          </Button>
          {onPrompt && (
            <Button disabled={!!edit} onClick={onPrompt}>
              生成视频提示词
            </Button>
          )}
        </Space>
      )}
    </Form>
  )
}
