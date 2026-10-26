import { UploadOutlined } from '@ant-design/icons'
import { App, Button, Upload } from 'antd'
import type { ReactNode } from 'react'
import { parseJsonFileText } from '../utils/jsonFile'

interface Props {
  /** 紧凑序列化后的字节上限（与后端一致） */
  maxBytes: number
  /** 超限提示文案（与后端 400 一致） */
  tooLarge: string
  onData: (data: unknown) => void | Promise<void>
  loading?: boolean
  disabled?: boolean
  children?: ReactNode
}

/** 选择 JSON 文件导入：本地先查 JSON 语法与大小，结构交给后端校验。 */
export default function JsonImportButton({ maxBytes, tooLarge, onData, loading, disabled, children = '导入' }: Props) {
  const { message } = App.useApp()
  const handle = async (file: File) => {
    const parsed = parseJsonFileText(await file.text(), maxBytes, tooLarge)
    if (parsed.error) {
      message.error(parsed.error)
      return
    }
    await onData(parsed.data)
  }
  return (
    <Upload
      accept=".json,application/json"
      showUploadList={false}
      disabled={disabled || loading}
      beforeUpload={(file) => {
        void handle(file)
        return false
      }}
    >
      <Button icon={<UploadOutlined />} loading={loading} disabled={disabled}>
        {children}
      </Button>
    </Upload>
  )
}
