import { Alert, Button, Empty, List, Popconfirm, Space, Spin, Tabs } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { errorMessage } from '../../../api/client'
import { StudioPage, StudioPanel } from '../../../components/StudioUI'
import { useLoader } from '../../../hooks/useLoader'
import { dramaApi } from '../api'
import AssetStep from '../components/AssetStep'
import NewEpisode from '../components/NewEpisode'
import PageScope, { LoadError, type Access } from '../components/PageScope'
import SeriesForm from '../components/SeriesForm'
import TaskPanel from '../components/TaskPanel'

function SeriesDetail({ pid, sid, access }: { pid: number; sid: number; access: Access }) {
  const { data, error, reload } = useLoader(
    useCallback(async () => {
      const row = await dramaApi.series(sid)
      if (row.project_id !== pid) throw new Error('本作品中没有这部短剧')
      const [episodes, styles] = await Promise.all([dramaApi.episodes(sid), dramaApi.styles()])
      return { row, episodes, styles }
    }, [pid, sid]),
  )
  const [modal, setModal] = useState<'edit' | 'episode' | null>(null)
  const [failure, setFailure] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => {
    reload()
    setRefreshKey((key) => key + 1)
  }
  const navigate = useNavigate()
  const path = '/projects/' + pid + '/features/drama'
  if (error) return <LoadError error={error} reload={reload} />
  if (!data) return <Spin />
  const { row, episodes, styles } = data
  const remove = async () => {
    if (!access.canEditNow()) return
    try {
      await dramaApi.delete(sid, row.revision)
      if (access.canEditNow()) navigate(path)
    } catch (e) {
      if (access.canEditNow()) {
        setFailure(errorMessage(e))
        reload()
      }
    }
  }
  return (
    <StudioPage
      title={row.title}
      hint={[styles.find((s) => s.key === row.style)?.label ?? row.style, row.aspect_ratio, row.description]
        .filter(Boolean)
        .join(' · ')}
      action={
        <Space wrap>
          <Link to={path}>全部短剧</Link>
          {access.canEdit && (
            <>
              <Button onClick={() => setModal('edit')}>编辑短剧</Button>
              <Popconfirm title="删除短剧及其全部剧集和素材？" onConfirm={remove}>
                <Button danger>删除短剧</Button>
              </Popconfirm>
            </>
          )}
        </Space>
      }
    >
      {failure && <Alert type="error" message={failure} />}
      <Tabs
        items={[
          {
            key: 'episodes',
            label: '剧集',
            children: (
              <StudioPanel>
                {access.canEdit && (
                  <Button type="primary" onClick={() => setModal('episode')}>
                    新建剧集
                  </Button>
                )}
                <List
                  dataSource={episodes}
                  locale={{ emptyText: <Empty description="还没有剧集" /> }}
                  renderItem={(episode) => (
                    <List.Item key={episode.id}>
                      <Link to={path + '/' + sid + '/episodes/' + episode.id}>
                        第 {episode.number} 集 · {episode.title}
                      </Link>
                    </List.Item>
                  )}
                />
              </StudioPanel>
            ),
          },
          {
            key: 'assets',
            label: '素材库',
            children: (
              <TaskPanel sid={sid} access={access} refresh={refresh}>
                {(run) => (
                  <AssetStep
                    pid={pid}
                    sid={sid}
                    canEdit={access.canEdit}
                    canEditNow={access.canEditNow}
                    reloadKey={refreshKey}
                    onTask={run}
                    onChange={reload}
                  />
                )}
              </TaskPanel>
            ),
          },
        ]}
      />
      {modal === 'edit' && access.canEdit && (
        <SeriesForm
          pid={pid}
          initial={row}
          styles={styles}
          close={() => setModal(null)}
          current={access.canEditNow}
          saved={() => {
            setModal(null)
            reload()
          }}
        />
      )}
      {modal === 'episode' && access.canEdit && (
        <NewEpisode
          pid={pid}
          sid={sid}
          close={() => setModal(null)}
          current={access.canEditNow}
          created={(id) => navigate(path + '/' + sid + '/episodes/' + id)}
        />
      )}
    </StudioPage>
  )
}
export default function SeriesDetailPage() {
  return <PageScope>{(pid, sid, _eid, access) => <SeriesDetail pid={pid} sid={sid} access={access} />}</PageScope>
}
