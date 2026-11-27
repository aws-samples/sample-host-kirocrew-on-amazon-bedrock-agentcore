// Pure text rules for the call: what to read aloud, what counts as speech,
// what is a speed command. No browser APIs, so tests run anywhere.

export interface ScreenWords { image: string; code: string; link: string; path: string }

const ZH_SCREEN: ScreenWords = { image: '（图在屏幕上）', code: '（代码在屏幕上）', link: '（链接在屏幕上）', path: '（路径在屏幕上）' }

export function forSpeech(text: string, w: ScreenWords = ZH_SCREEN): string {
  // Strip HTML comments until none are left, so nested or split markers
  // cannot leave a fresh "<!--" behind.
  let plain = text
  for (let prev = ''; prev !== plain; ) {
    prev = plain
    plain = plain.replace(/<!--[\s\S]*?-->/g, '')
  }
  return plain
    .replace(/^\s*\[OPTIONS:.*\]\s*$/gm, '')
    .replace(/<mcwidget[\s\S]*?<\/mcwidget>/g, w.image)
    .replace(/```[\s\S]*?```/g, w.code)
    .replace(/^\s*\|.*\|\s*$/gm, '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, w.link)
    .replace(/`([^`]*)`/g, (_m, s: string) => (s.includes('/') ? w.path : s))
    .replace(/[*_#<>]/g, '')
    .replace(/^\s*[-•]\s+/gm, '')
    .replace(/\n{2,}/g, '\n')
    .trim()
}

/**
 * Text a recognizer makes up from silence or noise: video-subtitle credits and
 * one-word ghosts. Matched against the whole result, after trimming.
 */
const JUNK = new RegExp(
  '^(' + [
    '谢谢(大家|观看|收看)?[。!！]*',
    '字幕.*',
    '请不吝点赞.*',
    '.*独播剧场.*',
    '.*YoYo Television.*',
    '.*(Amara\\.org|Sous-titrage|ご視聴ありがとうございました).*',
    'thank you\\.?',
    'thanks for watching.*',
    'terima kasih[.!]?( terima kasih[.!]?)*',
    'you',
    '\\.+',
    '。+',
  ].join('|') + ')$',
  'i',
)

/** Only fillers, hums and punctuation: 嗯, 啊, hmm, uh, ah, NNN… */
const FILLER = /^[嗯啊哦呃唔哼哈嘿喂诶欸额噢呀啦吧了呵哎唉呜嘛恩昂嗷nmhaoeuNMHAOEU，。！？!?,.~…\-\s]+$/

export function isJunk(t: string): boolean {
  const s = t.trim()
  return s.length === 0 || JUNK.test(s)
}

/**
 * Real words, not just fillers. Any CJK character, kana or hangul counts, and
 * any Latin word of two letters or more, so a one-word answer ("行", "Sí",
 * "OK") still goes through.
 */
export function isSpeech(t: string): boolean {
  const s = t.trim()
  if (!s || FILLER.test(s)) return false
  if (/[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0600-\u06ff]/.test(s)) return true
  return /\p{L}{2,}/u.test(s)
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

export type RateCommand = 'slower' | 'faster' | 'reset'

/** 「说慢一点」「太快了」「正常语速」 / "slower please": a short utterance that only adjusts speed. */
export function rateCommand(t: string): RateCommand | null {
  const s = t.replace(/[\s，。！？,.!?~…]/g, '')
  if (!s || s.length > 16) return null
  if (/(正常|恢复|原来|默认)(的)?(语速|速度)|^normalspeed$|^resetspeed$/i.test(s)) return 'reset'
  if (/^(你)?(说|讲|念|读)?(得)?(慢|满)(一)?(点|些)(说|讲|念|读)?(吧|啊|呀|好吗)?$|^(你)?(说|讲|念|读)(得)?太快(了)?|^太快了$|^(speak|talk)?slower(please)?$|^slowdown(please)?$/i.test(s)) return 'slower'
  if (/^(你)?(说|讲|念|读)(得)?快(一)?(点|些)(吧|啊|呀|好吗)?$|^快(一)?(点|些)(说|讲|念|读)$|^(你)?(说|讲|念|读)(得)?太慢(了)?|^(speak|talk)?faster(please)?$|^speedup(please)?$/i.test(s)) return 'faster'
  return null
}

/** Next speed after a command: 15% steps, 0.6×–1.6×. */
export function nextRate(cur: number, cmd: RateCommand): number {
  if (cmd === 'reset') return 1
  const v = Math.min(1.6, Math.max(0.6, cur * (cmd === 'slower' ? 0.85 : 1.15)))
  return Math.round(v * 100) / 100
}

/**
 * When to send in auto mode: autoSendMs after the last recognized text, never
 * held up by noise; while a recognition is still running, wait up to 3 s more.
 * Returns ms to wait, or 0 to send now.
 */
export function autoSendDelay(now: number, lastTextAt: number, autoSendMs: number, pending: boolean): number {
  const due = lastTextAt + autoSendMs
  if (now < due) return Math.max(500, due - now)
  if (pending && now < due + 3000) return 400
  return 0
}
