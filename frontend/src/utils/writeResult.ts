import { ApiError } from '../api/client'

/** 已发送的写请求遇到这些错误时，不能推断服务端事务是否提交。 */
export function writeResultUnknown(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status < 400 || error.status === 408 || error.status >= 500
}
