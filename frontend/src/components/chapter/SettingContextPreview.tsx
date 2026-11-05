import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Collapse, Space, Spin, theme, Typography } from 'antd'
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import { progressionApi } from '../../api/progressions'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { SettingContextPreviewController } from './settingContextPreviewController'

interface Props {
  projectId: number
  chapterId: number
}

/** 章节章初设定的只读预览，与 AI 私有检索预览分开授权。 */
export default function SettingContextPreview({ projectId, chapterId }: Props) {
  const { token } = theme.useToken()
  const { canRead, canReadNow } = useProjectAccess(projectId)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const authenticated = useAuthStore((state) => state.status === 'authenticated')
  const [expanded, setExpanded] = useState(false)
  const controller = useMemo(() => {
    const canReadCurrentIdentity = () => {
      const auth = useAuthStore.getState()
      const currentIdentity = `${auth.status}:${auth.user?.id ?? ''}:${authorizationKey(auth.user)}`
      return currentIdentity === identity && canReadNow()
    }
    return new SettingContextPreviewController(projectId, chapterId, progressionApi.chapterSettingContext, canReadCurrentIdentity)
  }, [projectId, chapterId, identity, canReadNow])
  const { loading, data, error } = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const visible = canRead && authenticated

  useEffect(() => {
    controller.activate()
    if (visible && expanded) void controller.load()
    return () => controller.deactivate()
  }, [controller, expanded, visible])

  if (!visible) return null

  return (
    <Collapse
      size="small"
      style={{ marginBottom: 8 }}
      activeKey={expanded ? ['setting'] : []}
      onChange={(keys) => setExpanded(keys.includes('setting'))}
      items={[{
        key: 'setting',
        label: '本章有效设定',
        extra: expanded && data ? (
          <Button
            type="text"
            size="small"
            aria-label="刷新本章有效设定"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={(event) => {
              event.stopPropagation()
              void controller.load()
            }}
          />
        ) : undefined,
        children: (
          <Spin spinning={loading} aria-label={loading ? '正在读取本章有效设定' : undefined}>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                这里展示本章章初可注入的设定，不包含完整提示词、参考资料和旧情节；设定或选角变更后请刷新。
              </Typography.Text>
              {error && (
                <Alert
                  type="error"
                  showIcon
                  message={error}
                  action={<Button size="small" onClick={() => void controller.load()}>重试</Button>}
                />
              )}
              {data?.warnings.map((warning, index) => (
                <Alert key={`${index}:${warning}`} type="warning" showIcon message={warning} />
              ))}
              {data && data.legacy_unverified_count > 0 && data.warnings.length === 0 && (
                <Alert type="warning" showIcon message={`本作品有 ${data.legacy_unverified_count} 条旧设定尚未校准，早期设定可能不准确。`} />
              )}
              {data && !data.text.trim() && <Typography.Text type="secondary">本章暂无可用设定。</Typography.Text>}
              {data?.text.trim() && (
                <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12, maxHeight: 240, overflow: 'auto', padding: 8, borderRadius: 4, margin: 0, background: token.colorFillQuaternary }}>
                  {data.text}
                </pre>
              )}
            </Space>
          </Spin>
        ),
      }]}
    />
  )
}
