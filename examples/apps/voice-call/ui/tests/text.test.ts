import { describe, expect, it } from 'vitest'
import { autoSendDelay, forSpeech, isJunk, isSpeech, nextRate, rateCommand, splitSentences } from '../src/text'

describe('voice-call text rules', () => {
  it('drops fillers and hums in any mode, keeps one-word answers', () => {
    for (const t of ['嗯', '啊啊', '嗯嗯啊啊', 'NNN', 'hmm', 'ah', 'Mm? Mm?', '呵呵。', '...', '']) expect(isSpeech(t), t).toBe(false)
    for (const t of ['行', '好的', 'Sí', 'OK', 'Oui', 'はい', '네', 'Muito bem']) expect(isSpeech(t), t).toBe(true)
  })

  it('drops recognizer hallucinations', () => {
    for (const t of ['谢谢观看', '字幕由 Amara.org 社区提供', '优优独播剧场——YoYo Television Series Exclusive', 'Thanks for watching!', 'you', 'Terima kasih. Terima kasih.', 'ご視聴ありがとうございました', '。。。']) {
      expect(isJunk(t), t).toBe(true)
    }
    for (const t of ['谢谢你帮我查', 'Thank you for the help with the deploy']) expect(isJunk(t), t).toBe(false)
  })

  it('recognizes speed commands but not ordinary requests', () => {
    expect(rateCommand('说慢一点')).toBe('slower')
    expect(rateCommand('你说得太快了')).toBe('slower')
    expect(rateCommand('Slower please')).toBe('slower')
    expect(rateCommand('快点说')).toBe('faster')
    expect(rateCommand('你说太慢了')).toBe('faster')
    expect(rateCommand('正常语速')).toBe('reset')
    expect(rateCommand('Normal speed')).toBe('reset')
    for (const t of ['快点帮我查一下', '慢慢来', '明天快点到', '帮我把这个部署说慢一点的原因查清楚']) expect(rateCommand(t), t).toBeNull()
  })

  it('steps speed by 15% within 0.6x to 1.6x', () => {
    expect(nextRate(1, 'slower')).toBe(0.85)
    expect(nextRate(1, 'faster')).toBe(1.15)
    expect(nextRate(0.6, 'slower')).toBe(0.6)
    expect(nextRate(1.6, 'faster')).toBe(1.6)
    expect(nextRate(0.7, 'reset')).toBe(1)
  })

  it('auto mode sends on the text clock, never held by noise or a stuck recognition', () => {
    expect(autoSendDelay(1000, 1000, 5000, false)).toBe(5000)
    expect(autoSendDelay(5800, 1000, 5000, false)).toBe(500)
    expect(autoSendDelay(6000, 1000, 5000, false)).toBe(0)
    expect(autoSendDelay(6000, 1000, 5000, true)).toBe(400)
    expect(autoSendDelay(9001, 1000, 5000, true)).toBe(0)
  })

  it('reads only what makes sense aloud', () => {
    const out = forSpeech('结论。<!-- x -->\n```js\nx()\n```\n[OPTIONS: a | b]\n| a | b |\n看 [文档](https://x.y) 和 https://a.b/c `src/a.ts` **加粗**')
    expect(out).not.toMatch(/OPTIONS|<!--|```|\||\*\*/)
    expect(out).toContain('（代码在屏幕上）')
    expect(out).toContain('文档')
    expect(out).toContain('（链接在屏幕上）')
    expect(out).toContain('（路径在屏幕上）')
  })

  it('splits sentences and cuts long unpunctuated runs', () => {
    expect(splitSentences('一。二！三？')).toEqual(['一。', '二！', '三？'])
    expect(splitSentences('Hello. Hi!')).toEqual(['Hello. Hi!'])
    expect(splitSentences('a'.repeat(50) + '，' + 'b'.repeat(50))).toHaveLength(2)
  })
})
