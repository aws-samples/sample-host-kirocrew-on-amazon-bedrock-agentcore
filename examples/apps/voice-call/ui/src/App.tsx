// App page: speech status, per-language voices and speeds, how to send, recognition.
import { useAppApi } from '@kirocrew/app-sdk'
import { useEffect, useState } from 'react'
import { chineseVoices, HOTKEYS, hotkeyLabel, loadSettings, saveSettings, type HotkeyId, type Settings } from './engine'
import { defaultSttLanguages, langName, setUiLang, uiLang, type UiLang } from './i18n'

const card: React.CSSProperties = {
  background: 'var(--surface, transparent)', color: 'var(--text)',
  border: '1px solid var(--border)', borderRadius: 10, padding: 16, marginBottom: 14,
}
const label: React.CSSProperties = { display: 'block', fontSize: 13, margin: '10px 0 4px' }
const input: React.CSSProperties = {
  background: 'var(--bg, transparent)', color: 'var(--text)', border: '1px solid var(--border)',
  borderRadius: 6, padding: '4px 8px', fontSize: 13,
}
const muted: React.CSSProperties = { fontSize: 12, color: 'var(--muted)', margin: '4px 0 8px' }

type PollyLang = { sample: string; default: string; options: Array<{ id: string; gender: 'f' | 'm'; engine: string }> }
type Status = { polly: boolean; transcribe: boolean; region: string; error?: string; transcribe_missing?: string }

const TEXT = {
  zh: {
    title: '语音通话',
    intro: '在任意会话的输入框下面点「语音通话」，直接跟 Kiro 说话；回复会念给你听，你一开口它就停。手机上按住大按钮说、松开发送；电脑上点「说」或用快捷键。',
    status: '语音服务',
    checking: '正在检查…',
    on: '已接通', off: '未接通',
    pollyOff: '回复会改用浏览器自带的声音朗读。',
    sttOff: '识别会改用 Kiro Crew 自带的识别。',
    howToAws: '要用 Amazon 的声音和识别：给运行这个 App 的环境配好 AWS 凭证（本机可设 VOICE_CALL_AWS_PROFILE），需要 polly:SynthesizeSpeech、transcribe:StartStreamTranscription 权限。',
    region: '区域',
    voices: '各语言的声音（Amazon Polly）',
    voicesHelp: '回复是哪种语言，就用那种语言选中的声音读。通话里说「说慢一点」「说快一点」「正常语速」，会调刚才那种语言的语速。',
    gender: { f: '女', m: '男' }, generative: '，生成式',
    speed: '语速', reset: '恢复', play: '试听', playing: '播放中…',
    fallback: '朗读方式', usePolly: 'Amazon Polly（推荐）', useBrowser: '只用浏览器自带的声音（不花钱）',
    maxChars: '一次最多念多少字',
    send: '怎么发', hotkey: '快捷键（⌥空格可能和输入法冲突，可以换）',
    manual: '手动发送（推荐）：点「说」或按快捷键开始，点「发送」或再按一次发出。旁边有人说话也不会被听进去。手机上是按住大按钮说、松开发送。',
    auto: '自动发送：一直在听，识别出的文字停 {0} 秒没有新内容就发出。我念的时候你开口就能打断。适合一个人在安静的地方。',
    fast: '快答：通话时把这个会话的推理强度调成「低」，回得更快；挂断后自动恢复。',
    stt: '听写',
    sttLangs: '识别语言（Transcribe 语言代码，逗号分隔；填两种以上会自动判断语言，每句慢约 4 秒）',
    silence: '停顿多久算一句说完（毫秒）', autoSend: '自动发送：最后一次识别出文字后等多久发出（毫秒）',
    tuneHelp: '老是被切断就调大，嫌等得久就调小。',
    uiLang: '界面语言',
  },
  en: {
    title: 'Voice call',
    intro: 'Click "Voice call" under the message box of any chat and talk to Kiro; replies are read aloud and stop as soon as you speak. On a phone, hold the big button and release to send; on a computer, click "Talk" or use the hotkey.',
    status: 'Speech services',
    checking: 'Checking…',
    on: 'connected', off: 'not connected',
    pollyOff: 'Replies are read by the browser voice instead.',
    sttOff: 'Recognition falls back to Kiro Crew\'s own recognizer.',
    howToAws: 'For Amazon voices and recognition, give the environment running this app AWS credentials (locally you can set VOICE_CALL_AWS_PROFILE) with polly:SynthesizeSpeech and transcribe:StartStreamTranscription.',
    region: 'Region',
    voices: 'Voice per language (Amazon Polly)',
    voicesHelp: 'Each reply is read by the voice chosen for its language. During a call, say "slower", "faster" or "normal speed" to change the speed of the language just spoken.',
    gender: { f: 'female', m: 'male' }, generative: ', generative',
    speed: 'Speed', reset: 'Reset', play: 'Play', playing: 'Playing…',
    fallback: 'Read aloud with', usePolly: 'Amazon Polly (recommended)', useBrowser: 'Browser voice only (free)',
    maxChars: 'Most characters read per reply',
    send: 'Sending', hotkey: 'Hotkey (⌥Space may clash with an input method; change it if so)',
    manual: 'Manual (recommended): click "Talk" or press the hotkey to start, click "Send" or press it again to send. People talking nearby are never picked up. On a phone, hold the big button and release to send.',
    auto: 'Automatic: always listening; sends once no new words have been recognized for {0} s. Speak while Kiro is talking to interrupt. Best alone in a quiet room.',
    fast: 'Quick answers: lower this chat\'s reasoning effort to "low" during a call, restored on hang-up.',
    stt: 'Recognition',
    sttLangs: 'Recognition languages (Transcribe codes, comma separated; two or more turn on language identification, about 4 s slower per sentence)',
    silence: 'Pause that ends a phrase (ms)', autoSend: 'Automatic: wait after the last recognized words (ms)',
    tuneHelp: 'Raise it if you get cut off, lower it if sending feels slow.',
    uiLang: 'Interface language',
  },
}

