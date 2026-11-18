import { App, Button, Checkbox, Space } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'

interface Props {
  stamp: string
  disabled?: boolean
  onKeepLocal?: () => Promise<boolean>
  onUseRemote?: () => Promise<boolean>
}

/** 确认只对界面上这一对版本有效，执行层仍要核对编辑代次和服务器版本。 */
export default function DraftConflictChoices({ stamp, disabled, onKeepLocal, onUseRemote }: Props) {
  const { message } = App.useApp()
  const [checked, setChecked] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const busy = useRef(false)
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  if (!onKeepLocal && !onUseRemote) return null
  const confirmed = checked === stamp
  const choose = async (action: () => Promise<boolean>) => {
    if (!confirmed || disabled || busy.current) return
    busy.current = true
    setWorking(true)
    try {
      if (!(await action()) && mounted.current) message.warning('未采用所选版本，请核对当前草稿和保存状态。')
    } catch (error) { if (mounted.current) message.error(errorMessage(error)) }
    finally {
      busy.current = false
      if (mounted.current) {
        setWorking(false)
        setChecked(null)
      }
    }
  }
  return <>
    <Checkbox checked={confirmed} disabled={disabled || working} onChange={(event) => setChecked(event.target.checked ? stamp : null)}>
      已核对当前两份版本
    </Checkbox>
    <Space wrap>
      {onKeepLocal && <Button type="primary" disabled={!confirmed || disabled} loading={working} onClick={() => void choose(onKeepLocal)}>保留本地并同步</Button>}
      {onUseRemote && <Button disabled={!confirmed || disabled} loading={working} onClick={() => void choose(onUseRemote)}>采用服务端版本</Button>}
    </Space>
  </>
}
