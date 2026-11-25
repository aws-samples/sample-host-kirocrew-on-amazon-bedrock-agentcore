import { jsxs as a, jsx as t } from "react/jsx-runtime";
import { useAppApi as b } from "@kirocrew/app-sdk";
import { useState as p, useEffect as x } from "react";
import { l as f, H as h, c as S, s as C } from "./engine.mjs";
const c = {
  background: "var(--surface, transparent)",
  color: "var(--text)",
  border: "1px solid var(--border)",
  borderRadius: 10,
  padding: 16,
  marginBottom: 14
}, r = { display: "block", fontSize: 13, margin: "10px 0 4px" }, d = {
  background: "var(--bg, transparent)",
  color: "var(--text)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  padding: "4px 8px",
  fontSize: 13
};
function N() {
  const u = b(), [n, y] = p(f()), [s, g] = p([]);
  x(() => {
    var o;
    const e = () => g(S());
    return e(), (o = window.speechSynthesis) == null || o.addEventListener("voiceschanged", e), () => {
      var i;
      return (i = window.speechSynthesis) == null ? void 0 : i.removeEventListener("voiceschanged", e);
    };
  }, []);
  const l = (e) => {
    const o = { ...n, ...e };
    y(o), C(o);
  }, m = () => {
    if (window.speechSynthesis.cancel(), n.voice === "polly") {
      const i = new Audio();
      u.raw("/apps/voice-call/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: "你好，我是 Kiro。现在可以直接跟我说话了。", voice: n.voice }) }).then(async (v) => {
        i.src = URL.createObjectURL(await v.blob()), i.play();
      });
      return;
    }
    const e = new SpeechSynthesisUtterance("你好，我是 Kiro。现在可以直接跟我说话了。"), o = s.find((i) => i.name === n.voice) ?? s.find((i) => /Tingting/i.test(i.name)) ?? s[0];
    o ? (e.voice = o, e.lang = o.lang) : e.lang = "zh-CN", e.rate = n.rate, window.speechSynthesis.speak(e);
  };
  return /* @__PURE__ */ a("div", { style: { padding: "20px 24px", overflowY: "auto", flex: 1, minHeight: 0, color: "var(--text)" }, children: [
    /* @__PURE__ */ t("h1", { style: { fontSize: 20, margin: "0 0 4px" }, children: "语音通话" }),
    /* @__PURE__ */ t("p", { style: { fontSize: 13, color: "var(--muted)", margin: "0 0 16px" }, children: "在任意会话的输入框下面点「语音通话」，直接说话。手机上按住大按钮说、松开发送；电脑上用快捷键。 听写用沙箱里 Kiro Crew 自带的识别，朗读用 Amazon Polly 的 Zhiyu，失败的那句会改用浏览器自带的声音。 手机上请保持本页在前台、屏幕常亮，切到后台时系统会停掉麦克风。" }),
    /* @__PURE__ */ a("div", { style: c, children: [
      /* @__PURE__ */ t("strong", { style: { fontSize: 14 }, children: "朗读" }),
      /* @__PURE__ */ t("label", { style: r, children: "声音" }),
      /* @__PURE__ */ a("select", { style: d, value: n.voice, onChange: (e) => l({ voice: e.target.value }), children: [
        /* @__PURE__ */ t("option", { value: "polly", children: "Zhiyu（Amazon Polly，推荐）" }),
        /* @__PURE__ */ t("option", { value: "", children: "浏览器自带声音（马上开口）" }),
        s.map((e) => /* @__PURE__ */ a("option", { value: e.name, children: [
          e.name,
          e.localService ? "" : "（联网）"
        ] }, e.name))
      ] }),
      /* @__PURE__ */ a("label", { style: r, children: [
        "语速：",
        n.rate.toFixed(2)
      ] }),
      /* @__PURE__ */ t("input", { type: "range", min: 0.8, max: 1.8, step: 0.05, value: n.rate, onChange: (e) => l({ rate: Number(e.target.value) }) }),
      /* @__PURE__ */ a("label", { style: r, children: [
        "一次最多念多少字：",
        n.maxSpokenChars
      ] }),
      /* @__PURE__ */ t("input", { type: "range", min: 150, max: 2e3, step: 50, value: n.maxSpokenChars, onChange: (e) => l({ maxSpokenChars: Number(e.target.value) }) }),
      /* @__PURE__ */ t("div", { style: { marginTop: 12 }, children: /* @__PURE__ */ t("button", { type: "button", style: { ...d, cursor: "pointer" }, onClick: m, children: "试听" }) })
    ] }),
    /* @__PURE__ */ a("div", { style: c, children: [
      /* @__PURE__ */ t("strong", { style: { fontSize: 14 }, children: "怎么发" }),
      /* @__PURE__ */ t("label", { style: r, children: "快捷键" }),
      /* @__PURE__ */ t("select", { style: d, value: n.hotkey, onChange: (e) => l({ hotkey: e.target.value }), children: Object.keys(h).map((e) => /* @__PURE__ */ t("option", { value: e, children: h[e].label }, e)) }),
      /* @__PURE__ */ a("label", { style: { ...r, display: "flex", gap: 8, alignItems: "center" }, children: [
        /* @__PURE__ */ t("input", { type: "radio", checked: n.mode === "manual", onChange: () => l({ mode: "manual" }) }),
        "手动发送（推荐）：点「说」或按 ⌥空格 开始，点「发送」或再按一次 ⌥空格 发出。旁边有人说话也不会被听进去。手机上是按住大按钮说、松开发送。"
      ] }),
      /* @__PURE__ */ a("label", { style: { ...r, display: "flex", gap: 8, alignItems: "center" }, children: [
        /* @__PURE__ */ t("input", { type: "radio", checked: n.mode === "auto", onChange: () => l({ mode: "auto" }) }),
        "自动发送：一直在听，停顿就发出，我念的时候你开口就能打断。适合一个人在安静的地方。"
      ] }),
      /* @__PURE__ */ a("label", { style: { ...r, display: "flex", gap: 8, alignItems: "center" }, children: [
        /* @__PURE__ */ t("input", { type: "checkbox", checked: n.fast, onChange: (e) => l({ fast: e.target.checked }) }),
        "快答：通话时把这个会话的推理强度调成「低」，回得更快；挂断后自动恢复原来的设置。"
      ] })
    ] }),
    /* @__PURE__ */ a("div", { style: c, children: [
      /* @__PURE__ */ t("strong", { style: { fontSize: 14 }, children: "听写" }),
      /* @__PURE__ */ a("label", { style: r, children: [
        "停顿多久算说完（毫秒）：",
        n.silenceMs
      ] }),
      /* @__PURE__ */ t("input", { type: "range", min: 500, max: 2500, step: 100, value: n.silenceMs, onChange: (e) => l({ silenceMs: Number(e.target.value) }) }),
      /* @__PURE__ */ a("label", { style: r, children: [
        "自动发送：没人说话多久才发出（毫秒）：",
        n.autoSendMs
      ] }),
      /* @__PURE__ */ t("input", { type: "range", min: 1500, max: 8e3, step: 500, value: n.autoSendMs, onChange: (e) => l({ autoSendMs: Number(e.target.value) }) }),
      /* @__PURE__ */ t("p", { style: { fontSize: 12, color: "var(--muted)" }, children: "老是被打断就调大，嫌等得久就调小。改完下一通电话生效。" })
    ] })
  ] });
}
export {
  N as default
};
