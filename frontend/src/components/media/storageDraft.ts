import type { MediaLocationInput } from '../../api/types'

/** 空凭据从请求中省略，本地模式只传 kind。 */
export function locationInput(values: MediaLocationInput): MediaLocationInput {
  if (values.kind === 'local') return { kind: 'local' }
  return {
    kind: 's3', endpoint: values.endpoint?.trim() ?? '', region: values.region?.trim() || 'us-east-1',
    bucket: values.bucket?.trim() ?? '', prefix: values.prefix?.trim() ?? '', path_style: values.path_style ?? true,
    ...(values.access_key ? { access_key: values.access_key } : {}),
    ...(values.secret_key ? { secret_key: values.secret_key } : {}),
  }
}