export default function VoiceCallPage() {
  const api = useAppApi()
  const [s, setS] = useState<Settings>(loadSettings())
  const [lang, setLang] = useState<UiLang>(uiLang())
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([])
  const [catalog, setCatalog] = useState<Record<string, PollyLang>>({})
  const [status, setStatus] = useState<Status | null>(null)
  const [playing, setPlaying] = useState('')
  const L = TEXT[lang]

  useEffect(() => {
    const load = () => setVoices(chineseVoices())
    load()
    window.speechSynthesis?.addEventListener('voiceschanged', load)
    return () => window.speechSynthesis?.removeEventListener('voiceschanged', load)
  }, [])

  useEffect(() => {
    const post = (path: string) => api.raw(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    void post('/apps/voice-call/api/voices').then(async (r) => { if (r.ok) setCatalog((await r.json()) as Record<string, PollyLang>) }).catch(() => undefined)
    void post('/apps/voice-call/api/status').then(async (r) => { if (r.ok) setStatus((await r.json()) as Status) }).catch(() => setStatus({ polly: false, transcribe: false, region: '' }))
  }, [api])

  const update = (patch: Partial<Settings>) => {
    const next = { ...s, ...patch }
    setS(next)
    saveSettings(next)
  }

  const preview = async (code: string, voice: string) => {
    setPlaying(`${code}:${voice}`)
    try {
      const r = await api.raw('/apps/voice-call/api/tts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: catalog[code].sample, lang: code, voices: { [code]: voice }, rates: s.pollyRates }),
      })
      if (!r.ok) { setPlaying(''); return }
      const url = URL.createObjectURL(await r.blob())
      const au = new Audio(url)
      au.onended = () => { URL.revokeObjectURL(url); setPlaying('') }
      await au.play()
    } catch { setPlaying('') }
  }

  const dot = (ok: boolean) => <span style={{ color: ok ? 'var(--success, #30a46c)' : 'var(--danger, #e5484d)' }}>●</span>

  return (
    <div style={{ padding: '20px 24px', overflowY: 'auto', flex: 1, minHeight: 0, color: 'var(--text)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h1 style={{ fontSize: 20, margin: '0 0 4px', flex: 1 }}>{L.title}</h1>
        <label style={{ fontSize: 12, color: 'var(--muted)' }}>{L.uiLang}{' '}
          <select style={input} value={lang} onChange={(e) => { const v = e.target.value as UiLang; setUiLang(v); setLang(v) }}>
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </label>
      </div>
      <p style={{ fontSize: 13, color: 'var(--muted)', margin: '0 0 16px' }}>{L.intro}</p>

      <div style={card} data-testid="voice-call-status-card">
        <strong style={{ fontSize: 14 }}>{L.status}</strong>
        {!status ? <p style={muted}>{L.checking}</p> : (
          <>
            <p style={{ fontSize: 13, margin: '8px 0 2px' }}>{dot(status.polly)} Amazon Polly：{status.polly ? L.on : L.off}{status.polly ? '' : ` — ${L.pollyOff}`}</p>
            <p style={{ fontSize: 13, margin: '2px 0' }}>{dot(status.transcribe)} Amazon Transcribe：{status.transcribe ? L.on : L.off}{status.transcribe ? '' : ` — ${L.sttOff}`}</p>
            {status.region && <p style={muted}>{L.region}: {status.region}</p>}
            {(!status.polly || !status.transcribe) && <p style={muted}>{L.howToAws}{status.error ? ` (${status.error})` : ''}{status.transcribe_missing ? ` (${status.transcribe_missing})` : ''}</p>}
          </>
        )}
      </div>

      {Object.keys(catalog).length > 0 && status?.polly !== false && (
        <div style={card}>
          <strong style={{ fontSize: 14 }}>{L.voices}</strong>
          <p style={muted}>{L.voicesHelp}</p>
          {Object.entries(catalog).map(([code, c]) => {
            const cur = s.pollyVoices[code] ?? c.default
            const rate = s.pollyRates[code] ?? 1
            return (
              <div key={code} style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '6px 0', flexWrap: 'wrap' }}>
                <span style={{ width: 130, fontSize: 13 }}>{langName(code, lang)}</span>
                <select style={input} value={cur} onChange={(e) => update({ pollyVoices: { ...s.pollyVoices, [code]: e.target.value } })}>
                  {c.options.map((o) => (
                    <option key={o.id} value={o.id}>{o.id}（{L.gender[o.gender]}{o.engine === 'generative' ? L.generative : ''}）</option>
                  ))}
                </select>
                <span style={{ fontSize: 12, color: 'var(--muted)' }}>{L.speed} {rate.toFixed(2)}×</span>
                {rate !== 1 && <button type="button" style={input} onClick={() => update({ pollyRates: { ...s.pollyRates, [code]: 1 } })}>{L.reset}</button>}
                <button type="button" style={input} onClick={() => void preview(code, cur)}>{playing === `${code}:${cur}` ? L.playing : L.play}</button>
              </div>
            )
          })}
        </div>
      )}

      <div style={card}>
        <strong style={{ fontSize: 14 }}>{L.fallback}</strong>
        <label style={label}>
          <select style={input} value={s.voice === 'polly' ? 'polly' : ''} onChange={(e) => update({ voice: e.target.value })}>
            <option value="polly">{L.usePolly}</option>
            <option value="">{L.useBrowser}</option>
          </select>
        </label>
        {s.voice !== 'polly' && voices.length > 0 && (
          <select style={input} value={s.voice} onChange={(e) => update({ voice: e.target.value })}>
            {voices.map((v) => <option key={v.name} value={v.name}>{v.name}</option>)}
          </select>
        )}
        <label style={label}>{L.maxChars}: {s.maxSpokenChars}</label>
        <input type="range" min={150} max={2000} step={50} value={s.maxSpokenChars} onChange={(e) => update({ maxSpokenChars: Number(e.target.value) })} />
      </div>

      <div style={card}>
        <strong style={{ fontSize: 14 }}>{L.send}</strong>
        <label style={label}>{L.hotkey}</label>
        <select style={input} value={s.hotkey} onChange={(e) => update({ hotkey: e.target.value as HotkeyId })}>
          {(Object.keys(HOTKEYS) as HotkeyId[]).map((id) => <option key={id} value={id}>{hotkeyLabel(id)}</option>)}
        </select>
        <label style={{ ...label, display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="radio" checked={s.mode === 'manual'} onChange={() => update({ mode: 'manual' })} />
          {L.manual}
        </label>
        <label style={{ ...label, display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="radio" checked={s.mode === 'auto'} onChange={() => update({ mode: 'auto' })} />
          {L.auto.replace('{0}', String(s.autoSendMs / 1000))}
        </label>
        <label style={{ ...label, display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="checkbox" checked={s.fast} onChange={(e) => update({ fast: e.target.checked })} />
          {L.fast}
        </label>
      </div>

      <div style={card}>
        <strong style={{ fontSize: 14 }}>{L.stt}</strong>
        <label style={label}>{L.sttLangs}</label>
        <input style={{ ...input, width: 220 }} value={s.sttLangs} placeholder={defaultSttLanguages(lang)} onChange={(e) => update({ sttLangs: e.target.value })} />
        <label style={label}>{L.silence}: {s.silenceMs}</label>
        <input type="range" min={500} max={2500} step={100} value={s.silenceMs} onChange={(e) => update({ silenceMs: Number(e.target.value) })} />
        <label style={label}>{L.autoSend}: {s.autoSendMs}</label>
        <input type="range" min={1500} max={8000} step={500} value={s.autoSendMs} onChange={(e) => update({ autoSendMs: Number(e.target.value) })} />
        <p style={muted}>{L.tuneHelp}</p>
      </div>
    </div>
  )
}
