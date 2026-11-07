import { Alert, Button, Collapse, Space, Spin, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { sceneApi } from '../../api/scenes'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'

interface Props { projectId: number; chapterId: number; sceneId: number }
/** 真实场景开始时的受管理设定预览，不将章末当前档案冒充场景历史。 */
export default function SceneSettingPreview({ projectId, chapterId, sceneId }: Props) {
  const [open, setOpen] = useState(false)
  const { canRead } = useProjectAccess(projectId)
  const load = useCallback((signal: AbortSignal) => sceneApi.settingContext(projectId, chapterId, sceneId, signal), [projectId, chapterId, sceneId])
  const result = useMentionQuery(projectId, `scene-setting:${chapterId}:${sceneId}`, load, open && canRead)
  return <Collapse size="small" onChange={(keys) => setOpen(keys.length > 0)} items={[{ key: 'settings', label: '本场景开始时的有效设定', children: <Space direction="vertical" style={{ width: '100%' }}>
    {result.loading && <Spin />}
    {result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
    {result.data?.warnings.map((warning) => <Alert type="warning" key={warning} message={warning} />)}
    {result.data && <><Typography.Text type="secondary">场景开始前 · 旧资料待校准 {result.data.legacy_unverified_count} 项</Typography.Text><pre style={{ whiteSpace: 'pre-wrap', maxHeight: 280, overflow: 'auto' }}>{result.data.text || '暂无可用设定'}</pre></>}
  </Space> }]} />
}
