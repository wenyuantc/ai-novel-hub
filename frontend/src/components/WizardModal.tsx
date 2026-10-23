import { App, Modal } from 'antd'
import { useState } from 'react'
import type { WizardItemMap, WizardKind } from '../api/types'
import { WIZARD_KIND_LABELS } from '../api/wizard'
import WizardGenerator from './WizardGenerator'

interface Props<K extends WizardKind> {
  projectId: number
  kind: K
  open: boolean
  existingCount: number
  onClose: () => void
  onDone: (items: WizardItemMap[K][]) => void
}

/** 面板内的「AI 重新生成」弹窗，复用向导任务组件；重新打开时自动接上进行中的同类任务。 */
export default function WizardModal<K extends WizardKind>({
  projectId,
  kind,
  open,
  existingCount,
  onClose,
  onDone,
}: Props<K>) {
  const { message } = App.useApp()
  const [running, setRunning] = useState(false)
  return (
    <Modal
      title={`AI 生成${WIZARD_KIND_LABELS[kind]}`}
      open={open}
      footer={null}
      width={760}
      maskClosable={false}
      destroyOnHidden
      onCancel={() => {
        if (running) {
          message.warning('正在生成，请等待完成或先停止')
          return
        }
        onClose()
      }}
    >
      <WizardGenerator
        projectId={projectId}
        kind={kind}
        existingCount={existingCount}
        onRunningChange={setRunning}
        onDone={(items) => {
          onDone(items)
          onClose()
        }}
      />
    </Modal>
  )
}
