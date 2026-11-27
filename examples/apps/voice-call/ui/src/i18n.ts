// UI strings in Chinese and English. The language follows the browser
// (navigator.language), and the settings page can override it.

export type UiLang = 'zh' | 'en'

const STORE = 'voice-call.ui-lang'

export function uiLang(): UiLang {
  try {
    const saved = localStorage.getItem(STORE)
    if (saved === 'zh' || saved === 'en') return saved
  } catch { /* private mode */ }
  const nav = typeof navigator !== 'undefined' ? navigator.language || '' : ''
  return /^zh/i.test(nav) ? 'zh' : 'en'
}
export function setUiLang(l: UiLang): void {
  try { localStorage.setItem(STORE, l) } catch { /* private mode */ }
}

/** The Transcribe language to recognize by default: the UI language. */
export function defaultSttLanguages(l: UiLang = uiLang()): string {
  return l === 'zh' ? 'zh-CN' : 'en-US'
}

const zh = {
  'state.idle': '点一下开始通话',
  'state.starting': '正在打开麦克风…',
  'state.ready': '按住说话，松开发送',
  'state.listening': '在听，直接说',
  'state.hearing': '在听你说…',
  'state.recording': '在录…再按一次发送',
  'state.transcribing': '在识别…',
  'state.thinking': '在想…',
  'state.speaking': '在说…',
  'state.error': '出错了',
  hint: '（语音通话：用口语简短回答，先说结论，两三句话；表格、代码、链接放在后面，我看屏幕。）',
  'screen.image': '（图在屏幕上）',
  'screen.code': '（代码在屏幕上）',
  'screen.link': '（链接在屏幕上）',
  'screen.path': '（路径在屏幕上）',
  'screen.more': '后面的内容在屏幕上。',
  greet: '我在。',
  'mic.denied': '麦克风被拒绝了：请在浏览器或系统设置里允许麦克风',
  'mic.notFound': '找不到麦克风：请在系统设置里给浏览器打开麦克风权限，或者插上麦克风',
  'mic.failed': '打不开麦克风：{0}',
  'mic.back': '页面回到前台，麦克风已重新打开',
  'heard.nothing': '没听到内容，请再说一遍',
  'heard.unclear': '没听清，请再说一遍',
  'heard.filler': '只听到「{0}」，没发出去',
  'heard.failed': '没识别出来：{0}，请再说一遍',
  'send.failed': '没发出去：{0}，请再试一次',
  'stt.fallback': 'Transcribe 不可用（{0}），改用 Kiro Crew 识别',
  'tts.fallback': 'Polly 不可用（{0}），改用浏览器自带的声音',
  'effort.failed': '调推理强度没成功：{0}',
  'rate.set': '{0}语速调成 {1} 倍',
  'rate.edge': '（已经到头了）',
  'hold.idle': '按住说话',
  'hold.recording': '松开发送',
  'hold.keepFront': '请保持本页在前台、屏幕常亮：切到后台时手机会停掉麦克风。',
  'phone.ready': '按住下面的按钮说话，松开发送',
  'phone.recording': '在听… 松开发送，滑出按钮不发',
  'phone.speaking': '在说… 按住按钮就能打断',
  'desk.ready': '点「说」或按 {0} 开始',
  'desk.recording': '在听… 点「发送」或再按一次 {0} 发出，Esc 不发',
  'desk.speaking': '在说… 按 {0} 打断',
  talk: '说',
  send: '发送',
  hangup: '挂断',
  close: '关闭',
} as const

export type Key = keyof typeof zh

const en: Record<Key, string> = {
  'state.idle': 'Tap to start a call',
  'state.starting': 'Opening the microphone…',
  'state.ready': 'Hold to talk, release to send',
  'state.listening': 'Listening, just talk',
  'state.hearing': 'Hearing you…',
  'state.recording': 'Recording… press again to send',
  'state.transcribing': 'Recognizing…',
  'state.thinking': 'Thinking…',
  'state.speaking': 'Speaking…',
  'state.error': 'Something went wrong',
  hint: '(Voice call: answer briefly and conversationally, conclusion first, two or three sentences; put tables, code and links after that, I will read the screen.)',
  'screen.image': '(image on screen)',
  'screen.code': '(code on screen)',
  'screen.link': '(link on screen)',
  'screen.path': '(path on screen)',
  'screen.more': 'The rest is on screen.',
  greet: "I'm here.",
  'mic.denied': 'Microphone blocked: allow it in the browser or system settings',
  'mic.notFound': 'No microphone found: allow the browser to use the microphone in system settings, or plug one in',
  'mic.failed': 'Could not open the microphone: {0}',
  'mic.back': 'Page is back in front; the microphone is open again',
  'heard.nothing': 'Heard nothing, please say it again',
  'heard.unclear': 'Did not catch that, please say it again',
  'heard.filler': 'Only heard "{0}", not sent',
  'heard.failed': 'Recognition failed: {0}, please say it again',
  'send.failed': 'Not sent: {0}, please try again',
  'stt.fallback': 'Transcribe unavailable ({0}); using Kiro Crew recognition',
  'tts.fallback': 'Polly unavailable ({0}); using the browser voice',
  'effort.failed': 'Could not change reasoning effort: {0}',
  'rate.set': '{0} speed set to {1}×',
  'rate.edge': ' (limit reached)',
  'hold.idle': 'Hold to talk',
  'hold.recording': 'Release to send',
  'hold.keepFront': 'Keep this page in front with the screen on: phones stop the microphone in the background.',
  'phone.ready': 'Hold the button below to talk, release to send',
  'phone.recording': 'Listening… release to send, slide off to cancel',
  'phone.speaking': 'Speaking… hold the button to interrupt',
  'desk.ready': 'Click "Talk" or press {0} to start',
  'desk.recording': 'Listening… click "Send" or press {0} again, Esc to cancel',
  'desk.speaking': 'Speaking… press {0} to interrupt',
  talk: 'Talk',
  send: 'Send',
  hangup: 'Hang up',
  close: 'Close',
}

const LANG_NAMES: Record<UiLang, Record<string, string>> = {
  zh: { zh: '中文', ja: '日语', ko: '韩语', ar: '阿拉伯语', en: '英语', pt: '葡萄牙语（巴西）', es: '西班牙语', fr: '法语', it: '意大利语', de: '德语' },
  en: { zh: 'Chinese', ja: 'Japanese', ko: 'Korean', ar: 'Arabic', en: 'English', pt: 'Portuguese (Brazil)', es: 'Spanish', fr: 'French', it: 'Italian', de: 'German' },
}

export function langName(code: string, l: UiLang = uiLang()): string {
  return LANG_NAMES[l][code] ?? code
}

export function t(key: Key, ...args: Array<string | number>): string {
  const table: Record<Key, string> = uiLang() === 'zh' ? zh : en
  return table[key].replace(/\{(\d)\}/g, (_m, i: string) => String(args[Number(i)] ?? ''))
}
