import { Alert, Button, Drawer, Empty, Flex, List, Popconfirm, Space, Spin, Tag } from 'antd'
import { useCallback, useState } from 'react'
import { buildUrl, errorMessage } from '../../../api/client'
import MediaImage from '../../../components/media/MediaImage'
import { useLoader } from '../../../hooks/useLoader'
import { dramaAssetApi } from '../assetApi'
import type { LibraryLook, LookImage } from '../assetTypes'
import type { LookImageAction } from './TaskPanel'

function Preview({ objectId, label }: { objectId: string; label: string }) {
  return (
    <a
      href={buildUrl('/media/' + encodeURIComponent(objectId))}
      target="_blank"
      rel="noreferrer"
      aria-label={'查看原图：' + label}
    >
      <MediaImage objectId={objectId} variant="thumb" alt={label} loading="lazy" />
    </a>
  )
}

export function PictureHistory({
  lookId,
  label,
  canEdit,
  canEditNow,
  onChange,
}: {
  lookId: number
  label: string
  canEdit: boolean
  canEditNow: () => boolean
  onChange: () => void
}) {
  const { data, error, reload } = useLoader(useCallback(() => dramaAssetApi.lookImages(lookId), [lookId]))
  const [pending, setPending] = useState(false)
  const [failure, setFailure] = useState('')
  const update = async (row: LookImage, remove: boolean) => {
    if (pending || !canEditNow()) return
    setPending(true)
    setFailure('')
    try {
      if (remove) await dramaAssetApi.deleteImage(row.id)
      else await dramaAssetApi.selectImage(row.id)
    } catch (e) {
      if (canEditNow()) setFailure(errorMessage(e))
    } finally {
      if (canEditNow()) {
        setPending(false)
        reload()
        onChange()
      }
    }
  }
  return (
    <>
      {(failure || error) && (
        <Alert showIcon type="error" message={failure || error} action={<Button onClick={reload}>刷新</Button>} />
      )}
      {!data ? (
        !error && <Spin aria-label="读取图片历史" />
      ) : (
        <List
          dataSource={data}
          locale={{ emptyText: <Empty description="还没有生成记录" /> }}
          renderItem={(image, index) => (
            <List.Item key={image.id}>
              <Flex vertical gap={8} style={{ width: '100%' }}>
                <Space wrap>
                  <strong>第 {data.length - index} 张</strong>
                  {image.is_selected && <Tag color="green">当前选用</Tag>}
                  {image.status === 'pending' && <Tag color="processing">生成中</Tag>}
                </Space>
                {image.status === 'ready' && image.media_object_id && (
                  <Preview objectId={image.media_object_id} label={label + ' · 第 ' + (data.length - index) + ' 张'} />
                )}
                {image.status === 'failed' && (
                  <Alert showIcon type="error" message={image.error_message || '生成失败'} />
                )}
                {image.prompt && (
                  <details>
                    <summary>本次生成提示词</summary>
                    <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{image.prompt}</p>
                  </details>
                )}
                {canEdit && image.status !== 'pending' && (
                  <Space>
                    {image.status === 'ready' && !image.is_selected && (
                      <Button size="small" disabled={pending} onClick={() => void update(image, false)}>
                        选用
                      </Button>
                    )}
                    <Popconfirm
                      title="删除这张造型图？"
                      okText="删除"
                      cancelText="取消"
                      onConfirm={() => update(image, true)}
                    >
                      <Button size="small" danger disabled={pending}>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                )}
              </Flex>
            </List.Item>
          )}
        />
      )}
    </>
  )
}

export default function LookPictures({
  look,
  person,
  canEdit,
  canEditNow,
  action,
  onChange,
}: {
  look: LibraryLook
  person: string
  canEdit: boolean
  canEditNow: () => boolean
  action?: LookImageAction
  onChange: () => void
}) {
  const [open, setOpen] = useState(false)
  const label = person + ' · ' + look.name
  const selected = look.selected_image?.media_object_id
  const pending = look.pending_image_count > 0 || action?.pendingLookId === look.id
  const full = look.image_count >= 10
  return (
    <Flex vertical gap={6} style={{ marginBlock: 8 }}>
      {selected && <Preview objectId={selected} label={label} />}
      {pending && (
        <span role="status">
          <Spin size="small" /> 正在生成造型图
        </span>
      )}
      {!pending && look.latest_image_error && <Alert type="error" message={look.latest_image_error} />}
      <Space wrap>
        {canEdit && action && (
          <Button
            type="primary"
            size="small"
            disabled={!action.generate || pending || full}
            onClick={() => {
              if (canEditNow()) action.generate?.(look.id)
            }}
          >
            {selected ? '重新生成图片' : '生成图片'}
          </Button>
        )}
        {look.image_count > 0 && (
          <Button size="small" onClick={() => setOpen(true)}>
            图片历史（{look.image_count}）
          </Button>
        )}
      </Space>
      {canEdit && action && full && <span>已达 10 张上限，删除历史图片后可继续生成。</span>}
      {open && (
        <Drawer title={label + ' · 图片历史'} open onClose={() => setOpen(false)}>
          <PictureHistory
            key={[look.id, look.image_count, look.pending_image_count, selected].join('/')}
            lookId={look.id}
            label={label}
            canEdit={canEdit}
            canEditNow={canEditNow}
            onChange={onChange}
          />
        </Drawer>
      )}
    </Flex>
  )
}
