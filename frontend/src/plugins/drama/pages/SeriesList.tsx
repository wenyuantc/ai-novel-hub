import { Button, Empty, List, Spin, Tag } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { StudioPage, StudioPanel } from '../../../components/StudioUI'
import { useLoader } from '../../../hooks/useLoader'
import { dramaApi } from '../api'
import PageScope, { LoadError, type Access } from '../components/PageScope'
import SeriesForm from '../components/SeriesForm'

function SeriesList({ pid, access }: { pid: number; access: Access }) {
  const { data, error, reload } = useLoader(
    useCallback(() => Promise.all([dramaApi.list(pid), dramaApi.styles()]), [pid]),
  )
  const [create, setCreate] = useState(false)
  const navigate = useNavigate()
  const path = '/projects/' + pid + '/features/drama/'
  return (
    <StudioPage
      title="短剧"
      action={
        access.canEdit && (
          <Button type="primary" disabled={!data} onClick={() => setCreate(true)}>
            新建短剧
          </Button>
        )
      }
    >
      {error ? (
        <LoadError error={error} reload={reload} />
      ) : !data ? (
        <Spin />
      ) : (
        <StudioPanel>
          <List
            dataSource={data[0]}
            locale={{ emptyText: <Empty description="还没有短剧" /> }}
            renderItem={(row) => (
              <List.Item key={row.id}>
                <List.Item.Meta title={<Link to={path + row.id}>{row.title}</Link>} description={row.description} />
                <Tag>{data[1].find((s) => s.key === row.style)?.label ?? row.style}</Tag>
                <Tag>{row.aspect_ratio}</Tag>
              </List.Item>
            )}
          />
        </StudioPanel>
      )}
      {data && create && access.canEdit && (
        <SeriesForm
          pid={pid}
          styles={data[1]}
          close={() => setCreate(false)}
          current={access.canEditNow}
          saved={(row) => navigate(path + row.id)}
        />
      )}
    </StudioPage>
  )
}
export default function SeriesListPage() {
  return <PageScope>{(pid, _sid, _eid, access) => <SeriesList pid={pid} access={access} />}</PageScope>
}
