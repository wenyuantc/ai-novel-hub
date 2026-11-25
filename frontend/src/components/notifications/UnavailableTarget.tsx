import { Alert } from 'antd'
import { useEffect } from 'react'
import { invalidateNotifications } from '../../api/notifications'

export default function UnavailableTarget() {
  useEffect(() => { invalidateNotifications() }, [])
  return <Alert type="info" showIcon message="这条通知对应的内容已不可用。" />
}
