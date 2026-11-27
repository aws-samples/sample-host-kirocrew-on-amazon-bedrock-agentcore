import { jsx as l, jsxs as m, Fragment as T } from "react/jsx-runtime";
import { useAppApi as A } from "@kirocrew/app-sdk";
import { useRef as b, useCallback as P, useSyncExternalStore as L, useEffect as x, useState as D } from "react";
import { e as z, f as E, g as M, i as p, t as r, L as R, h as F } from "./engine.mjs";
function W(t, a) {
  const i = z(t, a), n = b(0), e = P(
    (c) => i.subscribe(() => {
      n.current++, c();
    }),
    [i]
  );
  return L(e, () => n.current), i;
}
const k = {
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
  const t = "(pointer: coarse), (max-width: 700px)", [a, i] = D(() => {
    var n;
    return typeof window < "u" && ((n = window.matchMedia) == null ? void 0 : n.call(window, t).matches) === !0;
  });
  return x(() => {
    var c;
    const n = (c = window.matchMedia) == null ? void 0 : c.call(window, t);
    if (!n) return;
    const e = () => i(n.matches);
    return n.addEventListener("change", e), () => n.removeEventListener("change", e);
  }, []), a;
}
const j = (t, a) => {
  if (a)
    return {
      ready: r("phone.ready"),
      recording: r("phone.recording"),
      speaking: r("phone.speaking")
    }[t];
  const i = F();
  return {
    ready: r("desk.ready", i),
    recording: r("desk.recording", i),
    speaking: r("desk.speaking", i)
  }[t];
};
function B({ state: t, level: a, onClick: i }) {
  const n = t === "error" ? "var(--danger, #e5484d)" : t === "speaking" ? "var(--accent, #9046ff)" : t === "thinking" || t === "transcribing" ? "#f5a524" : t === "recording" || t === "hearing" ? "#e5484d" : "var(--muted, #888)", c = t === "hearing" || t === "listening" || t === "recording" ? 1 + Math.min(a * 12, 0.8) : 1;
  return /* @__PURE__ */ l(
    "span",
    {
      role: "button",
      "aria-label": `${r("talk")} / ${r("send")}`,
      onClick: i,
      style: {
        width: 14,
        height: 14,
        borderRadius: 7,
        background: n,
        flex: "none",
        cursor: "pointer",
        transform: `scale(${c})`,
        transition: "transform 80ms linear, background 200ms"
      }
    }
  );
}
function X({ session: t, onClose: a }) {
  const i = A(), n = E(t.sessionKey), e = W(i, n), c = b(!1), g = $(), s = b(!1);
  if (x(() => {
    M(), !(c.current || !n) && (c.current = !0, e.running || (p(n), e.start()));
  }, [n, e]), !n)
    return /* @__PURE__ */ l("p", { style: { fontSize: 12, color: "var(--muted)", margin: 0, padding: "8px 12px" }, children: r("state.idle") });
  const f = e.state === "recording", h = e.state === "error" ? e.note : e.notice ? e.notice : e.draft ? e.draft : j(e.state, g) ?? R[e.state], y = () => {
    e.running ? f ? e.sendTalk() : e.mode === "manual" && e.beginTalk() : (p(n), e.start());
  }, w = async (o) => {
    var d, u;
    o.preventDefault(), (u = (d = o.currentTarget).setPointerCapture) == null || u.call(d, o.pointerId), s.current = !0, e.unlockAudio(), e.running || (p(n), await e.start()), s.current && e.beginTalk();
  }, C = (o) => {
    o.preventDefault(), s.current && (s.current = !1, e.state === "recording" && e.sendTalk());
  }, S = (o) => {
    if (!s.current) return;
    const d = o.currentTarget.getBoundingClientRect(), u = 48;
    (o.clientX < d.left - u || o.clientX > d.right + u || o.clientY < d.top - u || o.clientY > d.bottom + u) && v();
  }, v = () => {
    s.current && (s.current = !1, e.state === "recording" && e.cancelTalk());
  };
  return /* @__PURE__ */ m("div", { "data-testid": "voice-call-control", style: { color: "var(--text)", padding: "6px 10px" }, children: [
    /* @__PURE__ */ m("div", { style: { display: "flex", alignItems: "center", gap: 10 }, children: [
      /* @__PURE__ */ l(B, { state: e.state, level: e.level, onClick: y }),
      /* @__PURE__ */ l(
        "span",
        {
          "aria-live": "polite",
          "data-testid": "voice-call-status",
          title: h,
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
          children: e.draft ? /* @__PURE__ */ l("bdi", { children: h }) : h
        }
      ),
      !g && e.mode === "manual" && /* @__PURE__ */ l(
        "button",
        {
          type: "button",
          "data-testid": "voice-call-toggle",
          style: { ...k, background: f ? "var(--danger, #e5484d)" : "var(--accent, #9046ff)", color: "#fff", border: "none" },
          onClick: () => {
            e.unlockAudio(), f ? e.sendTalk() : e.running ? e.beginTalk() : (p(n), e.start().then(() => e.beginTalk()));
          },
          children: f ? r("send") : r("talk")
        }
      ),
      /* @__PURE__ */ l("button", { type: "button", style: k, onClick: () => {
        e.stop(), a();
      }, children: e.running ? r("hangup") : r("close") })
    ] }),
    g && e.mode === "manual" && /* @__PURE__ */ m(T, { children: [
      /* @__PURE__ */ l(
        "button",
        {
          type: "button",
          "data-testid": "voice-call-hold",
          "aria-label": r("hold.idle"),
          onPointerDown: (o) => void w(o),
          onPointerUp: C,
          onPointerMove: S,
          onPointerCancel: v,
          onLostPointerCapture: v,
          onContextMenu: (o) => o.preventDefault(),
          style: {
            width: "100%",
            height: 64,
            marginTop: 8,
            borderRadius: 32,
            border: "none",
            background: f ? "var(--danger, #e5484d)" : "var(--accent, #9046ff)",
            color: "#fff",
            fontSize: 17,
            fontWeight: 600,
            touchAction: "none",
            userSelect: "none",
            WebkitUserSelect: "none",
            WebkitTouchCallout: "none",
            cursor: "pointer"
          },
          children: f ? r("hold.recording") : r("hold.idle")
        }
      ),
      /* @__PURE__ */ l("p", { style: { fontSize: 11, color: "var(--muted)", margin: "6px 0 0", textAlign: "center" }, children: r("hold.keepFront") })
    ] })
  ] });
}
export {
  X as default
};
