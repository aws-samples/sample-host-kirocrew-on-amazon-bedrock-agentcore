// Voice call engine, cloud edition (KiroCrew on AgentCore, phone or laptop):
// mic -> Kiro Crew's own recognizer (/api/stt/transcribe inside the sandbox) ->
// the chat slot -> spoken reply (Amazon Polly Zhiyu through the app backend,
// falling back to the browser voice).
//
// Phones: there is no keyboard, so the bar shows a large hold-to-talk button.
// A phone browser stops the microphone when the page goes to the background;
// that is the platform's rule, so the bar asks to keep the page in front and a
// Wake Lock keeps the screen on during a call.
//
// Two ways to talk:
//   manual (default): tap 「说」 to open the mic, tap 「发送」 to send. Nothing is
//     heard in between, so people talking nearby never reach Kiro.
//   auto: always listening; a pause ends a phrase and sends it.
//
// The engine is a plain object (not React state) so the call outlives the
// popover that shows it: the host closes the popover on outside clicks, and
// approving a tool in the chat must not hang up. One engine per chat slot.
import type { AppApi } from '@kirocrew/app-sdk'
import * as OpenCC from 'opencc-js/t2cn'

/** Whisper often answers Mandarin in Traditional characters; show and send Simplified. */
const toSimplified = OpenCC.Converter({ from: 'tw', to: 'cn' })

export type CallState =
  | 'idle' | 'starting' | 'ready' | 'listening' | 'hearing' | 'recording'
  | 'transcribing' | 'thinking' | 'speaking' | 'error'
export type Mode = 'manual' | 'auto'

export const LABEL: Record<CallState, string> = {
  idle: '点一下开始通话',
  starting: '正在打开麦克风…',
  ready: '按住说话，松开发送',
  listening: '在听，直接说',
  hearing: '在听你说…',
  recording: '在录…再按一次快捷键发送',
  transcribing: '在识别…',
  thinking: '在想…',
  speaking: '在说…',
  error: '出错了',
}

export interface Line {
  who: 'you' | 'kiro' | 'note'
  text: string
}

export const VOICE_PREFIX = '🎙️ '
export const SPEECH_HINT = '\n\n（语音通话：用口语简短回答，先说结论，两三句话；表格、代码、链接放在后面，我看屏幕。）'

// ---- settings (per browser) ----
export interface Settings {
  voice: string
  rate: number
  maxSpokenChars: number
  silenceMs: number
  mode: Mode
  /** Lower this chat's reasoning effort to "low" during a call, restore it on hang-up. */
  fast: boolean
  /** Auto mode: how long with nobody talking before the collected words are sent. */
  autoSendMs: number
  hotkey: HotkeyId
}
export type HotkeyId = 'ctrl+backquote' | 'ctrl+alt+space' | 'f8' | 'alt+space'
export const HOTKEYS: Record<HotkeyId, { label: string; code: string; ctrl?: boolean; alt?: boolean }> = {
  'ctrl+backquote': { label: '⌃`（Control + 反引号，Esc 下面那个键）', code: 'Backquote', ctrl: true },
  'ctrl+alt+space': { label: '⌃⌥空格', code: 'Space', ctrl: true, alt: true },
  f8: { label: 'F8', code: 'F8' },
  'alt+space': { label: '⌥空格（会和豆包输入法冲突）', code: 'Space', alt: true },
}
export function hotkeyLabel(): string {
  return HOTKEYS[loadSettings().hotkey]?.label.split('（')[0] ?? '⌃`'
}
const SETTINGS_KEY = 'voice-call.settings.v6'
const DEFAULTS: Settings = { voice: 'polly', rate: 1.15, maxSpokenChars: 500, silenceMs: 1300, mode: 'manual', fast: true, autoSendMs: 5000, hotkey: 'ctrl+backquote' }
export function loadSettings(): Settings {
  try {
    const s = { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') as Partial<Settings>) }
    if (s.voice === 'me') s.voice = 'polly'
    return s
  } catch {
    return { ...DEFAULTS }
  }
}
export const SETTINGS_EVENT = 'voice-call-settings'
export function saveSettings(s: Settings): void {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s))
  // Same-tab listeners (a live call) pick changes up at once; 'storage' only fires in other tabs.
  window.dispatchEvent(new Event(SETTINGS_EVENT))
}
export function patchSettings(p: Partial<Settings>): Settings {
  const s = { ...loadSettings(), ...p }
  saveSettings(s)
  return s
}

