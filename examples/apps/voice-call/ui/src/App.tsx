// App page: settings (voice, speed, pause length) and a speech test.
import { useAppApi } from '@kirocrew/app-sdk'
import { useEffect, useState } from 'react'
import { chineseVoices, HOTKEYS, loadSettings, saveSettings, type HotkeyId, type Settings } from './engine'

const card: React.CSSProperties = {
  background: 'var(--surface, transparent)', color: 'var(--text)',
  border: '1px solid var(--border)', borderRadius: 10, padding: 16, marginBottom: 14,
}
const label: React.CSSProperties = { display: 'block', fontSize: 13, margin: '10px 0 4px' }
const input: React.CSSProperties = {
  background: 'var(--bg, transparent)', color: 'var(--text)', border: '1px solid var(--border)',
  borderRadius: 6, padding: '4px 8px', fontSize: 13,
}

export default function VoiceCallPage() {
  const api = useAppApi()
  const [s, setS] = useState<Settings>(loadSettings())
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([])

  useEffect(() => {
    const load = () => setVoices(chineseVoices())
    load()
    window.speechSynthesis?.addEventListener('voiceschanged', load)
    return () => window.speechSynthesis?.removeEventListener('voiceschanged', load)
  }, [])

  const update = (patch: Partial<Settings>) => {
    const next = { ...s, ...patch }
    setS(next)
    saveSettings(next)
  }

  const test = () => {
    window.speechSynthesis.cancel()
    if (s.voice === 'polly') { const a = new Audio(); void api.raw('/apps/voice-call/api/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: '你好，我是 Kiro。现在可以直接跟我说话了。', voice: s.voice }) }).then(async (r) => { a.src = URL.createObjectURL(await r.blob()); void a.play() }); return }
    const u = new SpeechSynthesisUtterance('你好，我是 Kiro。现在可以直接跟我说话了。')
    const v = voices.find((x) => x.name === s.voice) ?? voices.find((x) => /Tingting/i.test(x.name)) ?? voices[0]
    if (v) { u.voice = v; u.lang = v.lang } else u.lang = 'zh-CN'
    u.rate = s.rate
    window.speechSynthesis.speak(u)
  }

  return (
    <div style={{ padding: '20px 24px', overflowY: 'auto', flex: 1, minHeight: 0, color: 'var(--text)' }}>
      <h1 style={{ fontSize: 20, margin: '0 0 4px' }}>语音通话</h1>
      <p style={{ fontSize: 13, color: 'var(--muted)', margin: '0 0 16px' }}>
        在任意会话的输入框下面点「语音通话」，直接说话。手机上按住大按钮说、松开发送；电脑上用快捷键。
        听写用沙箱里 Kiro Crew 自带的识别，朗读用 Amazon Polly 的 Zhiyu，失败的那句会改用浏览器自带的声音。
        手机上请保持本页在前台、屏幕常亮，切到后台时系统会停掉麦克风。
      </p>

      <div style={card}>
        <strong style={{ fontSize: 14 }}>朗读</strong>
        <label style={label}>声音</label>
        <select style={input} value={s.voice} onChange={(e) => update({ voice: e.target.value })}>
          <option value="polly">Zhiyu（Amazon Polly，推荐）</option>
          <option value="">浏览器自带声音（马上开口）</option>
          {voices.map((v) => (
            <option key={v.name} value={v.name}>{v.name}{v.localService ? '' : '（联网）'}</option>
          ))}
        </select>
        <label style={label}>语速：{s.rate.toFixed(2)}</label>
        <input type="range" min={0.8} max={1.8} step={0.05} value={s.rate} onChange={(e) => update({ rate: Number(e.target.value) })} />
        <label style={label}>一次最多念多少字：{s.maxSpokenChars}</label>
        <input type="range" min={150} max={2000} step={50} value={s.maxSpokenChars} onChange={(e) => update({ maxSpokenChars: Number(e.target.value) })} />
        <div style={{ marginTop: 12 }}>
          <button type="button" style={{ ...input, cursor: 'pointer' }} onClick={test}>试听</button>
        </div>
      </div>

      <div style={card}>
        <strong style={{ fontSize: 14 }}>怎么发</strong>
        <label style={label}>快捷键</label>
        <select style={input} value={s.hotkey} onChange={(e) => update({ hotkey: e.target.value as HotkeyId })}>
          {(Object.keys(HOTKEYS) as HotkeyId[]).map((id) => <option key={id} value={id}>{HOTKEYS[id].label}</option>)}
        </select>
        <label style={{ ...label, display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="radio" checked={s.mode === 'manual'} onChange={() => update({ mode: 'manual' })} />
          手动发送（推荐）：点「说」或按 ⌥空格 开始，点「发送」或再按一次 ⌥空格 发出。旁边有人说话也不会被听进去。手机上是按住大按钮说、松开发送。
        </label>
        <label style={{ ...label, display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="radio" checked={s.mode === 'auto'} onChange={() => update({ mode: 'auto' })} />
          自动发送：一直在听，停顿就发出，我念的时候你开口就能打断。适合一个人在安静的地方。
        </label>
        <label style={{ ...label, display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="checkbox" checked={s.fast} onChange={(e) => update({ fast: e.target.checked })} />
          快答：通话时把这个会话的推理强度调成「低」，回得更快；挂断后自动恢复原来的设置。
        </label>
      </div>

      <div style={card}>
        <strong style={{ fontSize: 14 }}>听写</strong>
        <label style={label}>停顿多久算说完（毫秒）：{s.silenceMs}</label>
        <input type="range" min={500} max={2500} step={100} value={s.silenceMs} onChange={(e) => update({ silenceMs: Number(e.target.value) })} />
        <label style={label}>自动发送：没人说话多久才发出（毫秒）：{s.autoSendMs}</label>
        <input type="range" min={1500} max={8000} step={500} value={s.autoSendMs} onChange={(e) => update({ autoSendMs: Number(e.target.value) })} />
        <p style={{ fontSize: 12, color: 'var(--muted)' }}>老是被打断就调大，嫌等得久就调小。改完下一通电话生效。</p>
      </div>
    </div>
  )
}
