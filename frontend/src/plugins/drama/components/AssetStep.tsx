import { PictureOutlined } from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Col,
  Dropdown,
  Empty,
  Flex,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { errorMessage } from '../../../api/client'
import { useLoader } from '../../../hooks/useLoader'
import { dramaAssetApi } from '../assetApi'
import {
  assetLabel,
  assetName,
  assetsOfKind,
  promptType,
  type AssetKind,
  type DramaAsset,
  type DramaAssets,
  type MemberKind,
} from '../assetTypes'
import AssetEditor, { type AssetSelection } from './AssetEditor'

export interface AssetStepProps {
  pid: number
  sid: number
  eid?: number
  canEdit: boolean
  canEditNow: () => boolean
  episodeRevision?: number
  onTask?: (kind: 'drama.extract' | 'drama.prompts', params: Record<string, unknown>) => void
  reloadKey?: number
  onChange?: () => void
}

function ImagePlaceholder() {
  return <PictureOutlined role="img" aria-label="暂无素材图" />
}

function Detail({ children }: { children: ReactNode }) {
  return (
    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBlockEnd: 4 }}>
      {children}
    </Typography.Paragraph>
  )
}

function MembershipDialog({
  sid,
  assets,
  busy,
  error,
  onAdd,
  onClose,
}: {
  sid: number
  assets: DramaAssets
  busy: boolean
  error: string
  onAdd: (kind: MemberKind, id: number) => Promise<void>
  onClose: () => void
}) {
  const library = useLoader(useCallback(() => dramaAssetApi.series(sid), [sid]))
  const [selected, setSelected] = useState<{ kind: MemberKind; id: number } | null>(null)
  const options = (['looks', 'scenes', 'props'] as const)
    .map((kind) => {
      const used = new Set(assetsOfKind(assets, kind).map((row) => row.id))
      const rows = !library.data
        ? []
        : kind === 'looks'
          ? library.data.characters.flatMap((character) =>
              character.looks.map((look) => ({ id: look.id, label: `${character.name} · ${look.name}` })),
            )
          : library.data[kind].map((row) => ({ id: row.id, label: assetName(row) }))
      return {
        label: assetLabel[kind],
        options: rows
          .filter((row) => !used.has(row.id))
          .map(({ label, id }) => ({ label, value: `${kind}/${id}`, kind, id })),
      }
    })
    .filter((group) => group.options.length)
  return (
    <Modal
      open
      title="从素材库加入本集"
      okText="加入本集"
      cancelText="关闭"
      confirmLoading={busy}
      onCancel={onClose}
      okButtonProps={{ disabled: !selected }}
      onOk={() => {
        if (selected) void onAdd(selected.kind, selected.id)
      }}
    >
      {error && <Alert type="error" message={error} />}
      {library.error && (
        <Alert type="error" message={library.error} action={<Button onClick={library.reload}>重试</Button>} />
      )}
      <Select
        style={{ width: '100%' }}
        aria-label="选择素材"
        placeholder="选择造型、场景或道具"
        loading={library.loading}
        showSearch
        optionFilterProp="label"
        options={options}
        notFoundContent={library.loading ? <Spin size="small" /> : '没有可加入本集的素材'}
        onChange={(value) => {
          const option = options.flatMap((group) => group.options).find((item) => item.value === value)
          setSelected(option ?? null)
        }}
      />
    </Modal>
  )
}