export function slotFromSessionKey(sessionKey: string): string {
  return sessionKey.startsWith('dashboard:') ? sessionKey.slice('dashboard:'.length) : sessionKey
}

// ---- text helpers ----
function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) return content.map(textOf).join('')
  if (content && typeof content === 'object' && 'text' in content) return textOf((content as { text: unknown }).text)
  return ''
}

export function forSpeech(text: string): string {
  return text
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/^\s*\[OPTIONS:.*\]\s*$/gm, '')
    .replace(/<mcwidget[\s\S]*?<\/mcwidget>/g, '（图在屏幕上）')
    .replace(/```[\s\S]*?```/g, '（代码在屏幕上）')
    .replace(/^\s*\|.*\|\s*$/gm, '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, '（链接在屏幕上）')
    .replace(/`([^`]*)`/g, (_m, s: string) => (s.includes('/') ? '（路径在屏幕上）' : s))
    .replace(/[*_#<>]/g, '')
    .replace(/^\s*[-•]\s+/gm, '')
    .replace(/\n{2,}/g, '\n')
    .trim()
}

/** Whisper's well-known hallucinations on silence or noise. */
const JUNK = /^(谢谢(大家|观看|收看)?[。!！]*|字幕.*|请不吝点赞.*|thank you\.?|thanks for watching.*|you|\.+|。+)$/i
const FILLER = /^[嗯啊哦呃唔哼哈嘿喂诶欸额噢呀啦吧了，。！？!?,.~…\s]+$/
/** Auto mode only keeps real words: at least 2 Chinese characters or 2 Latin words, not just fillers. */
export function isSpeech(t: string): boolean {
  const s = t.trim()
  if (FILLER.test(s)) return false
  const cjk = (s.match(/[\u4e00-\u9fff]/g) ?? []).length
  const words = (s.match(/[A-Za-z]{2,}/g) ?? []).length
  return cjk >= 2 || words >= 2
}
export function isJunk(t: string): boolean {
  const s = t.trim()
  return s.length === 0 || JUNK.test(s)
}

export function splitSentences(text: string): string[] {
  const parts = text.split(/(?<=[。！？!?；;\n])/)
  const out: string[] = []
  for (const p of parts) {
    const s = p.trim()
    if (!s) continue
    // Very long runs without punctuation: cut on commas so speech starts sooner.
    if (s.length > 80) out.push(...s.split(/(?<=[，,])/).map((x) => x.trim()).filter(Boolean))
    else out.push(s)
  }
  return out
}

// ---- WAV encoding (16 kHz mono PCM16) ----
function downsample(buf: Float32Array, from: number, to: number): Float32Array {
  if (from === to) return buf
  const ratio = from / to
  const out = new Float32Array(Math.floor(buf.length / ratio))
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio)
    const end = Math.min(buf.length, Math.floor((i + 1) * ratio))
    let sum = 0
    for (let j = start; j < end; j++) sum += buf[j]
    out[i] = sum / Math.max(1, end - start)
  }
  return out
}
export function encodeWav(chunks: Float32Array[], sampleRate: number): Blob {
  const total = chunks.reduce((n, c) => n + c.length, 0)
  const merged = new Float32Array(total)
  let o = 0
  for (const c of chunks) {
    merged.set(c, o)
    o += c.length
  }
  const pcm = downsample(merged, sampleRate, 16000)
  const view = new DataView(new ArrayBuffer(44 + pcm.length * 2))
  const w = (off: number, s: string) => [...s].forEach((ch, i) => view.setUint8(off + i, ch.charCodeAt(0)))
  w(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); w(8, 'WAVE'); w(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  w(36, 'data'); view.setUint32(40, pcm.length * 2, true)
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]))
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([view], { type: 'audio/wav' })
}

// ---- voices ----
export function chineseVoices(): SpeechSynthesisVoice[] {
  if (!('speechSynthesis' in window)) return []
  return window.speechSynthesis.getVoices().filter((v) => /^zh[-_](CN|TW|HK)/i.test(v.lang) || /^cmn/i.test(v.lang))
}
function pickVoice(name: string): SpeechSynthesisVoice | null {
  const all = window.speechSynthesis.getVoices()
  if (name) {
    // Prefer an enhanced/premium download of the same voice when the user has one.
    const same = all.filter((x) => x.name === name || x.name.startsWith(name + ' ('))
    const best = same.find((x) => /premium|高级|高品质/i.test(x.name)) ?? same.find((x) => /enhanced|增强|优化/i.test(x.name)) ?? same[0]
    if (best) return best
  }
  const zh = chineseVoices()
  return zh.find((v) => /Tingting/i.test(v.name)) ?? zh.find((v) => v.localService) ?? zh[0] ?? null
}

// ---- tuning ----
const FRAME = 2048
const MIN_SPEECH_MS = 350
const BARGE_MS = 300
// One upload must stay under the sandbox transport's 1 MiB request limit:
// 16 kHz mono PCM16 is 32 KB/s, so a 25 s segment is about 800 KB.
const MAX_UTTER_MS = 25_000
const PREROLL_FRAMES = 6
const POLL_MS = 1200

interface SlotMessage { role?: string; content?: unknown }
interface SlotDetail { running?: boolean; stopping?: boolean; total?: number; messages?: SlotMessage[]; queue?: unknown[] }

type Listener = () => void

export class VoiceCall {
  state: CallState = 'idle'
  lines: Line[] = []
  note = ''
  muted = false
  level = 0
  draft = ''
  mode: Mode = 'manual'
  fast = true
  private capturing = false
  private listeners = new Set<Listener>()
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private proc: ScriptProcessorNode | null = null
  private src: MediaStreamAudioSourceNode | null = null
  private preroll: Float32Array[] = []
  private utter: Float32Array[] = []
  private inSpeech = false
  private speechMs = 0
  private silenceMs = 0
  private noise = 0.01
  private bargeMs = 0
  private pollTimer: number | null = null
  private lastTotal = 0
  private waiting = false
  private speakQueue: string[] = []
  private speaking = false
  private pending = new Set<Promise<void>>()
  private settings: Settings = loadSettings()
  /** Effort the chat had before the call lowered it; null = we did not change it. */
  private prevEffort: string | null = null
  private autoTimer: number | null = null
  private lastVoiceAt = 0
  /** Keeps a phone's screen on during a call; the mic stops when the page sleeps. */
  private wakeLock: { release(): Promise<void> } | null = null
  /** One element for every reply clip: a phone only lets an element play after a tap unlocked it. */
  private player: HTMLAudioElement | null = null
  private onVisibility = (): void => void this.visibilityChanged()

  constructor(private api: AppApi, readonly slot: string) {
    const reload = () => this.onSettingsChanged()
    window.addEventListener(SETTINGS_EVENT, reload)
    window.addEventListener('storage', reload)
  }

  /** Apply settings edited on the app page to a call that is already running. */
  private onSettingsChanged(): void {
    this.settings = loadSettings()
    if (!this.running) return
    if (this.settings.mode !== this.mode) {
      this.mode = this.settings.mode
      this.clearAuto()
      this.capturing = false
      this.draft = ''
      this.resetUtterance()
    }
    if (this.settings.fast !== this.fast) {
      this.fast = this.settings.fast
      void this.applyFast(this.fast)
    }
    this.settle()
  }

  get running(): boolean {
    return this.state !== 'idle' && this.state !== 'error'
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }
  private emit(): void {
    this.listeners.forEach((f) => f())
  }
  private set(state: CallState, note = ''): void {
    this.state = state
    this.note = note
    this.emit()
  }
  private add(line: Line): void {
    this.lines = [...this.lines.slice(-30), line]
    this.emit()
  }

  /** A short message in the bar for a few seconds (errors, dropped audio). */
  notice = ''
  private noticeTimer: number | null = null
  private flash(msg: string): void {
    this.notice = msg
    this.add({ who: 'note', text: msg })
    if (this.noticeTimer !== null) window.clearTimeout(this.noticeTimer)
    this.noticeTimer = window.setTimeout(() => { this.notice = ''; this.emit() }, 6000)
  }

  /** State to show when nothing in flight. */
  private settle(): void {
    if (!this.running) return
    if (this.capturing) this.set('recording')
    else if (this.speaking) this.set('speaking')
    else if (this.inSpeech) this.set('hearing')
    else if (this.pending.size > 0) this.set('transcribing')
    else if (this.waiting) this.set('thinking')
    else this.set(this.mode === 'manual' ? 'ready' : 'listening')
  }

  async start(): Promise<void> {
    if (this.running) return
    this.settings = loadSettings()
    this.mode = this.settings.mode
    this.fast = this.settings.fast
    this.draft = ''
    this.set('starting')
    try {
      void this.api.post('/api/stt/prewarm', {}).catch(() => undefined)
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      })
      this.ctx = new AudioContext()
      void this.ctx.resume().catch(() => undefined)
      this.unlockAudio()
      void this.keepAwake()
      document.addEventListener('visibilitychange', this.onVisibility)
      this.src = this.ctx.createMediaStreamSource(this.stream)
      this.proc = this.ctx.createScriptProcessor(FRAME, 1, 1)
      this.proc.onaudioprocess = (e) => this.onFrame(e.inputBuffer.getChannelData(0))
      this.src.connect(this.proc)
      // ScriptProcessor only runs while connected to the destination; output is silent.
      const silent = this.ctx.createGain()
      silent.gain.value = 0
      this.proc.connect(silent).connect(this.ctx.destination)
      const d = await this.detail(1)
      this.lastTotal = d.total ?? 0
      this.waiting = !!d.running
      this.pollTimer = window.setInterval(() => void this.poll(), POLL_MS)
      if (this.fast) void this.applyFast(true)
      this.say('我在。')
      this.settle()
    } catch (e) {
      this.teardown()
      const msg = (e as Error)?.name === 'NotAllowedError' ? '麦克风被拒绝了：请在浏览器或系统设置里允许麦克风' : `打不开麦克风：${(e as Error)?.message ?? e}`
      this.set('error', msg)
    }
  }

  stop(): void {
    this.teardown()
    this.set('idle')
  }

  toggleMute(): void {
    this.muted = !this.muted
    if (this.muted) this.resetUtterance()
    this.emit()
  }

  setMode(mode: Mode): void {
    patchSettings({ mode })
  }

  setFast(on: boolean): void {
    patchSettings({ fast: on })
  }

  // ---- manual talk: tap to start, tap to send ----
  beginTalk(): void {
    if (!this.running || this.mode !== 'manual') return
    this.unlockAudio()
    this.cancelSpeech()
    this.capturing = true
    this.draft = ''
    this.resetUtterance()
    this.settle()
  }

  cancelTalk(): void {
    this.capturing = false
    this.draft = ''
    this.resetUtterance()
    this.settle()
  }

  async sendTalk(): Promise<void> {
    if (!this.capturing) return
    this.capturing = false
    // Flush whatever is still being said.
    if (this.inSpeech && this.ctx && this.speechMs >= 150) this.track(this.transcribe(this.utter, this.ctx.sampleRate))
    this.resetUtterance()
    this.settle()
    await Promise.allSettled([...this.pending])
    const text = this.draft.trim()
    this.draft = ''
    if (!text) {
      this.flash('没听到内容，请再说一遍')
      this.settle()
      return
    }
    this.add({ who: 'you', text })
    await this.sendSafely(text)
  }

  /** Sending can fail (sandbox reconnecting): say so in the bar instead of going silent. */
  private async sendSafely(text: string): Promise<void> {
    try {
      await this.send(text)
    } catch (e) {
      this.draft = text
      this.flash(`没发出去：${(e as Error).message}，请再试一次`)
      this.settle()
    }
  }

  /** ⌥Space: start in manual mode, send on the second press. */
  hotkey(): void {
    if (!this.running || this.mode !== 'manual') return
    if (this.capturing) void this.sendTalk()
    else this.beginTalk()
  }

  private teardown(): void {
    if (this.pollTimer !== null) window.clearInterval(this.pollTimer)
    this.pollTimer = null
    this.clearAuto()
    this.cancelSpeech()
    try { this.proc?.disconnect(); this.src?.disconnect() } catch { /* already gone */ }
    this.stream?.getTracks().forEach((t) => t.stop())
    void this.ctx?.close().catch(() => undefined)
    this.ctx = null
    this.stream = null
    this.proc = null
    this.src = null
    this.capturing = false
    this.draft = ''
    this.resetUtterance()
    this.waiting = false
    document.removeEventListener('visibilitychange', this.onVisibility)
    void this.wakeLock?.release().catch(() => undefined)
    this.wakeLock = null
    if (this.prevEffort !== null) void this.applyFast(false)
  }

  // ---- phone: screen, background, audio unlock ----
  private async keepAwake(): Promise<void> {
    try {
      const wl = (navigator as Navigator & { wakeLock?: { request(type: 'screen'): Promise<{ release(): Promise<void> }> } }).wakeLock
      if (wl && document.visibilityState === 'visible') this.wakeLock = await wl.request('screen')
    } catch {
      // Not supported or refused (battery saver): the note in the bar still applies.
    }
  }

  private async visibilityChanged(): Promise<void> {
    if (!this.running) return
    if (document.visibilityState === 'hidden') {
      // The phone is about to stop the mic. Drop a half-recorded segment rather than send noise.
      if (this.capturing) this.cancelTalk()
      return
    }
    void this.keepAwake()
    void this.ctx?.resume().catch(() => undefined)
    const ended = this.stream?.getAudioTracks().some((t) => t.readyState === 'ended') ?? true
    if (ended) {
      // The system took the mic away while we were in the background: reopen it.
      this.teardown()
      this.set('idle')
      await this.start()
      this.flash('页面回到前台，麦克风已重新打开')
    }
  }

  /** Call from a tap or press: lets later replies play on phones that block autoplay. */
  unlockAudio(): void {
    if (!this.player) this.player = new Audio()
    const a = this.player
    // A one-millisecond silent WAV, played inside the user gesture.
    a.src = 'data:audio/wav;base64,UklGRjQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YRAAAAAAAAAAAAAAAAAAAAAAAAAA'
    void a.play().then(() => a.pause()).catch(() => undefined)
    void this.ctx?.resume().catch(() => undefined)
  }

  private resetUtterance(): void {
    this.utter = []
    this.inSpeech = false
    this.speechMs = 0
    this.silenceMs = 0
    this.bargeMs = 0
  }

  // ---- reasoning effort ----
  private async applyFast(on: boolean): Promise<void> {
    try {
      if (on) {
        if (this.prevEffort !== null) return
        const data = await this.api.get<unknown>('/api/chat/slots')
        const rows = (Array.isArray(data) ? data : (data as { slots?: unknown[] })?.slots ?? []) as Array<{ key?: string; name?: string; reasoning_effort?: string }>
        const row = rows.find((r) => (r.key ?? r.name) === this.slot)
        const current = row?.reasoning_effort ?? ''
        if (current === 'low') return
        await this.api.post(`/api/chat/slots/${encodeURIComponent(this.slot)}/reasoning-effort`, { reasoning_effort: 'low' })
        this.prevEffort = current
      } else if (this.prevEffort !== null) {
        const restore = this.prevEffort
        this.prevEffort = null
        await this.api.post(`/api/chat/slots/${encodeURIComponent(this.slot)}/reasoning-effort`, { reasoning_effort: restore })
      }
    } catch (e) {
      this.add({ who: 'note', text: `调推理强度没成功：${(e as Error).message}` })
    }
  }

  // ---- voice activity detection ----
  private onFrame(input: Float32Array): void {
    if (!this.ctx) return
    const frame = new Float32Array(input)
    const frameMs = (frame.length / this.ctx.sampleRate) * 1000
    let sum = 0
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i]
    const rms = Math.sqrt(sum / frame.length)
    this.level = rms
    if (this.muted) return
    // Manual mode hears nothing until the user taps 「说」.
    if (this.mode === 'manual' && !this.capturing) {
      if (!this.inSpeech) this.noise = this.noise * 0.95 + rms * 0.05
      return
    }

    // While we speak (auto mode), echo cancellation removes most of our own
    // voice; demand a clearly louder, sustained signal before treating it as an
    // interruption.
    const base = Math.max(0.012, this.noise * 3)
    const threshold = this.speaking ? base * 2.5 : base
    const loud = rms > threshold
    if (!loud && !this.inSpeech) this.noise = this.noise * 0.95 + rms * 0.05

    if (!this.inSpeech) {
      this.preroll.push(frame)
      if (this.preroll.length > PREROLL_FRAMES) this.preroll.shift()
      if (loud) {
        this.bargeMs += frameMs
        const need = this.speaking ? BARGE_MS : 120
        if (this.bargeMs >= need) {
          if (this.speaking) this.cancelSpeech()
          this.clearAuto()
          this.inSpeech = true
          this.utter = [...this.preroll]
          this.preroll = []
          this.speechMs = this.bargeMs
          this.silenceMs = 0
          this.settle()
        }
      } else {
        this.bargeMs = 0
      }
      return
    }

    this.utter.push(frame)
    if (loud) {
      this.lastVoiceAt = Date.now()
      this.speechMs += frameMs
      this.silenceMs = 0
    } else {
      this.silenceMs += frameMs
    }
    const total = this.utter.length * frameMs
    // In manual mode a pause only cuts a segment (transcribed into the draft
    // early, so 「发送」 is quick); it never sends.
    if (this.silenceMs >= this.settings.silenceMs || total >= MAX_UTTER_MS) {
      const chunks = this.utter
      const spoke = this.speechMs
      const rate = this.ctx.sampleRate
      this.resetUtterance()
      if (spoke >= (this.mode === 'auto' ? 600 : MIN_SPEECH_MS)) this.track(this.transcribe(chunks, rate))
      this.settle()
    }
  }

  private track(p: Promise<void>): void {
    this.pending.add(p)
    this.settle()
    void p.finally(() => {
      this.pending.delete(p)
      this.settle()
    })
  }

  /**
   * Recognize one WAV segment. The app's own backend (Amazon Transcribe) goes
   * first because it takes PCM from every browser; if it fails once, the bar
   * says so and the rest of the call uses Kiro Crew's recognizer.
   */
  private appSttFailed = false
  private async recognize(wav: Blob): Promise<string> {
    if (!this.appSttFailed) {
      try {
        const res = await this.api.raw('/apps/voice-call/api/stt', {
          method: 'POST',
          headers: { 'Content-Type': 'audio/wav' },
          body: wav,
        })
        const body = (await res.json().catch(() => ({}))) as { text?: string; error?: string }
        if (res.ok) return body.text ?? ''
        throw new Error(body.error || `HTTP ${res.status}`)
      } catch (e) {
        this.appSttFailed = true
        this.flash(`Transcribe 不可用（${(e as Error).message}），改用 Kiro Crew 识别`)
      }
    }
    const form = new FormData()
    form.append('audio', wav, 'voice-call.wav')
    const res = await this.api.raw('/api/stt/transcribe', { method: 'POST', body: form })
    const body = (await res.json().catch(() => ({}))) as { text?: string; error?: string }
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
    return body.text ?? ''
  }

  /** Transcribe one segment: manual mode appends to the draft, auto mode sends it. */
  private async transcribe(chunks: Float32Array[], rate: number): Promise<void> {
    const sendNow = this.mode === 'auto'
    try {
      const text = toSimplified((await this.recognize(encodeWav(chunks, rate))).trim())
      if (!this.running) return
      if (isJunk(text)) { this.flash('没听清，请再说一遍'); return }
      // Birds, keyboards, a cough: in auto mode keep only real words.
      if (sendNow && !isSpeech(text)) { this.flash(`只听到「${text}」，没发出去`); return }
      this.draft = this.draft ? `${this.draft}${/[\s，。！？,.!?]$/.test(this.draft) ? '' : '，'}${text}` : text
      this.emit()
      if (sendNow) this.armAuto()
    } catch (e) {
      // Shown in the bar itself, so a failed segment is never silent.
      this.flash(`没识别出来：${(e as Error).message}，请再说一遍`)
    }
  }

  // ---- auto mode: send only after a long quiet spell ----
  private clearAuto(): void {
    if (this.autoTimer !== null) window.clearTimeout(this.autoTimer)
    this.autoTimer = null
  }

  private armAuto(): void {
    this.clearAuto()
    const wait = Math.max(500, this.settings.autoSendMs - (Date.now() - this.lastVoiceAt))
    this.autoTimer = window.setTimeout(() => void this.flushAuto(), wait)
  }

  private async flushAuto(): Promise<void> {
    this.autoTimer = null
    if (!this.running || this.mode !== 'auto' || this.inSpeech) return
    if (this.pending.size > 0) { this.armAuto(); return }
    const text = this.draft.trim()
    this.draft = ''
    this.emit()
    if (!text) return
    this.add({ who: 'you', text })
    await this.sendSafely(text)
  }

  // ---- chat bridge ----
  private detail(limit = 30): Promise<SlotDetail> {
    return this.api.get<SlotDetail>(`/api/chat/slots/${encodeURIComponent(this.slot)}?limit=${limit}`)
  }

  private async send(text: string): Promise<void> {
    // Mid-turn messages are fine: Kiro Crew steers or queues them like typed ones.
    await this.api.post('/api/chat?ws=1', { message: VOICE_PREFIX + text + SPEECH_HINT, slot: this.slot })
    this.waiting = true
    this.beep()
    this.settle()
  }

  private async poll(): Promise<void> {
    if (!this.running) return
    let d: SlotDetail
    try {
      d = await this.detail(30)
    } catch {
      return
    }
    const total = d.total ?? 0
    const idle = !d.running && !d.stopping && (d.queue?.length ?? 0) === 0
    if (!idle) {
      if (!this.waiting) { this.waiting = true; this.settle() }
      return
    }
    if (total > this.lastTotal) {
      const msgs = d.messages ?? []
      const fresh = msgs.slice(-Math.min(total - this.lastTotal, msgs.length))
      this.lastTotal = total
      const answer = fresh.filter((m) => m.role === 'assistant').map((m) => textOf(m.content)).join('\n')
      let spoken = forSpeech(answer)
      if (spoken) {
        if (spoken.length > this.settings.maxSpokenChars) {
          const cut = spoken.slice(0, this.settings.maxSpokenChars)
          const end = Math.max(cut.lastIndexOf('。'), cut.lastIndexOf('！'), cut.lastIndexOf('？'))
          spoken = (end > 40 ? cut.slice(0, end + 1) : cut) + '后面的内容在屏幕上。'
        }
        this.add({ who: 'kiro', text: spoken })
        // Don't talk over the user while they are recording.
        if (!this.capturing) this.say(spoken)
      }
    }
    if (this.waiting) { this.waiting = false; this.settle() }
  }

  // ---- speech out ----
  // voice === 'polly': Amazon Polly Zhiyu via the app backend.
  // Sentences are synthesized one after another while earlier ones play, so
  // after the first sentence speech runs roughly without gaps. Any failure
  // falls back to the OS voice for that sentence.
  private gen = 0
  private audio: HTMLAudioElement | null = null
  private synthChain: Promise<unknown> = Promise.resolve()
  private clips: Array<Promise<Blob | null>> = []

  say(text: string): void {
    const parts = splitSentences(text)
    if (this.remote()) {
      for (const s of parts) {
        const g = this.gen
        const clip = this.synthChain.then(() => (g === this.gen ? this.synth(s) : null))
        this.synthChain = clip.catch(() => null)
        this.clips.push(clip.catch(() => null))
      }
    }
    this.speakQueue.push(...parts)
    if (!this.speaking) this.nextSentence()
  }

  private remote(): boolean {
    return this.settings.voice === 'polly'
  }

  private async synth(text: string): Promise<Blob | null> {
    const res = await this.api.raw('/apps/voice-call/api/tts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, voice: this.settings.voice }),
    })
    if (!res.ok) return null
    return res.blob()
  }

  stopSpeaking(): void {
    this.cancelSpeech()
    this.settle()
  }

  private nextSentence(): void {
    const s = this.speakQueue.shift()
    const clip = this.remote() ? this.clips.shift() : undefined
    if (!s || !this.running) {
      this.speaking = false
      this.settle()
      return
    }
    this.speaking = true
    this.settle()
    if (clip) {
      const g = this.gen
      void clip.then((blob) => {
        if (g !== this.gen) return
        if (!blob) return this.speakOs(s)
        const url = URL.createObjectURL(blob)
        const a = this.player ?? new Audio()
        this.player = a
        a.src = url
        this.audio = a
        a.playbackRate = Math.max(0.8, Math.min(1.5, this.settings.rate / 1.15))
        ;(a as HTMLAudioElement & { preservesPitch?: boolean }).preservesPitch = true
        const done = () => { URL.revokeObjectURL(url); if (g === this.gen) this.nextSentence() }
        a.onended = done
        a.onerror = () => { URL.revokeObjectURL(url); if (g === this.gen) this.speakOs(s) }
        void a.play().catch(() => this.speakOs(s))
      })
      return
    }
    this.speakOs(s)
  }

  private speakOs(s: string): void {
    if (!('speechSynthesis' in window)) return this.nextSentence()
    const u = new SpeechSynthesisUtterance(s)
    const v = pickVoice(this.remote() ? 'Tingting' : this.settings.voice)
    if (v) { u.voice = v; u.lang = v.lang } else u.lang = 'zh-CN'
    u.rate = this.settings.rate
    u.onend = () => this.nextSentence()
    u.onerror = () => this.nextSentence()
    window.speechSynthesis.speak(u)
  }

  private cancelSpeech(): void {
    this.gen++
    this.speakQueue = []
    this.clips = []
    this.synthChain = Promise.resolve()
    this.speaking = false
    if (this.audio) { this.audio.pause(); this.audio = null }
    if ('speechSynthesis' in window) window.speechSynthesis.cancel()
  }

  private beep(): void {
    if (!this.ctx) return
    const o = this.ctx.createOscillator()
    const g = this.ctx.createGain()
    o.frequency.value = 880
    g.gain.setValueAtTime(0.0001, this.ctx.currentTime)
    g.gain.exponentialRampToValueAtTime(0.08, this.ctx.currentTime + 0.02)
    g.gain.exponentialRampToValueAtTime(0.0001, this.ctx.currentTime + 0.15)
    o.connect(g).connect(this.ctx.destination)
    o.start()
    o.stop(this.ctx.currentTime + 0.16)
  }
}

// One call per chat slot, shared across popover mounts; one live call at a time.
const calls = new Map<string, VoiceCall>()
export function callFor(api: AppApi, slot: string): VoiceCall {
  let c = calls.get(slot)
  if (!c) {
    c = new VoiceCall(api, slot)
    calls.set(slot, c)
  }
  return c
}
export function stopOthers(slot: string): void {
  calls.forEach((c, k) => { if (k !== slot && c.running) c.stop() })
}
export function liveCall(): VoiceCall | null {
  for (const c of calls.values()) if (c.running) return c
  return null
}

// ⌥Space anywhere on the page drives the live call, the way voice input in
// Doubao IME / Typeless-style tools works with one key:
//   short press  -> start; short press again -> send
//   hold (>0.5s) -> talk while held; release -> send
//   Esc while recording -> discard
const HOLD_MS = 500
let hotkeyBound = false
let downAt = 0
let startedByThisPress = false
export function bindHotkey(): void {
  if (hotkeyBound) return
  hotkeyBound = true
  window.addEventListener('keydown', (e) => {
    const c = liveCall()
    if (!c || c.mode !== 'manual') return
    if (e.code === 'Escape' && c.state === 'recording') {
      e.preventDefault()
      c.cancelTalk()
      return
    }
    const k = HOTKEYS[loadSettings().hotkey] ?? HOTKEYS['ctrl+backquote']
    if (e.code === k.code && !!k.ctrl === e.ctrlKey && !!k.alt === e.altKey && !e.metaKey) {
      e.preventDefault()
      if (e.repeat) return
      downAt = Date.now()
      if (c.state === 'recording') {
        startedByThisPress = false
        void c.sendTalk()
      } else {
        startedByThisPress = true
        c.beginTalk()
      }
    }
  }, true)
  window.addEventListener('keyup', (e) => {
    const k = HOTKEYS[loadSettings().hotkey] ?? HOTKEYS['ctrl+backquote']
    if (e.code !== k.code && e.key !== 'Alt' && e.key !== 'Control') return
    const c = liveCall()
    if (!c || !startedByThisPress) return
    startedByThisPress = false
    // Held long enough: push-to-talk, so letting go sends.
    if (Date.now() - downAt >= HOLD_MS && c.state === 'recording') void c.sendTalk()
  }, true)
}
