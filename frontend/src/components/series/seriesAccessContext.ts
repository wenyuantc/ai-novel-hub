import { createContext, useContext } from 'react'
import { ApiError } from '../../api/client'
import { SeriesAuthorizationScope } from './seriesAuthorization'

export function isSeriesAccessError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404
    || (error.status === 409 && error.body?.code === 'stale_series_access'))
}

export const SeriesAccessContext = createContext<(error: unknown) => void>(() => {})
export const SeriesAuthorizationContext = createContext(new SeriesAuthorizationScope())

export function useSeriesAccessLoss() {
  return useContext(SeriesAccessContext)
}

export function useSeriesAuthorization() {
  return useContext(SeriesAuthorizationContext)
}
