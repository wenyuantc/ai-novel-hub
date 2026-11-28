import { Alert, Button } from 'antd'
import { MODEL_TYPE_LABELS } from '../../../api/aiConfigs'
import AIConfigSelect from '../../../components/AIConfigSelect'
import { EPISODE_MODEL_TYPES, type useEpisodeModelSettings } from '../useEpisodeModelSettings'

export default function EpisodeModelSettings({ settings }: { settings: ReturnType<typeof useEpisodeModelSettings> }) {
  return (
    <section aria-label="本集模型设置" aria-busy={settings.saving}>
      <div className="drama-model-bar">
        {EPISODE_MODEL_TYPES.map((type) => (
          <div key={type}>
            <label htmlFor={'drama-model-' + type}>{MODEL_TYPE_LABELS[type]}</label>
            {settings.unavailable.includes(type) ? (
              <Alert
                type="warning"
                showIcon
                message="当前无法确认已保存的配置"
                action={
                  <>
                    <Button size="small" onClick={settings.reload}>
                      重新读取
                    </Button>
                    <Button size="small" disabled={!settings.ready} onClick={() => void settings.change(type, null)}>
                      清除选择
                    </Button>
                  </>
                }
              />
            ) : (
              <AIConfigSelect
                id={'drama-model-' + type}
                modelType={type}
                value={settings.values?.[type] ?? null}
                disabled={!settings.ready}
                onChange={(id) => void settings.change(type, id)}
              />
            )}
          </div>
        ))}
      </div>
      {settings.saving && <span role="status">正在保存模型选择…</span>}
      {settings.error && (
        <Alert
          type="warning"
          showIcon
          message={settings.error}
          action={<Button onClick={settings.reload}>重新读取</Button>}
        />
      )}
    </section>
  )
}
