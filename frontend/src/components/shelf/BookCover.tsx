import type { CSSProperties } from 'react'
import { useState } from 'react'
import { useResolvedTheme } from '../../theme/themeStore'
import { coverColor, hashString } from '../../utils/shelf'
import './BookCover.css'

interface Props {
  title: string
  genre?: string
  author?: string
  coverUrl?: string | null
  onClick?: () => void
  /** 可选横版装帧；默认 2:3 竖版供书库与概览复用。 */
  compact?: boolean
}

/** 真实封面完整展示；无图或图片失效时使用书名文字装帧，不引入示例作品素材。 */
export default function BookCover({ title, genre, author, coverUrl, onClick, compact = false }: Props) {
  const resolved = useResolvedTheme()
  const c = coverColor(title, resolved)
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const hasImage = !!coverUrl && failedUrl !== coverUrl
  const variant = ['sage', 'night', 'sand'][hashString(title.trim()) % 3]
  const style: CSSProperties | undefined = compact ? undefined : { background: c.bg, color: c.text, boxShadow: `inset 0 0 0 1px ${c.spine}` }
  const content = <>
    {hasImage ? <img src={coverUrl} alt={`《${title}》封面`} loading="lazy" onError={() => setFailedUrl(coverUrl)} /> : <>
      {compact && <div className="book-cover-ornament" aria-hidden="true"><span /><i /></div>}
      <div className="book-cover-caption"><span className="book-cover-title">{title}</span>{(genre || author) && <span className="book-cover-meta">{[genre, author ? `${author} 著` : null].filter(Boolean).join(' · ')}</span>}</div>
    </>}
  </>
  const className = `book-cover ${compact ? `book-cover-compact book-cover-${variant}` : 'book-cover-portrait'}${hasImage ? ' book-cover-image' : ''}`
  return onClick ? <button type="button" className={className} style={style} onClick={onClick} aria-label={`打开《${title}》`}>{content}</button> : <div className={className} style={style}>{content}</div>
}
