import type { CSSProperties } from 'react'
import { useResolvedTheme } from '../../theme/themeStore'
import { coverColor } from '../../utils/shelf'

interface Props {
  title: string
  genre?: string
  /** 封面图（封面任务接入后传入）：有图时直接显示图片 */
  coverUrl?: string | null
  onClick?: () => void
}

/** 书本式文字封面（2:3）：按书名哈希取色、左侧加深书脊，书名最多 3 行，题材在底部。 */
export default function BookCover({ title, genre, coverUrl, onClick }: Props) {
  const resolved = useResolvedTheme()
  const c = coverColor(title, resolved)
  const frame: CSSProperties = {
    position: 'relative',
    aspectRatio: '2 / 3',
    width: '100%',
    borderRadius: '2px 6px 6px 2px',
    overflow: 'hidden',
    cursor: onClick ? 'pointer' : undefined,
    background: c.bg,
    color: c.text,
    boxShadow: `inset 0 0 0 1px ${c.spine}`,
  }
  if (coverUrl) {
    return (
      <div style={frame} onClick={onClick} role="img" aria-label={`《${title}》封面`}>
        <img src={coverUrl} alt={title} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
      </div>
    )
  }
  return (
    <div style={frame} onClick={onClick} role="img" aria-label={`《${title}》封面`}>
      <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: '8%', background: c.spine }} />
      <div style={{ position: 'absolute', left: '8%', top: 0, bottom: 0, width: 2, background: c.text, opacity: 0.25 }} />
      <div
        style={{
          position: 'absolute',
          inset: '14% 10% 18% 18%',
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'center',
        }}
      >
        <div
          style={{
            fontFamily: "'Songti SC', 'STSong', 'SimSun', 'Noto Serif SC', serif",
            fontWeight: 700,
            fontSize: 'clamp(14px, 1.6vw + 6px, 22px)',
            lineHeight: 1.35,
            textAlign: 'center',
            wordBreak: 'break-all',
            display: '-webkit-box',
            WebkitLineClamp: 3,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {title}
        </div>
      </div>
      {genre && (
        <div
          style={{
            position: 'absolute',
            left: '18%',
            right: '10%',
            bottom: '7%',
            fontSize: 12,
            textAlign: 'center',
            opacity: 0.85,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {genre}
        </div>
      )}
    </div>
  )
}
