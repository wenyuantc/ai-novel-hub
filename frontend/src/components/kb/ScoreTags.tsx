import { Space, Tag, Tooltip } from 'antd'
import { formatScore, scoreColor } from '../../utils/kb'

interface Props {
  score: number
  vectorScore: number | null
  keywordScore: number
}

/** 检索命中的三项分数：总分（着色）、向量分（只用关键词时为「—」）、关键词分。 */
export default function ScoreTags({ score, vectorScore, keywordScore }: Props) {
  return (
    <Space size={4} wrap>
      <Tooltip title="最终分（0–1），低于最低分数的不返回">
        <Tag color={scoreColor(score)} style={{ marginInlineEnd: 0 }}>
          总分 {formatScore(score)}
        </Tag>
      </Tooltip>
      <Tooltip title={vectorScore === null ? '本次只用关键词检索' : '向量相似度，(cos + 1) / 2'}>
        <Tag bordered={false} style={{ marginInlineEnd: 0 }}>
          向量 {formatScore(vectorScore)}
        </Tag>
      </Tooltip>
      <Tooltip title="BM25 关键词分，按本次最高分归一化">
        <Tag bordered={false} style={{ marginInlineEnd: 0 }}>
          关键词 {formatScore(keywordScore)}
        </Tag>
      </Tooltip>
    </Space>
  )
}
