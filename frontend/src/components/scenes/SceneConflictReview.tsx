import { Alert, Button, Checkbox, Space, Typography } from 'antd'
import { useState } from 'react'
import type { SceneDraftSnapshot, SceneDraftController } from './sceneDraftController'
import TextDiff from '../TextDiff'

interface Props { state: SceneDraftSnapshot; controller: SceneDraftController; disabled?: boolean }
/** 内容冲突只展示服务端新稿；作者显式核对后才接受 revision，草稿保持原样。 */
export default function SceneConflictReview({ state, controller, disabled }: Props) {
  const [checkedStamp, setCheckedStamp] = useState<string | null>(null)
  const stamp = state.conflict ? `${state.conflict.content_revision}:${state.conflict.planning_revision}` : null
  const checked = stamp !== null && checkedStamp === stamp
  if (!state.conflict && !state.externalChange) return null
  return <Alert type="warning" showIcon message="场景已被其他操作修改，本地草稿已保留" description={<Space direction="vertical" style={{ width: '100%' }}>
    {!state.conflict ? <Button onClick={() => void controller.reviewLatest()}>加载最新场景供核对</Button> : <>
      <Typography.Text>最新标题：{state.conflict.title || '未命名'} · 最新摘要：{state.conflict.summary || '无'} · 版本 {state.conflict.content_revision}</Typography.Text>
      <Typography.Text>最新规划：POV {state.conflict.planning.effective.pov_character_name ?? '无'}；标签 {state.conflict.planning.effective.tags.map((tag) => tag.name).join('、') || '无'}；引用 {state.conflict.planning.effective.references.map((ref) => `${ref.kind} #${ref.kind === 'relationship' ? ref.timeline_id : ref.id}`).join('、') || '无'}</Typography.Text>
      <Typography.Text type="secondary">逐项来源：{Object.entries(state.conflict.planning.raw.modes).map(([field, mode]) => `${field} ${mode === 'inherit' ? '继承本章' : '本场景覆盖'}`).join('；')}</Typography.Text>
      <Typography.Text type="secondary">左侧为最新已保存稿，右侧为当前本地输入；核对标题、摘要及正文后再决定是否重试。</Typography.Text>
      <TextDiff a={state.conflict.content} b={state.draft.content} maxHeight={240} />
      <Checkbox checked={checked} onChange={(event) => setCheckedStamp(event.target.checked ? stamp : null)}>已核对最新场景，保留本地输入继续</Checkbox>
      <Button disabled={!checked || disabled} onClick={() => { controller.acknowledgeLatest(); setCheckedStamp(null) }}>使用最新版本号，不覆盖本地输入</Button>
    </>}
  </Space>} />
}
