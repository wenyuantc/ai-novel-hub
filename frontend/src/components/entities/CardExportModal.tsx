import { Alert, App, Button, Form, Modal, Select, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { cardApi, cardFilename, downloadJson } from '../../api/cards'
import { errorMessage } from '../../api/client'
import { organizationApi } from '../../api/organizations'
import type { Character } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useAuthorizationScope } from '../../hooks/useAuthorizationScope'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'

interface Props {
  projectId: number
  projectTitle: string
  open: boolean
  characters: Character[]
  /** 预选的角色（列表勾选） */
  initialCharacterIds: number[]
  onClose: () => void
}

/** 导出当前快照，类型化字段使用包内身份及明确的外部依赖。 */
export default function CardExportModal(props: Props) {
  const user = useAuthStore((state) => state.user)
  const { canRead } = useProjectAccess(props.projectId)
  // 仅在打开时挂载：每次打开都以当前勾选为初始值并拉取最新组织
  if (!props.open) return null
  if (!user || !canRead) return null
  return <Inner key={`${props.projectId}:${user.id}:${authorizationKey(user)}`} {...props} />
}

function Inner({ projectId, projectTitle, open, characters, initialCharacterIds, onClose }: Props) {
  const { message } = App.useApp()
  const { canReadNow } = useProjectAccess(projectId)
  const scope = useAuthorizationScope()
  const [charIds, setCharIds] = useState<number[]>(initialCharacterIds)
  const [orgIds, setOrgIds] = useState<number[]>([])
  const [exporting, setExporting] = useState(false)
  const busy = useRef(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])
  const orgLoader = useCallback(async () => {
    if (!canReadNow()) throw new Error('作品访问权限已变更')
    const items = await organizationApi.list(projectId)
    if (!canReadNow()) throw new Error('作品访问权限已变更')
    return items
  }, [projectId, canReadNow])
  const { data: orgs = [], loading, error, reload } = useLoader(orgLoader)

  const handleExport = async () => {
    if (busy.current || !canReadNow() || !scope.allowed()) return
    busy.current = true
    const ticket = scope.begin()
    const controller = new AbortController()
    request.current = controller
    setExporting(true)
    try {
      const data = await cardApi.export(projectId, { character_ids: charIds, organization_ids: orgIds }, controller.signal)
      if (!scope.current(ticket) || !canReadNow()) return
      downloadJson(data, cardFilename(projectTitle))
      message.success(`已导出 ${charIds.length} 个角色、${orgIds.length} 个组织`)
      onClose()
    } catch (err) {
      if (scope.current(ticket) && canReadNow()) message.error(errorMessage(err))
    } finally {
      busy.current = false
      if (scope.current(ticket) && canReadNow()) setExporting(false)
    }
  }

  return (
    <Modal
      title="导出卡片"
      open={open}
      onCancel={onClose}
      onOk={() => void handleExport()}
      okText="导出"
      cancelText="取消"
      confirmLoading={exporting}
      closable={!exporting}
      keyboard={!exporting}
      cancelButtonProps={{ disabled: exporting }}
      okButtonProps={{ disabled: charIds.length === 0 && orgIds.length === 0 }}
    >
      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={reload}>重试</Button>} style={{ marginBottom: 16 }} />}
      <Form layout="vertical" disabled={exporting}>
        <Form.Item label={`角色（${charIds.length}）`}>
          <Select
            mode="multiple"
            allowClear
            optionFilterProp="label"
            value={charIds}
            onChange={setCharIds}
            options={characters.map((c) => ({ value: c.id, label: c.name }))}
            maxTagCount="responsive"
          />
        </Form.Item>
        <Form.Item label={`组织（${orgIds.length}）`}>
          <Select
            mode="multiple"
            allowClear
            optionFilterProp="label"
            value={orgIds}
            loading={loading}
            disabled={exporting || loading}
            onChange={setOrgIds}
            options={orgs.map((o) => ({ value: o.id, label: o.name }))}
            maxTagCount="responsive"
          />
        </Form.Item>
      </Form>
      <Typography.Text type="secondary">
        只导出两端都在所选范围内的人物关系，以及成员角色在所选范围内的组织成员关系。
        角色、组织、相关职业及其字段导出当前有效快照（包含全部有效的计划章变化），不包含源章节和变化历史。
        v5 同时携带字段定义、选项与当前整理标签，分类规则不随卡片导出。引用只关联目标身份，不递归复制档案；包外目标导入时需要明确映射。
        世界观没有卡片容器，完整历史请使用作品备份。
      </Typography.Text>
    </Modal>
  )
}
