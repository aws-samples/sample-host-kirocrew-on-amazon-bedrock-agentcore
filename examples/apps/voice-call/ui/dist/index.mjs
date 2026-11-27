import { jsxs as a, jsx as n, Fragment as P } from "react/jsx-runtime";
import { useAppApi as T } from "@kirocrew/app-sdk";
import { useState as h, useEffect as x } from "react";
import { l as V, u as j, s as E, a as H, H as I, h as M, d as _, c as $, b as B } from "./engine.mjs";
const u = {
  background: "var(--surface, transparent)",
  color: "var(--text)",
  border: "1px solid var(--border)",
  borderRadius: 10,
  padding: 16,
  marginBottom: 14
}, p = { display: "block", fontSize: 13, margin: "10px 0 4px" }, d = {
  background: "var(--bg, transparent)",
  color: "var(--text)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  padding: "4px 8px",
  fontSize: 13
}, y = { fontSize: 12, color: "var(--muted)", margin: "4px 0 8px" }, K = {
  zh: {
    title: "语音通话",
    intro: "在任意会话的输入框下面点「语音通话」，直接跟 Kiro 说话；回复会念给你听，你一开口它就停。手机上按住大按钮说、松开发送；电脑上点「说」或用快捷键。",
    status: "语音服务",
    checking: "正在检查…",
    on: "已接通",
    off: "未接通",
    pollyOff: "回复会改用浏览器自带的声音朗读。",
    sttOff: "识别会改用 Kiro Crew 自带的识别。",
    howToAws: "要用 Amazon 的声音和识别：给运行这个 App 的环境配好 AWS 凭证（本机可设 VOICE_CALL_AWS_PROFILE），需要 polly:SynthesizeSpeech、polly:DescribeVoices、transcribe:StartStreamTranscription 权限。",
    region: "区域",
    voices: "各语言的声音（Amazon Polly）",
    voicesHelp: "回复是哪种语言，就用那种语言选中的声音读。通话里说「说慢一点」「说快一点」「正常语速」，会调刚才那种语言的语速。",
    gender: { f: "女", m: "男" },
    generative: "，生成式",
    speed: "语速",
    reset: "恢复",
    play: "试听",
    playing: "播放中…",
    fallback: "朗读方式",
    usePolly: "Amazon Polly（推荐）",
    useBrowser: "只用浏览器自带的声音（不花钱）",
    maxChars: "一次最多念多少字",
    send: "怎么发",
    hotkey: "快捷键（⌥空格可能和输入法冲突，可以换）",
    manual: "手动发送（推荐）：点「说」或按快捷键开始，点「发送」或再按一次发出。旁边有人说话也不会被听进去。手机上是按住大按钮说、松开发送。",
    auto: "自动发送：一直在听，识别出的文字停 {0} 秒没有新内容就发出。我念的时候你开口就能打断。适合一个人在安静的地方。",
    fast: "快答：通话时把这个会话的推理强度调成「低」，回得更快；挂断后自动恢复。",
    stt: "听写",
    sttLangs: "识别语言（Transcribe 语言代码，逗号分隔；填两种以上会自动判断语言，每句慢约 4 秒）",
    silence: "停顿多久算一句说完（毫秒）",
    autoSend: "自动发送：最后一次识别出文字后等多久发出（毫秒）",
    tuneHelp: "老是被切断就调大，嫌等得久就调小。",
    uiLang: "界面语言"
  },
  en: {
    title: "Voice call",
    intro: 'Click "Voice call" under the message box of any chat and talk to Kiro; replies are read aloud and stop as soon as you speak. On a phone, hold the big button and release to send; on a computer, click "Talk" or use the hotkey.',
    status: "Speech services",
    checking: "Checking…",
    on: "connected",
    off: "not connected",
    pollyOff: "Replies are read by the browser voice instead.",
    sttOff: "Recognition falls back to Kiro Crew's own recognizer.",
    howToAws: "For Amazon voices and recognition, give the environment running this app AWS credentials (locally you can set VOICE_CALL_AWS_PROFILE) with polly:SynthesizeSpeech, polly:DescribeVoices and transcribe:StartStreamTranscription.",
    region: "Region",
    voices: "Voice per language (Amazon Polly)",
    voicesHelp: 'Each reply is read by the voice chosen for its language. During a call, say "slower", "faster" or "normal speed" to change the speed of the language just spoken.',
    gender: { f: "female", m: "male" },
    generative: ", generative",
    speed: "Speed",
    reset: "Reset",
    play: "Play",
    playing: "Playing…",
    fallback: "Read aloud with",
    usePolly: "Amazon Polly (recommended)",
    useBrowser: "Browser voice only (free)",
    maxChars: "Most characters read per reply",
    send: "Sending",
    hotkey: "Hotkey (⌥Space may clash with an input method; change it if so)",
    manual: 'Manual (recommended): click "Talk" or press the hotkey to start, click "Send" or press it again to send. People talking nearby are never picked up. On a phone, hold the big button and release to send.',
    auto: "Automatic: always listening; sends once no new words have been recognized for {0} s. Speak while Kiro is talking to interrupt. Best alone in a quiet room.",
    fast: `Quick answers: lower this chat's reasoning effort to "low" during a call, restored on hang-up.`,
    stt: "Recognition",
    sttLangs: "Recognition languages (Transcribe codes, comma separated; two or more turn on language identification, about 4 s slower per sentence)",
    silence: "Pause that ends a phrase (ms)",
    autoSend: "Automatic: wait after the last recognized words (ms)",
    tuneHelp: "Raise it if you get cut off, lower it if sending feels slow.",
    uiLang: "Interface language"
  }
};
function D() {
  const v = T(), [o, C] = h(V()), [m, z] = h(j()), [S, L] = h([]), [b, A] = h({}), [s, k] = h(null), [O, f] = h(""), t = K[m];
  x(() => {
    var l;
    const e = () => L($());
    return e(), (l = window.speechSynthesis) == null || l.addEventListener("voiceschanged", e), () => {
      var c;
      return (c = window.speechSynthesis) == null ? void 0 : c.removeEventListener("voiceschanged", e);
    };
  }, []), x(() => {
    const e = (l) => v.raw(l, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    e("/apps/voice-call/api/voices").then(async (l) => {
      l.ok && A(await l.json());
    }).catch(() => {
    }), e("/apps/voice-call/api/status").then(async (l) => {
      l.ok && k(await l.json());
    }).catch(() => k({ polly: !1, transcribe: !1, region: "" }));
  }, [v]);
  const i = (e) => {
    const l = { ...o, ...e };
    C(l), B(l);
  }, R = async (e, l) => {
    f(`${e}:${l}`);
    try {
      const c = await v.raw("/apps/voice-call/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: b[e].sample, lang: e, voices: { [e]: l }, rates: o.pollyRates })
      });
      if (!c.ok) {
        f("");
        return;
      }
      const g = URL.createObjectURL(await c.blob()), r = new Audio(g);
      r.onended = () => {
        URL.revokeObjectURL(g), f("");
      }, await r.play();
    } catch {
      f("");
    }
  }, w = (e) => /* @__PURE__ */ n("span", { style: { color: e ? "var(--success, #30a46c)" : "var(--danger, #e5484d)" }, children: "●" });
  return /* @__PURE__ */ a("div", { style: { padding: "20px 24px", overflowY: "auto", flex: 1, minHeight: 0, color: "var(--text)" }, children: [
    /* @__PURE__ */ a("div", { style: { display: "flex", alignItems: "center", gap: 12 }, children: [
      /* @__PURE__ */ n("h1", { style: { fontSize: 20, margin: "0 0 4px", flex: 1 }, children: t.title }),
      /* @__PURE__ */ a("label", { style: { fontSize: 12, color: "var(--muted)" }, children: [
        t.uiLang,
        " ",
        /* @__PURE__ */ a("select", { style: d, value: m, onChange: (e) => {
          const l = e.target.value;
          E(l), z(l);
        }, children: [
          /* @__PURE__ */ n("option", { value: "zh", children: "中文" }),
          /* @__PURE__ */ n("option", { value: "en", children: "English" })
        ] })
      ] })
    ] }),
    /* @__PURE__ */ n("p", { style: { fontSize: 13, color: "var(--muted)", margin: "0 0 16px" }, children: t.intro }),
    /* @__PURE__ */ a("div", { style: u, "data-testid": "voice-call-status-card", children: [
      /* @__PURE__ */ n("strong", { style: { fontSize: 14 }, children: t.status }),
      s ? /* @__PURE__ */ a(P, { children: [
        /* @__PURE__ */ a("p", { style: { fontSize: 13, margin: "8px 0 2px" }, children: [
          w(s.polly),
          " Amazon Polly：",
          s.polly ? t.on : t.off,
          s.polly ? "" : ` — ${t.pollyOff}`
        ] }),
        /* @__PURE__ */ a("p", { style: { fontSize: 13, margin: "2px 0" }, children: [
          w(s.transcribe),
          " Amazon Transcribe：",
          s.transcribe ? t.on : t.off,
          s.transcribe ? "" : ` — ${t.sttOff}`
        ] }),
        s.region && /* @__PURE__ */ a("p", { style: y, children: [
          t.region,
          ": ",
          s.region
        ] }),
        (!s.polly || !s.transcribe) && /* @__PURE__ */ a("p", { style: y, children: [
          t.howToAws,
          s.error ? ` (${s.error})` : "",
          s.transcribe_missing ? ` (${s.transcribe_missing})` : ""
        ] })
      ] }) : /* @__PURE__ */ n("p", { style: y, children: t.checking })
    ] }),
    Object.keys(b).length > 0 && (s == null ? void 0 : s.polly) !== !1 && /* @__PURE__ */ a("div", { style: u, children: [
      /* @__PURE__ */ n("strong", { style: { fontSize: 14 }, children: t.voices }),
      /* @__PURE__ */ n("p", { style: y, children: t.voicesHelp }),
      Object.entries(b).map(([e, l]) => {
        const c = o.pollyVoices[e] ?? l.default, g = o.pollyRates[e] ?? 1;
        return /* @__PURE__ */ a("div", { style: { display: "flex", alignItems: "center", gap: 8, margin: "6px 0", flexWrap: "wrap" }, children: [
          /* @__PURE__ */ n("span", { style: { width: 130, fontSize: 13 }, children: H(e, m) }),
          /* @__PURE__ */ n("select", { style: d, value: c, onChange: (r) => i({ pollyVoices: { ...o.pollyVoices, [e]: r.target.value } }), children: l.options.map((r) => /* @__PURE__ */ a("option", { value: r.id, children: [
            r.id,
            "（",
            t.gender[r.gender],
            r.engine === "generative" ? t.generative : "",
            "）"
          ] }, r.id)) }),
          /* @__PURE__ */ a("span", { style: { fontSize: 12, color: "var(--muted)" }, children: [
            t.speed,
            " ",
            g.toFixed(2),
            "×"
          ] }),
          g !== 1 && /* @__PURE__ */ n("button", { type: "button", style: d, onClick: () => i({ pollyRates: { ...o.pollyRates, [e]: 1 } }), children: t.reset }),
          /* @__PURE__ */ n("button", { type: "button", style: d, onClick: () => void R(e, c), children: O === `${e}:${c}` ? t.playing : t.play })
        ] }, e);
      })
    ] }),
    /* @__PURE__ */ a("div", { style: u, children: [
      /* @__PURE__ */ n("strong", { style: { fontSize: 14 }, children: t.fallback }),
      /* @__PURE__ */ n("label", { style: p, children: /* @__PURE__ */ a("select", { style: d, value: o.voice === "polly" ? "polly" : "", onChange: (e) => i({ voice: e.target.value }), children: [
        /* @__PURE__ */ n("option", { value: "polly", children: t.usePolly }),
        /* @__PURE__ */ n("option", { value: "", children: t.useBrowser })
      ] }) }),
      o.voice !== "polly" && S.length > 0 && /* @__PURE__ */ n("select", { style: d, value: o.voice, onChange: (e) => i({ voice: e.target.value }), children: S.map((e) => /* @__PURE__ */ n("option", { value: e.name, children: e.name }, e.name)) }),
      /* @__PURE__ */ a("label", { style: p, children: [
        t.maxChars,
        ": ",
        o.maxSpokenChars
      ] }),
      /* @__PURE__ */ n("input", { type: "range", min: 150, max: 2e3, step: 50, value: o.maxSpokenChars, onChange: (e) => i({ maxSpokenChars: Number(e.target.value) }) })
    ] }),
    /* @__PURE__ */ a("div", { style: u, children: [
      /* @__PURE__ */ n("strong", { style: { fontSize: 14 }, children: t.send }),
      /* @__PURE__ */ n("label", { style: p, children: t.hotkey }),
      /* @__PURE__ */ n("select", { style: d, value: o.hotkey, onChange: (e) => i({ hotkey: e.target.value }), children: Object.keys(I).map((e) => /* @__PURE__ */ n("option", { value: e, children: M(e) }, e)) }),
      /* @__PURE__ */ a("label", { style: { ...p, display: "flex", gap: 8, alignItems: "center" }, children: [
        /* @__PURE__ */ n("input", { type: "radio", checked: o.mode === "manual", onChange: () => i({ mode: "manual" }) }),
        t.manual
      ] }),
      /* @__PURE__ */ a("label", { style: { ...p, display: "flex", gap: 8, alignItems: "center" }, children: [
        /* @__PURE__ */ n("input", { type: "radio", checked: o.mode === "auto", onChange: () => i({ mode: "auto" }) }),
        t.auto.replace("{0}", String(o.autoSendMs / 1e3))
      ] }),
      /* @__PURE__ */ a("label", { style: { ...p, display: "flex", gap: 8, alignItems: "center" }, children: [
        /* @__PURE__ */ n("input", { type: "checkbox", checked: o.fast, onChange: (e) => i({ fast: e.target.checked }) }),
        t.fast
      ] })
    ] }),
    /* @__PURE__ */ a("div", { style: u, children: [
      /* @__PURE__ */ n("strong", { style: { fontSize: 14 }, children: t.stt }),
      /* @__PURE__ */ n("label", { style: p, children: t.sttLangs }),
      /* @__PURE__ */ n("input", { style: { ...d, width: 220 }, value: o.sttLangs, placeholder: _(m), onChange: (e) => i({ sttLangs: e.target.value }) }),
      /* @__PURE__ */ a("label", { style: p, children: [
        t.silence,
        ": ",
        o.silenceMs
      ] }),
      /* @__PURE__ */ n("input", { type: "range", min: 500, max: 2500, step: 100, value: o.silenceMs, onChange: (e) => i({ silenceMs: Number(e.target.value) }) }),
      /* @__PURE__ */ a("label", { style: p, children: [
        t.autoSend,
        ": ",
        o.autoSendMs
      ] }),
      /* @__PURE__ */ n("input", { type: "range", min: 1500, max: 8e3, step: 500, value: o.autoSendMs, onChange: (e) => i({ autoSendMs: Number(e.target.value) }) }),
      /* @__PURE__ */ n("p", { style: y, children: t.tuneHelp })
    ] })
  ] });
}
export {
  D as default
};
