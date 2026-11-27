import { useState } from 'react'
import { buildUrl } from '../../api/client'

interface Props { objectId: string; label?: string; className?: string }

function VideoContent({ objectId, label, className }: Props) {
  const [failed, setFailed] = useState(false)
  if (failed) return <span role="status">文件暂不可用</span>
  const path = '/media/' + encodeURIComponent(objectId)
  return <video aria-label={label} className={className} style={{ maxWidth: '100%' }}
    src={buildUrl(path)} poster={buildUrl(path, { variant: 'poster' })}
    controls playsInline preload="metadata" {...{ loading: 'lazy' }} onError={() => setFailed(true)} />
}

export default function MediaVideo(props: Props) {
  if (!props.objectId) return <span role="status">文件暂不可用</span>
  return <VideoContent key={props.objectId} {...props} />
}
