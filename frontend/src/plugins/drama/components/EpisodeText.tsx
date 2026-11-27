import { Alert, Button, Input, Space } from 'antd'
import { useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../../api/client'
import { dramaApi, type Episode } from '../api'
import type { Access } from './PageScope'

type Field = 'title' | 'source_text' | 'script'
export default function EpisodeText({
  episode,
  field,
  label,
  access,
  updated,
  manual = false,
  onDirty,
}: {
  episode: Episode
  field: Field
  label: string
  access: Access
  updated: (episode: Episode) => void
  manual?: boolean
  onDirty: (dirty: boolean) => void
}) {
  const [draft, setDraft] = useState<{ value: string; revision: number } | null>(null)
  const [failure, setFailure] = useState('')
  const [conflict, setConflict] = useState(false)
  const [pending, setPending] = useState(false)
  const lock = useRef(false)
  const save = async (overwrite = false) => {
    if (!draft || lock.current || !access.canEditNow()) return
    lock.current = true
    setPending(true)
    try {
      const revision = overwrite ? (await dramaApi.episode(episode.id)).revision : draft.revision
      if (!access.canEditNow()) return
      const row = await dramaApi.updateEpisode(episode.id, {
        [field]: draft.value,
        revision,
        ...(manual && field === 'script' ? { script_skipped: true } : {}),
      })
      if (!access.canEditNow()) return
      setDraft(null)
      setConflict(false)
      setFailure('')
      onDirty(false)
      updated(row)
    } catch (e) {
      if (access.canEditNow()) {
        setFailure(errorMessage(e))
        setConflict(e instanceof ApiError && e.status === 409)
      }
    } finally {
      lock.current = false
      setPending(false)
    }
  }
  const discard = async () => {
    try {
      const fresh = await dramaApi.episode(episode.id)
      if (!access.canEditNow()) return
      setDraft(null)
      setConflict(false)
      setFailure('')
      onDirty(false)
      updated(fresh)
    } catch (e) {
      if (access.canEditNow()) setFailure(errorMessage(e))
    }
  }
  const shared = {
    'aria-label': label,
    value: draft?.value ?? episode[field],
    readOnly: !access.canEdit || pending,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      const value = e.target.value
      if (value === episode[field]) {
        setDraft(null)
        setFailure('')
        setConflict(false)
        onDirty(false)
        return
      }
      setDraft({ value, revision: draft?.revision ?? episode.revision })
      onDirty(true)
    },
    onBlur: () => {
      if (!failure && draft) void save()
    },
  }
  return (
    <div className="drama-field">
      <label>
        <span>{label}</span>
        {field === 'title' ? (
          <Input {...shared} maxLength={200} />
        ) : (
          <Input.TextArea
            {...shared}
            rows={field === 'script' ? 15 : 10}
            maxLength={field === 'script' ? 200000 : 60000}
          />
        )}
      </label>
      {failure && (
        <Alert
          type="warning"
          message={failure}
          description={
            <Space wrap>
              {conflict ? (
                <>
                  <Button onClick={discard}>重新载入</Button>
                  <Button onClick={() => void save(true)} loading={pending}>
                    覆盖保存
                  </Button>
                </>
              ) : (
                <Button onClick={() => void save()} loading={pending}>
                  重试保存
                </Button>
              )}
            </Space>
          }
        />
      )}
    </div>
  )
}
