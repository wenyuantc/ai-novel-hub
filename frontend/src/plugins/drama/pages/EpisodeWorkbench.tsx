import { Alert, Button, Dropdown, Popconfirm, Space, Spin, Tabs, Tag } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { errorMessage } from '../../../api/client'
import { StudioPage } from '../../../components/StudioUI'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { useLoader } from '../../../hooks/useLoader'
import { dramaApi, type Episode } from '../api'
import AssetStep from '../components/AssetStep'
import EpisodeText from '../components/EpisodeText'
import PageScope, { LoadError, type Access } from '../components/PageScope'
import EpisodeModelSettings from '../components/EpisodeModelSettings'
import { useEpisodeModelSettings } from '../useEpisodeModelSettings'
import ShotStep from '../components/ShotStep'
import TaskPanel from '../components/TaskPanel'
import './drama.css'

function EpisodeWorkbench({ pid, sid, eid, access }: { pid: number; sid: number; eid: number; access: Access }) {
  const { data, error, reload, setData } = useLoader(
    useCallback(async () => {
      const [series, episode] = await Promise.all([dramaApi.series(sid), dramaApi.episode(eid)])
      if (series.project_id !== pid || episode.series_id !== sid) throw new Error('本剧中没有这一集')
      return { series, episode }
    }, [pid, sid, eid]),
  )
  const showModels = Boolean(data) && access.canEdit && access.canUseAI
  const models = useEpisodeModelSettings(eid, showModels, access.canUseAINow)
  const navigate = useNavigate()
  const mobile = useIsMobile()
  const [failure, setFailure] = useState('')
  const [reloadKey, setReloadKey] = useState(0)
  const [dirty, setDirty] = useState<Record<string, boolean>>({})
  const mark = (field: string) => (value: boolean) => setDirty((prev) => ({ ...prev, [field]: value }))
  const refresh = () => {
    reload()
    setReloadKey((k) => k + 1)
  }
  if (error) return <LoadError error={error} reload={reload} />
  if (!data) return <Spin />
  const { episode, series } = data
  const updated = (change: Episode) =>
    setData(
      (prev) => ({
        ...data,
        ...prev,
        episode: prev && prev.episode.revision > change.revision ? prev.episode : change,
      }),
      { invalidatePending: true },
    )
  const path = '/projects/' + pid + '/features/drama/' + sid
  const remove = async () => {
    if (!access.canEditNow()) return
    try {
      await dramaApi.deleteEpisode(eid, episode.revision)
      if (access.canEditNow()) navigate(path)
    } catch (e) {
      if (access.canEditNow()) {
        setFailure(errorMessage(e))
        reload()
      }
    }
  }
  const download = async (format: 'md' | 'docx', parts: string) => {
    try {
      await dramaApi.export(eid, format, parts, access.canExportNow)
    } catch (e) {
      if (access.canExportNow()) setFailure(errorMessage(e))
    }
  }
  return (
    <StudioPage
      beforeHeading={showModels && <EpisodeModelSettings settings={models} />}
      title={'第 ' + episode.number + ' 集 · ' + episode.title}
      action={
        <Space wrap>
          <Link to={path}>{series.title}</Link>
          {access.canExport && (
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  { key: 'script', label: '剧本' },
                  { key: 'shots', label: '分镜表' },
                  { key: 'script,shots', label: '剧本和分镜表' },
                ].map((item) => ({
                  ...item,
                  children: (['docx', 'md'] as const).map((format) => ({
                    key: item.key + format,
                    label: format === 'docx' ? 'Word 文档' : 'Markdown',
                    onClick: () => void download(format, item.key),
                  })),
                })),
              }}
            >
              <Button>导出文档</Button>
            </Dropdown>
          )}
          {access.canEdit && (
            <Popconfirm title="删除本集及其全部分镜？" onConfirm={remove}>
              <Button danger>删除本集</Button>
            </Popconfirm>
          )}
        </Space>
      }
    >
      {failure && <Alert type="error" message={failure} />}
      <TaskPanel sid={sid} eid={eid} access={access} refresh={refresh} modelBinding={models.binding}>
        {(run, imageAction) => {
          const onTask = Object.values(dirty).some(Boolean) ? undefined : run
          return (
            <Tabs
              tabPosition={mobile ? 'top' : 'left'}
              items={[
                {
                  key: 'source',
                  label: '原始内容',
                  children: (
                    <>
                      <EpisodeText
                        episode={episode}
                        field="title"
                        label="集名"
                        access={access}
                        updated={updated}
                        onDirty={mark('title')}
                      />
                      {episode.sources?.length > 0 && (
                        <p>
                          取自{' '}
                          {episode.sources.map((source) => (
                            <Tag key={source.sort}>{source.chapter_title}</Tag>
                          ))}
                        </p>
                      )}
                      <EpisodeText
                        episode={episode}
                        field="source_text"
                        label="原始内容"
                        access={access}
                        updated={updated}
                        onDirty={mark('source_text')}
                      />
                    </>
                  ),
                },
                {
                  key: 'script',
                  label: '剧本',
                  children: (
                    <>
                      {onTask && (
                        <Button
                          type="primary"
                          disabled={!episode.source_text.trim()}
                          onClick={() => onTask('drama.rewrite')}
                        >
                          AI 改写剧本
                        </Button>
                      )}
                      <EpisodeText
                        episode={episode}
                        field="script"
                        label="剧本（可直接手写）"
                        access={access}
                        updated={updated}
                        manual={episode.script_skipped || !episode.script}
                        onDirty={mark('script')}
                      />
                    </>
                  ),
                },
                {
                  key: 'assets',
                  label: '资产',
                  children: (
                    <AssetStep
                      pid={pid}
                      sid={sid}
                      eid={eid}
                      canEdit={access.canEdit}
                      canEditNow={access.canEditNow}
                      onTask={onTask}
                      imageAction={imageAction}
                      episodeRevision={episode.revision}
                      reloadKey={reloadKey}
                      onChange={refresh}
                    />
                  ),
                },
                {
                  key: 'shots',
                  label: '分镜',
                  children: (
                    <ShotStep
                      pid={pid}
                      sid={sid}
                      eid={eid}
                      canEdit={access.canEdit}
                      canEditNow={access.canEditNow}
                      onTask={onTask}
                      episodeRevision={episode.revision}
                      reloadKey={reloadKey}
                      onChange={refresh}
                    />
                  ),
                },
              ]}
            />
          )
        }}
      </TaskPanel>
    </StudioPage>
  )
}
export default function EpisodeWorkbenchPage() {
  return (
    <PageScope>
      {(pid, sid, eid, access) => <EpisodeWorkbench pid={pid} sid={sid} eid={eid} access={access} />}
    </PageScope>
  )
}
