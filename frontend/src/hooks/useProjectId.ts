import { useParams } from 'react-router-dom'

/** 当前路由中的项目 id（:pid）。 */
export function useProjectId(): number {
  const { pid } = useParams()
  return Number(pid)
}
