// The bar the dashboard mounts above the composer when the user clicks the
// "语音通话" chip. Cloud edition: on a phone (touch screen or narrow window) it
// shows a large hold-to-talk button, because there is no keyboard shortcut; on
// a laptop the shortcut and the dot still work. One line of text carries the
// status, the live draft, or an error, so a failed segment is never silent.
import { useAppApi } from '@kirocrew/app-sdk'
import { useEffect, useRef, useState } from 'react'
import { bindHotkey, hotkeyLabel, LABEL, slotFromSessionKey, stopOthers } from './engine'
import { useCall } from './useCall'

interface Props {
  session: { sessionKey: string; folderId?: string; folderName?: string; cwd: string }
  onClose: () => void
}

const btn: React.CSSProperties = {
  background: 'transparent',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  padding: '2px 10px',
  fontSize: 12,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
}

function usePhone(): boolean {
  const query = '(pointer: coarse), (max-width: 700px)'
  const [phone, setPhone] = useState(() => typeof window !== 'undefined' && window.matchMedia?.(query).matches === true)
  useEffect(() => {
    const m = window.matchMedia?.(query)
    if (!m) return
    const on = () => setPhone(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return phone
}

const hint = (state: string, phone: boolean): string | undefined => {
  if (phone) {
    return ({
      ready: '按住下面的按钮说话，松开发送',
      recording: '在听… 松开发送，滑出按钮不发',
      speaking: '在说… 按住按钮就能打断',
    } as Record<string, string>)[state]
  }
  const k = hotkeyLabel()
  return ({
    ready: `按 ${k} 说话 · 按住说、松开发；或按一下开始、再按一下发`,
    recording: `在听… 再按 ${k} 发送，Esc 不发`,
    speaking: `在说… 按 ${k} 打断`,
  } as Record<string, string>)[state]
}

function Dot({ state, level, onClick }: { state: string; level: number; onClick: () => void }) {
  const color =
    state === 'error' ? 'var(--danger, #e5484d)'
      : state === 'speaking' ? 'var(--accent, #9046ff)'
        : state === 'thinking' || state === 'transcribing' ? '#f5a524'
          : state === 'recording' || state === 'hearing' ? '#e5484d'
            : 'var(--muted, #888)'
  const live = state === 'hearing' || state === 'listening' || state === 'recording'
  const scale = live ? 1 + Math.min(level * 12, 0.8) : 1
  return (
    <span
      role="button"
      aria-label="说话或发送"
      onClick={onClick}
      style={{
        width: 14, height: 14, borderRadius: 7, background: color, flex: 'none', cursor: 'pointer',
        transform: `scale(${scale})`, transition: 'transform 80ms linear, background 200ms',
      }}
    />
  )
}

export default function VoiceCallControl({ session, onClose }: Props) {
  const api = useAppApi()
  const slot = slotFromSessionKey(session.sessionKey)
  const call = useCall(api, slot)
  const started = useRef(false)
  const phone = usePhone()
  const holding = useRef(false)

  useEffect(() => {
    bindHotkey()
    if (started.current || !slot) return
    started.current = true
    if (!call.running) {
      stopOthers(slot)
      void call.start()
    }
  }, [slot, call])

  if (!slot) {
    return <p style={{ fontSize: 12, color: 'var(--muted)', margin: 0, padding: '8px 12px' }}>先打开一个会话，再从会话里开始通话。</p>
  }

  const recording = call.state === 'recording'
  const text =
    call.state === 'error' ? call.note
      : call.notice ? call.notice
      : call.draft ? call.draft
        : hint(call.state, phone) ?? LABEL[call.state]

  const tapDot = () => {
    if (!call.running) { stopOthers(slot); void call.start() }
    else if (recording) void call.sendTalk()
    else if (call.mode === 'manual') call.beginTalk()
  }

  // Hold-to-talk: press opens the mic, release sends, sliding off cancels.
  const press = async (e: React.PointerEvent<HTMLButtonElement>) => {
    e.preventDefault()
    e.currentTarget.setPointerCapture?.(e.pointerId)
    holding.current = true
    call.unlockAudio()
    if (!call.running) {
      stopOthers(slot)
      await call.start()
    }
    if (holding.current) call.beginTalk()
  }
  const release = (e: React.PointerEvent<HTMLButtonElement>) => {
    e.preventDefault()
    if (!holding.current) return
    holding.current = false
    if (call.state === 'recording') void call.sendTalk()
  }
  // Sliding well off the button (like a messaging app) discards the recording.
  const move = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!holding.current) return
    const r = e.currentTarget.getBoundingClientRect()
    const margin = 48
    if (e.clientX < r.left - margin || e.clientX > r.right + margin || e.clientY < r.top - margin || e.clientY > r.bottom + margin) cancel()
  }
  const cancel = () => {
    if (!holding.current) return
    holding.current = false
    if (call.state === 'recording') call.cancelTalk()
  }

  return (
    <div data-testid="voice-call-control" style={{ color: 'var(--text)', padding: '6px 10px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <Dot state={call.state} level={call.level} onClick={tapDot} />
        <span
          aria-live="polite"
          data-testid="voice-call-status"
          title={text}
          style={{
            flex: 1, minWidth: 0, fontSize: call.draft ? 13 : 12,
            color: call.state === 'error' || call.notice ? 'var(--danger, #e5484d)' : call.draft ? 'var(--text)' : 'var(--muted)',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', direction: call.draft ? 'rtl' : 'ltr', textAlign: 'left',
          }}
        >
          {call.draft ? <bdi>{text}</bdi> : text}
        </span>
        <button type="button" style={btn} onClick={() => { call.stop(); onClose() }}>{call.running ? '挂断' : '关闭'}</button>
      </div>
      {phone && call.mode === 'manual' && (
        <>
          <button
            type="button"
            data-testid="voice-call-hold"
            aria-label="按住说话"
            onPointerDown={(e) => void press(e)}
            onPointerUp={release}
            onPointerMove={move}
            onPointerCancel={cancel}
            onLostPointerCapture={cancel}
            onContextMenu={(e) => e.preventDefault()}
            style={{
              width: '100%', height: 64, marginTop: 8, borderRadius: 32, border: 'none',
              background: recording ? 'var(--danger, #e5484d)' : 'var(--accent, #9046ff)', color: '#fff',
              fontSize: 17, fontWeight: 600, touchAction: 'none', userSelect: 'none',
              WebkitUserSelect: 'none', WebkitTouchCallout: 'none', cursor: 'pointer',
            } as React.CSSProperties}
          >
            {recording ? '松开发送' : '按住说话'}
          </button>
          <p style={{ fontSize: 11, color: 'var(--muted)', margin: '6px 0 0', textAlign: 'center' }}>
            请保持本页在前台、屏幕常亮：切到后台时手机会停掉麦克风。
          </p>
        </>
      )}
    </div>
  )
}
