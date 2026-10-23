import { App, Form, Modal, Select, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { cardApi, cardFilename, downloadJson } from '../../api/cards'
import { errorMessage } from '../../api/client'
import { organizationApi } from '../../api/organizations'
import type { Character } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'

interface Props {
  projectId: number
  projectTitle: string
  open: boolean
  characters: Character[]
  /** 预选的角色（列表勾选） */
  initialCharacterIds: number[]
  onClose: () => void
}

/** 导出角色 / 组织卡片（JSON，按名称关联，不含内部 ID）。 */
export default function CardExportModal(props: Props) {
  // 仅在打开时挂载：每次打开都以当前勾选为初始值并拉取最新组织
  if (!props.open) return null
  return <Inner {...props} />
}

function Inner({ projectId, projectTitle, open, characters, initialCharacterIds, onClose }: Props) {
  const { message } = App.useApp()
  const [charIds, setCharIds] = useState<number[]>(initialCharacterIds)
  const [orgIds, setOrgIds] = useState<number[]>([])
  const [exporting, setExporting] = useState(false)
  const orgLoader = useCallback(() => organizationApi.list(projectId), [projectId])
  const { data: orgs = [] } = useLoader(orgLoader)

  const handleExport = async () => {
    setExporting(true)
    try {
      const data = await cardApi.export(projectId, { character_ids: charIds, organization_ids: orgIds })
      downloadJson(data, cardFilename(projectTitle))
      message.success(`已导出 ${charIds.length} 个角色、${orgIds.length} 个组织`)
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setExporting(false)
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
      okButtonProps={{ disabled: charIds.length === 0 && orgIds.length === 0 }}
    >
      <Form layout="vertical">
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
            onChange={setOrgIds}
            options={orgs.map((o) => ({ value: o.id, label: o.name }))}
            maxTagCount="responsive"
          />
        </Form.Item>
      </Form>
      <Typography.Text type="secondary">
        只导出两端都在所选范围内的人物关系，以及成员角色在所选范围内的组织成员关系。
      </Typography.Text>
    </Modal>
  )
}
