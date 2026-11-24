import type { AppApi } from '@kirocrew/app-sdk'
import { useCallback, useSyncExternalStore, useRef } from 'react'
import { callFor, type VoiceCall } from './engine'

/** Subscribe a component to the shared call engine of one chat slot. */
export function useCall(api: AppApi, slot: string): VoiceCall {
  const call = callFor(api, slot)
  const version = useRef(0)
  const subscribe = useCallback(
    (cb: () => void) =>
      call.subscribe(() => {
        version.current++
        cb()
      }),
    [call],
  )
  useSyncExternalStore(subscribe, () => version.current)
  return call
}