function AssetPanel({
  pid,
  sid,
  eid,
  canEdit,
  canEditNow,
  onTask,
  episodeRevision,
  onChange,
  reloadKey,
}: AssetStepProps) {
  const loaded = useLoader(
    useCallback(() => (eid ? dramaAssetApi.episode(eid) : dramaAssetApi.series(sid)), [eid, sid]),
  )
  const previousKey = useRef(reloadKey)
  const { reload } = loaded
  useEffect(() => {
    if (previousKey.current !== reloadKey) reload()
    previousKey.current = reloadKey
  }, [reloadKey, reload])
  const [selection, setSelection] = useState<AssetSelection | null>(null)
  const [chooseMember, setChooseMember] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const assets = loaded.data
  function changed() {
    loaded.reload()
    onChange?.()
  }
  async function mutate(action: () => Promise<unknown>, closeMembership = false) {
    if (!canEditNow() || busy) return
    setBusy(true)
    setError('')
    try {
      await action()
      if (!canEditNow()) return
      if (closeMembership) setChooseMember(false)
      changed()
    } catch (err) {
      if (!canEditNow()) return
      setError(errorMessage(err))
      changed()
    } finally {
      setBusy(false)
    }
  }
  async function member(kind: MemberKind, id: number, add: boolean) {
    if (eid && episodeRevision !== undefined)
      await mutate(() => dramaAssetApi.membership(eid, kind, id, episodeRevision, add), add)
  }
  function controls(kind: AssetKind, row: DramaAsset, parent = sid) {
    if (!canEdit) return null
    return (
      <Space wrap size={4}>
        <Button size="small" onClick={() => setSelection({ kind, row, parent })}>
          编辑{assetLabel[kind]}
        </Button>
        {kind !== 'characters' && onTask && (
          <Button
            size="small"
            onClick={() => onTask('drama.prompts', { targets: [{ type: promptType[kind], id: row.id }] })}
          >
            生成图片提示词
          </Button>
        )}
        {eid && kind !== 'characters' && (
          <Button
            size="small"
            disabled={busy || episodeRevision === undefined}
            onClick={() => void member(kind, row.id, false)}
          >
            移出本集
          </Button>
        )}
        {!eid && (
          <Popconfirm
            title={`从整部剧删除${assetLabel[kind]}「${assetName(row)}」？`}
            description="各集对它的引用也会移除。"
            okText="删除"
            cancelText="取消"
            onConfirm={() => mutate(() => dramaAssetApi.remove(kind, row.id, row.revision))}
          >
            <Button size="small" danger disabled={busy}>
              删除
            </Button>
          </Popconfirm>
        )}
      </Space>
    )
  }
  function column(kind: Exclude<AssetKind, 'looks'>, children: ReactNode) {
    return (
      <Col xs={24} lg={8} key={kind}>
        <Flex vertical gap={10}>
          <Flex justify="space-between" align="center">
            <Typography.Title level={4} style={{ margin: 0 }}>
              {assetLabel[kind]}
            </Typography.Title>
            {canEdit && (
              <Button
                size="small"
                disabled={!!eid && episodeRevision === undefined}
                onClick={() => setSelection({ kind, parent: sid })}
              >
                新建{assetLabel[kind]}
              </Button>
            )}
          </Flex>
          {assets?.[kind].length ? (
            children
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`暂无${assetLabel[kind]}`} />
          )}
        </Flex>
      </Col>
    )
  }
  if (!assets)
    return loaded.error ? (
      <Alert type="error" message={loaded.error} action={<Button onClick={loaded.reload}>重试</Button>} />
    ) : (
      <Spin />
    )
  return (
    <Flex vertical gap={16}>
      {(error || loaded.error) && (
        <Alert type="error" message={error || loaded.error} action={<Button onClick={changed}>刷新</Button>} />
      )}
      {canEdit && eid && (
        <Space wrap>
          {onTask && (
            <Dropdown.Button
              onClick={() => onTask('drama.extract', {})}
              menu={{
                items: [
                  { key: 'character', label: '仅提取角色' },
                  { key: 'scene', label: '仅提取场景' },
                  { key: 'prop', label: '仅提取道具' },
                ],
                onClick: ({ key }) => onTask('drama.extract', { asset_types: [key] }),
              }}
            >
              从剧本提取资产
            </Dropdown.Button>
          )}
          <Button
            disabled={episodeRevision === undefined}
            onClick={() => {
              setError('')
              setChooseMember(true)
            }}
          >
            从素材库加入本集
          </Button>
        </Space>
      )}
      <Row gutter={[16, 24]}>
        {column(
          'characters',
          assets.characters.map((person) => (
            <Card key={person.id} size="small" title={person.name}>
              {person.novel_character_name && <Tag color="blue">已关联小说角色 {person.novel_character_name}</Tag>}
              {person.role && <Tag>{person.role}</Tag>}
              <Detail>{person.appearance}</Detail>
              <Detail>{person.description}</Detail>
              {controls('characters', person)}
              {person.looks.map((look) => (
                <div
                  key={look.id}
                  style={{ borderInlineStart: '2px solid currentColor', marginBlock: 12, paddingInlineStart: 10 }}
                >
                  <Space wrap>
                    <ImagePlaceholder />
                    <Typography.Text strong>{look.name}</Typography.Text>
                    {look.is_default && <Tag>默认造型</Tag>}
                  </Space>
                  <Detail>{look.styling}</Detail>
                  {look.image_prompt && <Detail>图片提示词 · {look.image_prompt}</Detail>}
                  {controls('looks', look, person.id)}
                </div>
              ))}
              {canEdit && (
                <Button
                  size="small"
                  disabled={!!eid && episodeRevision === undefined}
                  onClick={() => setSelection({ kind: 'looks', parent: person.id })}
                >
                  新增造型
                </Button>
              )}
            </Card>
          )),
        )}
        {column(
          'scenes',
          assets.scenes.map((scene) => (
            <Card
              key={scene.id}
              size="small"
              title={
                <Space>
                  <ImagePlaceholder />
                  {assetName(scene)}
                </Space>
              }
            >
              {scene.novel_world_setting_title && (
                <Tag color="blue">已关联小说地点 {scene.novel_world_setting_title}</Tag>
              )}
              <Detail>{scene.description}</Detail>
              <Detail>{scene.lighting}</Detail>
              {scene.image_prompt && <Detail>图片提示词 · {scene.image_prompt}</Detail>}
              {controls('scenes', scene)}
            </Card>
          )),
        )}
        {column(
          'props',
          assets.props.map((prop) => (
            <Card
              key={prop.id}
              size="small"
              title={
                <Space>
                  <ImagePlaceholder />
                  {prop.name}
                </Space>
              }
            >
              {prop.category && <Tag>{prop.category}</Tag>}
              <Detail>{prop.description}</Detail>
              {prop.image_prompt && <Detail>图片提示词 · {prop.image_prompt}</Detail>}
              {controls('props', prop)}
            </Card>
          )),
        )}
      </Row>
      {canEdit && chooseMember && (
        <MembershipDialog
          sid={sid}
          assets={assets}
          busy={busy}
          error={error}
          onAdd={(kind, id) => member(kind, id, true)}
          onClose={() => setChooseMember(false)}
        />
      )}
      {canEdit && selection && (
        <AssetEditor
          pid={pid}
          sid={sid}
          selection={selection}
          canEditNow={canEditNow}
          episode={eid && episodeRevision !== undefined ? { id: eid, revision: episodeRevision } : undefined}
          onSaved={changed}
          onClose={() => setSelection(null)}
        />
      )}
    </Flex>
  )
}

export default function AssetStep(props: AssetStepProps) {
  return <AssetPanel key={`${props.pid}/${props.sid}/${props.eid ?? ''}`} {...props} />
}
