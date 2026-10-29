import { DownloadOutlined, LikeFilled, LikeOutlined } from '@ant-design/icons'
import { Button, Space } from 'antd'
import type { WorkshopItem } from '../../api/types'
import { canRestoreWorkshopItem } from '../../utils/workshop'

interface Props {
  item: WorkshopItem
  isAdmin: boolean
  busy: boolean
  importDisabled: boolean
  onLike: () => void
  onImport: () => void
  onUpdate: () => void
  onHide: () => void
  onRestore: () => void
  onMetadata: () => void
  onDelete: () => void
}
/** 操作显隐跟服务端的 is_mine 与 hidden_by 对齐，正文更新只向作者开放。 */
export default function WorkshopItemControls({ item, isAdmin, busy, importDisabled, onLike, onImport, onUpdate, onHide, onRestore, onMetadata, onDelete }: Props) {
  const active = item.status === 'active'
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Space wrap>
      <Button icon={item.liked ? <LikeFilled /> : <LikeOutlined />} disabled={!active || busy} onClick={onLike}>{item.liked ? '取消点赞' : '点赞'}</Button>
      <Button type="primary" icon={<DownloadOutlined />} loading={busy} disabled={!active || importDisabled} onClick={onImport}>{item.imported ? '再次导入' : '导入到我的'}</Button>
    </Space>
    {(item.is_mine || isAdmin) && <Space wrap>
      {item.is_mine && <Button disabled={busy} onClick={onUpdate}>提交更新</Button>}
      {active ? <Button disabled={busy} onClick={onHide}>下架</Button>
        : <Button disabled={busy || !canRestoreWorkshopItem(item, isAdmin)} onClick={onRestore}>重新上架</Button>}
      {isAdmin && <><Button disabled={busy} onClick={onMetadata}>管理分类和标签</Button><Button danger disabled={busy} onClick={onDelete}>删除条目</Button></>}
    </Space>}
  </Space>
}
