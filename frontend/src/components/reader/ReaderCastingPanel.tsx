import { Button, Pagination, Typography } from 'antd'
import { useId, useMemo, useState, type ReactNode } from 'react'
import type { useReaderCasting } from '../../hooks/useReaderCasting'
import type { useReadAloud } from '../../hooks/useReadAloud'
import { READER_EMOTIONS, READER_EMOTION_LABELS, type ReaderEmotion, type ReaderPassage } from '../../utils/readerDialogue'
import { NARRATOR_ROLE, DIALOGUE_ROLE, readerRoleLabel, readerRole, castReaderVoices, roleIdsForScript } from '../../utils/readerVoices'

type Casting = ReturnType<typeof useReaderCasting>
type Player = Pick<ReturnType<typeof useReadAloud>, 'voices' | 'preview' | 'status' | 'message' | 'previewPassage' | 'previewing' | 'stop'>
const pageSize = 8

function CueEditor({ cue, roles, casting, autoLabel, onPreview }: {
  cue: ReaderPassage
  roles: string[]
  casting: Casting
  autoLabel: string
  onPreview?: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const override = casting.overrides[String(cue.start)] ?? {}
  const speakerId = useId(), emotionId = useId(), nameId = useId()
  const setRole = (speaker?: string) => {
    casting.setOverride(cue.start, { ...override, speaker })
    setAdding(false)
    setName('')
  }
  return <li className="reader-casting-cue">
    <p>{cue.text}</p>
    <div className="reader-casting-field">
      <label htmlFor={speakerId}>说话人</label>
      <select id={speakerId} value={adding ? 'new' : override.speaker ? `role:${override.speaker}` : 'auto'}
        onChange={(event) => {
          const value = event.target.value
          if (value === 'new') setAdding(true)
          else setRole(value === 'auto' ? undefined : value.slice(5))
        }}>
        <option value="auto">{autoLabel} · {readerRoleLabel(readerRole(cue))}</option>
        {roles.map((role) => <option key={role} value={`role:${role}`}>{readerRoleLabel(role)}</option>)}
        <option value="new">新增角色…</option>
      </select>
    </div>
    {adding && <form className="reader-casting-field" onSubmit={(e) => { e.preventDefault(); if (name.trim()) setRole(name.trim()) }}>
      <label htmlFor={nameId}>角色名</label>
      <input id={nameId} autoFocus value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
      <Button htmlType="submit" disabled={!name.trim()}>使用</Button>
    </form>}
    <div className="reader-casting-field">
      <label htmlFor={emotionId}>情绪</label>
      <select id={emotionId} value={override.emotion ?? 'auto'}
        onChange={(e) => casting.setOverride(cue.start, { ...override, emotion: e.target.value === 'auto' ? undefined : e.target.value as ReaderEmotion })}>
        <option value="auto">{autoLabel} · {READER_EMOTION_LABELS[cue.emotion]}</option>
        {READER_EMOTIONS.map((emotion) => <option key={emotion} value={emotion}>{READER_EMOTION_LABELS[emotion]}</option>)}
      </select>
      <Button disabled={!onPreview} onClick={onPreview}>试听这句</Button>
    </div>
  </li>
}

export default function ReaderCastingPanel({ detected, script, casting, player, children }: {
  detected: ReaderPassage[]
  script: ReaderPassage[]
  casting: Casting
  player: Player
  children?: ReactNode
}) {
  const [page, setPage] = useState(1)
  const cues = useMemo(() => detected.filter((p) => p.kind === 'dialogue'), [detected])
  const roles = useMemo(() => roleIdsForScript(script), [script])
  const assigned = useMemo(() => castReaderVoices(player.voices, roles, casting.effectiveVoices), [player.voices, roles, casting.effectiveVoices])
  const aiRoles = useMemo(() => new Map(casting.aiResult?.voices.map((v) => [v.speaker, v.voice_id])), [casting.aiResult])
  const aiCues = useMemo(() => new Set(casting.aiResult?.cues.map((cue) => cue.start)), [casting.aiResult])
  const voicesById = useMemo(() => new Map(player.voices.map((voice) => [voice.id, voice])), [player.voices])
  const voiceFor = (role: string) => {
    const id = assigned.get(role)?.voiceId
    return id === undefined ? undefined : voicesById.get(id)
  }
  const choices = useMemo(() => [...new Set([NARRATOR_ROLE, DIALOGUE_ROLE, ...roles, ...roleIdsForScript(detected), ...Object.keys(casting.voices)])],
    [roles, detected, casting.voices])
  const currentPage = Math.min(page, Math.max(1, Math.ceil(cues.length / pageSize)))
  return <div className="reader-casting-panel">
    {children}
    <h3>角色声音</h3>
    {!player.voices.length && <Typography.Paragraph type="secondary">当前没有可用的本机中文声音。请检查浏览器是否支持朗读，并在系统语音设置中安装中文声音。</Typography.Paragraph>}
    {roles.map((role) => {
      const saved = casting.voices[role]
      const available = voicesById.has(saved)
      const suggestedId = aiRoles.get(role)
      const voice = voiceFor(role)
      const label = readerRoleLabel(role)
      return <div key={role} className="reader-casting-voice">
        <label>
          <span>{label}</span>
          <select aria-label={`${label}的声音`} value={saved ?? ''} onChange={(e) => casting.setVoice(role, e.target.value)}>
            <option value="">{suggestedId ? `AI 推荐 · ${voicesById.get(suggestedId)?.name ?? '声音暂不可用'}` : `自动${!saved && voice ? ` · ${voice.name}` : ''}`}</option>
            {saved && !available && <option value={saved}>已选声音暂不可用</option>}
            {player.voices.map((option) => <option key={option.id} value={option.id}>{option.name}（{option.lang}）</option>)}
          </select>
        </label>
        <Button aria-label={`试听${label}`} disabled={!voice || player.status !== 'idle'} onClick={() => voice && player.preview(voice.id, 'neutral', role)}>试听</Button>
      </div>
    })}
    {player.previewing && <Button onClick={player.stop}>停止试听</Button>}
    {player.message && <p role="status">{player.message}</p>}
    <h3>本章台词</h3>
    {!!cues.length && player.status !== 'idle' && <Typography.Paragraph type="secondary">修改台词分配或情绪会停止本次朗读。</Typography.Paragraph>}
    {cues.length ? <>
      <ol className="reader-casting-cues" start={(currentPage - 1) * pageSize + 1}>
        {cues.slice((currentPage - 1) * pageSize, currentPage * pageSize).map((cue) => {
          const role = casting.overrides[String(cue.start)]?.speaker ?? readerRole(cue)
          const canPreview = player.status === 'idle' && !!voiceFor(role)
          return <CueEditor key={cue.start} cue={cue} roles={choices} casting={casting} autoLabel={aiCues.has(cue.start) ? 'AI 识别' : '自动'}
            onPreview={canPreview ? () => player.previewPassage(cue.start) : undefined} />
        })}
      </ol>
      {cues.length > pageSize && <Pagination simple size="small" current={currentPage} pageSize={pageSize}
        total={cues.length} showSizeChanger={false} onChange={setPage} />}
    </> : <Typography.Text type="secondary">本章未识别到引号台词，正文由旁白朗读。</Typography.Text>}
  </div>
}
