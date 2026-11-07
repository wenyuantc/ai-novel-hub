import type { ModalFuncProps } from 'antd'
import { useEffect, useRef } from 'react'

interface ConfirmationHandle { destroy: () => void }
interface ConfirmationApi { confirm: (options: ModalFuncProps) => ConfirmationHandle }

/** App级modal holder不会随页面卸载；本页只销毁自己创建的确认框。 */
export function useSettingsConfirmation(modal: ConfirmationApi, denied: boolean, scopeKey?: string | number) {
  const owned = useRef<ConfirmationHandle | null>(null)
  useEffect(() => {
    if (denied) { owned.current?.destroy(); owned.current = null }
    return () => { owned.current?.destroy(); owned.current = null }
  }, [denied, scopeKey])

  return (options: ModalFuncProps) => {
    if (denied) return
    owned.current?.destroy()
    const handle = modal.confirm({
      ...options,
      afterClose: () => {
        if (owned.current === handle) owned.current = null
        options.afterClose?.()
      },
    })
    owned.current = handle
  }
}
