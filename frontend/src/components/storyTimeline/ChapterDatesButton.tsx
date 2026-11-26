import { Button, Dropdown } from 'antd'
import { useState } from 'react'
import type { TargetKind } from '../../api/storyTimeline'
import DateDialog from './DateDialog'

export default function ChapterDatesButton({ pid, chapterId, sceneId }: { pid: number; chapterId: number; sceneId?: number }) {
  const [target, setTarget] = useState<TargetKind>()
  const id = target === 'chapter' ? chapterId : sceneId
  const close = () => setTarget(undefined)
  return <>
    <Dropdown trigger={['click']} menu={{ items: [
      { key: 'chapter', label: '章节默认日期', onClick: () => setTarget('chapter') },
      { key: 'scene', label: '当前场景日期', disabled: !sceneId, onClick: () => setTarget('scene') },
    ] }}><Button size="small" aria-haspopup="menu">故事日期</Button></Dropdown>
    {target && id && <DateDialog key={`${target}:${id}`} pid={pid} kind={target} id={id} onClose={close} onSaved={close} />}
  </>
}
