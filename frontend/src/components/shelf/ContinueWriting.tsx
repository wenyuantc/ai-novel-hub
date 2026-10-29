import { ArrowRightOutlined, FormOutlined } from '@ant-design/icons'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { chapterApi } from '../../api/chapters'
import type { Project } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { continueTarget } from '../../utils/shelf'
import { formatRelative } from '../../utils/time'

/** 仅查询被选中作品的章节摘要；章节名读取失败时保留真实书名及继续入口。 */
export default function ContinueWriting({ project }: { project: Project }) {
  const loader = useCallback(() => chapterApi.list(project.id), [project.id])
  const { data: chapters } = useLoader(loader)
  const chapter = chapters?.find((c) => c.id === project.last_edited_chapter_id)
  return (
    <section className="library-continue" aria-label="继续最近作品">
      <div className="library-continue-story"><FormOutlined aria-hidden /><div><p title={project.title}>上次写到《{project.title}》</p><h2>{chapter ? `第${chapter.chapter_no}章 · ${chapter.title}` : project.title}</h2></div></div>
      <div className="library-continue-action"><span>{project.chapter_count} 章 · {formatRelative(project.updated_at)}更新</span><Link to={continueTarget(project)}><ArrowRightOutlined />继续写作</Link></div>
    </section>
  )
}
