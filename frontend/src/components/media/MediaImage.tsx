import { useState } from 'react'
import { buildUrl } from '../../api/client'

interface Props { objectId: string; alt: string; variant?: 'thumb' | 'poster'; className?: string; loading?: 'lazy' | 'eager' }

function ImageContent({ src, alt, className, loading }: { src: string; alt: string; className?: string; loading?: 'lazy' | 'eager' }) {
  const [failed, setFailed] = useState(false)
  return failed ? <span role="status">{alt && alt + '：'}文件暂不可用</span>
    : <img src={src} alt={alt} loading={loading} className={className} style={{ maxWidth: '100%', height: 'auto' }} onError={() => setFailed(true)} />
}

export default function MediaImage({ objectId, variant, ...props }: Props) {
  if (!objectId) return <span role="status">文件暂不可用</span>
  const src = buildUrl('/media/' + encodeURIComponent(objectId), { variant })
  return <ImageContent key={src} src={src} {...props} />
}
