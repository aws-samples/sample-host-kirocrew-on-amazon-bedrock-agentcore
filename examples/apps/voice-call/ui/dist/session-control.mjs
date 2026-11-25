import { jsx as c, jsxs as v, Fragment as S } from "react/jsx-runtime";
import { useAppApi as T } from "@kirocrew/app-sdk";
import { useRef as m, useCallback as A, useSyncExternalStore as P, useEffect as k, useState as L } from "react";
import { a as D, b as E, d as z, e as f, L as M, h as R } from "./engine.mjs";
function W(n, i) {
  const o = D(n, i), t = m(0), e = A(
    (a) => o.subscribe(() => {
      t.current++, a();
    }),
    [o]
  );
  return P(e, () => t.current), o;
}
const b = {
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
  const n = "(pointer: coarse), (max-width: 700px)", [i, o] = L(() => {
    var t;
    return typeof window < "u" && ((t = window.matchMedia) == null ? void 0 : t.call(window, n).matches) === !0;
  });
  return k(() => {
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
  const o = R();
  return {
    ready: `点「说」或按 ${o} 开始`,
    recording: `在听… 点「发送」或再按一次 ${o} 发出，Esc 不发`,
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
  const o = T(), t = E(n.sessionKey), e = W(o, t), a = m(!1), p = $(), s = m(!1);
  if (k(() => {
    z(), !(a.current || !t) && (a.current = !0, e.running || (f(t), e.start()));
  }, [t, e]), !t)
    return /* @__PURE__ */ c("p", { style: { fontSize: 12, color: "var(--muted)", margin: 0, padding: "8px 12px" }, children: "先打开一个会话，再从会话里开始通话。" });
  const u = e.state === "recording", g = e.state === "error" ? e.note : e.notice ? e.notice : e.draft ? e.draft : F(e.state, p) ?? M[e.state], x = () => {
    e.running ? u ? e.sendTalk() : e.mode === "manual" && e.beginTalk() : (f(t), e.start());
  }, y = async (r) => {
    var l, d;
    r.preventDefault(), (d = (l = r.currentTarget).setPointerCapture) == null || d.call(l, r.pointerId), s.current = !0, e.unlockAudio(), e.running || (f(t), await e.start()), s.current && e.beginTalk();
  }, w = (r) => {
    r.preventDefault(), s.current && (s.current = !1, e.state === "recording" && e.sendTalk());
  }, C = (r) => {
    if (!s.current) return;
    const l = r.currentTarget.getBoundingClientRect(), d = 48;
    (r.clientX < l.left - d || r.clientX > l.right + d || r.clientY < l.top - d || r.clientY > l.bottom + d) && h();
  }, h = () => {
    s.current && (s.current = !1, e.state === "recording" && e.cancelTalk());
  };
  return /* @__PURE__ */ v("div", { "data-testid": "voice-call-control", style: { color: "var(--text)", padding: "6px 10px" }, children: [
    /* @__PURE__ */ v("div", { style: { display: "flex", alignItems: "center", gap: 10 }, children: [
      /* @__PURE__ */ c(j, { state: e.state, level: e.level, onClick: x }),
      /* @__PURE__ */ c(
        "span",
        {
          "aria-live": "polite",
          "data-testid": "voice-call-status",
          title: g,
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
          children: e.draft ? /* @__PURE__ */ c("bdi", { children: g }) : g
        }
      ),
      !p && e.mode === "manual" && /* @__PURE__ */ c(
        "button",
        {
          type: "button",
          "data-testid": "voice-call-toggle",
          style: { ...b, background: u ? "var(--danger, #e5484d)" : "var(--accent, #9046ff)", color: "#fff", border: "none" },
          onClick: () => {
            e.unlockAudio(), u ? e.sendTalk() : e.running ? e.beginTalk() : (f(t), e.start().then(() => e.beginTalk()));
          },
          children: u ? "发送" : "说"
        }
      ),
      /* @__PURE__ */ c("button", { type: "button", style: b, onClick: () => {
        e.stop(), i();
      }, children: e.running ? "挂断" : "关闭" })
    ] }),
    p && e.mode === "manual" && /* @__PURE__ */ v(S, { children: [
      /* @__PURE__ */ c(
        "button",
        {
          type: "button",
          "data-testid": "voice-call-hold",
          "aria-label": "按住说话",
          onPointerDown: (r) => void y(r),
          onPointerUp: w,
          onPointerMove: C,
          onPointerCancel: h,
          onLostPointerCapture: h,
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
