import { jsx as c, jsxs as g, Fragment as S } from "react/jsx-runtime";
import { useAppApi as C } from "@kirocrew/app-sdk";
import { useRef as v, useCallback as T, useSyncExternalStore as P, useEffect as b, useState as A } from "react";
import { a as L, b as D, d as E, e as h, L as z, h as M } from "./engine.mjs";
function R(n, i) {
  const o = L(n, i), t = v(0), e = T(
    (a) => o.subscribe(() => {
      t.current++, a();
    }),
    [o]
  );
  return P(e, () => t.current), o;
}
const W = {
  background: "transparent",
  color: "var(--text)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  padding: "2px 10px",
  fontSize: 12,
  cursor: "pointer",
  whiteSpace: "nowrap"
};
function $() {
  const n = "(pointer: coarse), (max-width: 700px)", [i, o] = A(() => {
    var t;
    return typeof window < "u" && ((t = window.matchMedia) == null ? void 0 : t.call(window, n).matches) === !0;
  });
  return b(() => {
    var a;
    const t = (a = window.matchMedia) == null ? void 0 : a.call(window, n);
    if (!t) return;
    const e = () => o(t.matches);
    return t.addEventListener("change", e), () => t.removeEventListener("change", e);
  }, []), i;
}
const F = (n, i) => {
  if (i)
    return {
      ready: "按住下面的按钮说话，松开发送",
      recording: "在听… 松开发送，滑出按钮不发",
      speaking: "在说… 按住按钮就能打断"
    }[n];
  const o = M();
  return {
    ready: `按 ${o} 说话 · 按住说、松开发；或按一下开始、再按一下发`,
    recording: `在听… 再按 ${o} 发送，Esc 不发`,
    speaking: `在说… 按 ${o} 打断`
  }[n];
};
function j({ state: n, level: i, onClick: o }) {
  const t = n === "error" ? "var(--danger, #e5484d)" : n === "speaking" ? "var(--accent, #9046ff)" : n === "thinking" || n === "transcribing" ? "#f5a524" : n === "recording" || n === "hearing" ? "#e5484d" : "var(--muted, #888)", a = n === "hearing" || n === "listening" || n === "recording" ? 1 + Math.min(i * 12, 0.8) : 1;
  return /* @__PURE__ */ c(
    "span",
    {
      role: "button",
      "aria-label": "说话或发送",
      onClick: o,
      style: {
        width: 14,
        height: 14,
        borderRadius: 7,
        background: t,
        flex: "none",
        cursor: "pointer",
        transform: `scale(${a})`,
        transition: "transform 80ms linear, background 200ms"
      }
    }
  );
}
function U({ session: n, onClose: i }) {
  const o = C(), t = D(n.sessionKey), e = R(o, t), a = v(!1), m = $(), s = v(!1);
  if (b(() => {
    E(), !(a.current || !t) && (a.current = !0, e.running || (h(t), e.start()));
  }, [t, e]), !t)
    return /* @__PURE__ */ c("p", { style: { fontSize: 12, color: "var(--muted)", margin: 0, padding: "8px 12px" }, children: "先打开一个会话，再从会话里开始通话。" });
  const u = e.state === "recording", f = e.state === "error" ? e.note : e.notice ? e.notice : e.draft ? e.draft : F(e.state, m) ?? z[e.state], x = () => {
    e.running ? u ? e.sendTalk() : e.mode === "manual" && e.beginTalk() : (h(t), e.start());
  }, k = async (r) => {
    var l, d;
    r.preventDefault(), (d = (l = r.currentTarget).setPointerCapture) == null || d.call(l, r.pointerId), s.current = !0, e.unlockAudio(), e.running || (h(t), await e.start()), s.current && e.beginTalk();
  }, y = (r) => {
    r.preventDefault(), s.current && (s.current = !1, e.state === "recording" && e.sendTalk());
  }, w = (r) => {
    if (!s.current) return;
    const l = r.currentTarget.getBoundingClientRect(), d = 48;
    (r.clientX < l.left - d || r.clientX > l.right + d || r.clientY < l.top - d || r.clientY > l.bottom + d) && p();
  }, p = () => {
    s.current && (s.current = !1, e.state === "recording" && e.cancelTalk());
  };
  return /* @__PURE__ */ g("div", { "data-testid": "voice-call-control", style: { color: "var(--text)", padding: "6px 10px" }, children: [
    /* @__PURE__ */ g("div", { style: { display: "flex", alignItems: "center", gap: 10 }, children: [
      /* @__PURE__ */ c(j, { state: e.state, level: e.level, onClick: x }),
      /* @__PURE__ */ c(
        "span",
        {
          "aria-live": "polite",
          "data-testid": "voice-call-status",
          title: f,
          style: {
            flex: 1,
            minWidth: 0,
            fontSize: e.draft ? 13 : 12,
            color: e.state === "error" || e.notice ? "var(--danger, #e5484d)" : e.draft ? "var(--text)" : "var(--muted)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            direction: e.draft ? "rtl" : "ltr",
            textAlign: "left"
          },
          children: e.draft ? /* @__PURE__ */ c("bdi", { children: f }) : f
        }
      ),
      /* @__PURE__ */ c("button", { type: "button", style: W, onClick: () => {
        e.stop(), i();
      }, children: e.running ? "挂断" : "关闭" })
    ] }),
    m && e.mode === "manual" && /* @__PURE__ */ g(S, { children: [
      /* @__PURE__ */ c(
        "button",
        {
          type: "button",
          "data-testid": "voice-call-hold",
          "aria-label": "按住说话",
          onPointerDown: (r) => void k(r),
          onPointerUp: y,
          onPointerMove: w,
          onPointerCancel: p,
          onLostPointerCapture: p,
          onContextMenu: (r) => r.preventDefault(),
          style: {
            width: "100%",
            height: 64,
            marginTop: 8,
            borderRadius: 32,
            border: "none",
            background: u ? "var(--danger, #e5484d)" : "var(--accent, #9046ff)",
            color: "#fff",
            fontSize: 17,
            fontWeight: 600,
            touchAction: "none",
            userSelect: "none",
            WebkitUserSelect: "none",
            WebkitTouchCallout: "none",
            cursor: "pointer"
          },
          children: u ? "松开发送" : "按住说话"
        }
      ),
      /* @__PURE__ */ c("p", { style: { fontSize: 11, color: "var(--muted)", margin: "6px 0 0", textAlign: "center" }, children: "请保持本页在前台、屏幕常亮：切到后台时手机会停掉麦克风。" })
    ] })
  ] });
}
export {
  U as default
};
