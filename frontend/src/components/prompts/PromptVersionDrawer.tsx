import { App, Button, Drawer, Space, Table, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { promptApi } from '../../api/prompts'
import type { PromptTemplateDetail, PromptVersion } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'
import PromptDiffModal from './PromptDiffModal'

interface Props {
  templateKey: string
  open: boolean
  /** 编辑器当前内容（对比用） */
  current: { system: string; user: string }
  /** 编辑器有未保存修改：回滚前提示会被替换 */
  dirty: boolean
  /** 覆盖的保存时间：变化后重新拉取历史 */
  version: string | null
  onClose: () => void
  onRestored: (detail: PromptTemplateDetail) => void
}

/** 覆盖的历史版本（最近 20 个）：查看与当前内容的差异、回滚（以该版本重新保存，会重新校验）。 */
export default function PromptVersionDrawer(props: Props) {
  return (
    <Drawer title="历史版本" width={720} open={props.open} onClose={props.onClose} destroyOnHidden>
      {/* key = 覆盖保存时间：保存 / 回滚后重建并重新拉取历史 */}
      {props.open && <Inner key={props.version ?? 'none'} {...props} />}
    </Drawer>
  )
}

function Inner({ templateKey, current, dirty, onRestored }: Props) {
  const { message, modal } = App.useApp()
  const loader = useCallback(() => promptApi.versions(templateKey), [templateKey])
  const { data: versions = [], loading, error } = useLoader(loader)
  const [diffing, setDiffing] = useState<PromptVersion | null>(null)

  const handleRestore = (v: PromptVersion) => {
    modal.confirm({
      title: `回滚到 ${formatTime(v.created_at)} 的版本？`,
      content: dirty
        ? '编辑器里未保存的修改会被替换。回滚会以该版本内容重新保存（会重新校验）。'
        : '回滚会以该版本内容重新保存（会重新校验），当前内容仍保留在历史中。',
      okText: '回滚',
      cancelText: '取消',
      onOk: async () => {
        try {
          const d = await promptApi.restore(templateKey, v.id)
          message.success('已回滚')
          onRestored(d)
        } catch (err) {
          message.error(errorMessage(err))
        }
      },
    })
  }

  return (
    <>
      {error && <Typography.Text type="danger">{error}</Typography.Text>}
      <Table<PromptVersion>
        size="small"
        rowKey="id"
        loading={loading}
        dataSource={versions}
        pagination={false}
        columns={[
          {
            title: '时间',
            width: 160,
            render: (_, v, i) => (
              <Space>
                {formatTime(v.created_at)}
                {i === 0 && <Typography.Text type="success">当前</Typography.Text>}
              </Space>
            ),
          },
          { title: '备注', dataIndex: 'note', render: (n: string) => n || <Typography.Text type="secondary">-</Typography.Text> },
          {
            title: '操作',
            width: 170,
            render: (_, v) => (
              <Space>
                <Button size="small" onClick={() => setDiffing(v)}>
                  对比当前
                </Button>
                <Button size="small" onClick={() => handleRestore(v)}>
                  回滚
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <PromptDiffModal
        open={diffing !== null}
        title="历史版本 → 编辑器当前内容"
        hint="删除（红）为历史版本中有、当前没有的内容；新增（绿）为当前新加的内容。"
        pairs={
          diffing
            ? [
                { label: '系统段', a: diffing.system, b: current.system },
                { label: '用户段', a: diffing.user, b: current.user },
              ]
            : []
        }
        onClose={() => setDiffing(null)}
      />
    </>
  )
}
