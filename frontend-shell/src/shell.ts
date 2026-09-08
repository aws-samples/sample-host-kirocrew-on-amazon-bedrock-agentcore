import type { LifecycleModel, LifecycleView } from "./lifecycle.js";
import { lifecyclePresentation } from "./lifecycle.js";

const STYLE_ID = "kirocrew-agentcore-shell-style";
const SVG_NS = "http://www.w3.org/2000/svg";

export interface KiroSsoLogin {
  readonly method: "sso";
  readonly startUrl: string;
  readonly region: string;
}

export interface AuthCredentials {
  readonly email: string;
  readonly password: string;
}

export interface PasswordResetRequest {
  readonly email: string;
  readonly code: string;
  readonly password: string;
}

export interface EmailConfirmation {
  readonly email: string;
  readonly password: string;
  readonly code: string;
}

export interface BrowserShellActions {
  readonly signIn: (credentials: AuthCredentials) => void | Promise<void>;
  readonly register: (credentials: AuthCredentials) => void | Promise<void>;
  readonly confirmEmail: (request: EmailConfirmation) => void | Promise<void>;
  readonly resendCode: (credentials: AuthCredentials) => void | Promise<void>;
  readonly forgotPassword: (email: string) => void | Promise<void>;
  readonly resetPassword: (
    request: PasswordResetRequest,
  ) => void | Promise<void>;
  readonly start: () => void | Promise<void>;
  readonly stop: () => void | Promise<void>;
  readonly retry: () => void | Promise<void>;
  readonly logout: () => void | Promise<void>;
  readonly kiroCheck: () => void | Promise<void>;
  readonly kiroLogin: (options?: KiroSsoLogin) => void | Promise<void>;
  readonly kiroLogout: () => void | Promise<void>;
}

export interface BrowserShellHandle {
  render(model: LifecycleModel): void;
  destroy(): void;
}

function createElement<K extends keyof HTMLElementTagNameMap>(
  document: Document,
  name: K,
  className?: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(name);
  if (className !== undefined) {
    element.className = className;
  }
  return element;
}

function installStyles(document: Document): void {
  if (document.getElementById(STYLE_ID) !== null) {
    return;
  }
  const style = createElement(document, "style");
  style.id = STYLE_ID;
  style.textContent = `
    .kcac-shell {
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
    }
    .kcac-float {
      --kcac-gutter: 16px;
      position: fixed;
      top: var(--kcac-gutter);
      right: var(--kcac-gutter);
      /* Above every upstream layer (modals, toasts, tour tooltips). */
      z-index: 2147483000;
      display: flex;
      flex-direction: column;
      max-width: min(460px, calc(100vw - 24px));
      /* Expanding the details section must not push the panel past the
         bottom of the screen, where its own overflow:hidden would clip the
         content out of reach. Dragging rewrites --kcac-top so the cap
         follows the panel's current offset. */
      max-height: calc(
        100vh - var(--kcac-top, var(--kcac-gutter)) - var(--kcac-gutter)
      );
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--card);
      color: var(--card-fg, var(--text));
      box-shadow: 0 12px 32px color-mix(in srgb, #000 24%, transparent);
      overflow: hidden;
    }
    .kcac-float[data-mode="panel"][data-emphasis="true"] {
      min-width: min(400px, calc(100vw - 24px));
    }
    .kcac-float[data-mode="pill"] {
      border-radius: 999px;
      max-width: none;
      width: auto;
      height: 40px;
    }
    .kcac-float[data-mode="pill"] .kcac-rail,
    .kcac-float[data-mode="pill"] .kcac-auth,
    .kcac-float[data-mode="pill"] .kcac-device,
    .kcac-float[data-mode="pill"] .kcac-kiro,
    .kcac-float[data-mode="pill"] .kcac-info { display: none !important; }
    .kcac-float[data-mode="panel"] .kcac-pill { display: none; }
    .kcac-pill {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      height: 40px;
      /* The pill is a drag handle: without this, mobile browsers claim the
         touch for page scrolling and cancel the drag immediately. */
      touch-action: none;
      -webkit-user-select: none;
      user-select: none;
      padding: 0 14px;
      border: 0;
      background: transparent;
      color: var(--text-strong, var(--text));
      font: inherit;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .04em;
      white-space: nowrap;
      cursor: grab;
    }
    .kcac-pill:focus-visible {
      outline: 3px solid var(--accent);
      outline-offset: 2px;
    }
    .kcac-rail {
      touch-action: none;
      -webkit-user-select: none;
      user-select: none;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 64px;
      padding: 10px 18px;
      background: var(--card);
      color: var(--card-fg, var(--text));
      cursor: grab;
    }
    .kcac-float[data-emphasis="true"] .kcac-rail {
      padding: 16px 20px;
      align-items: flex-start;
      flex-direction: column;
    }
    .kcac-state { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .kcac-copy { min-width: 0; }
    .kcac-eyebrow {
      margin: 0 0 2px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .kcac-title {
      margin: 0;
      color: var(--text-strong, var(--text));
      font: inherit;
      font-size: 14px;
      font-weight: 700;
    }
    .kcac-float[data-emphasis="true"] .kcac-title { font-size: 17px; }
    .kcac-progress {
      list-style: none; margin: 6px 0 0; padding: 0; display: none;
      flex-direction: column; gap: 2px;
    }
    .kcac-progress[data-active="true"] { display: flex; }
    .kcac-step {
      font-size: 12px; color: #8b93a7; display: flex; align-items: center; gap: 6px;
    }
    .kcac-step::before { content: "\\25CB"; font-size: 10px; }
    .kcac-step[data-state="active"] { color: #cdd5e4; }
    .kcac-step[data-state="active"]::before { content: "\\25CF"; color: #7aa2ff; }
    .kcac-step[data-state="done"] { color: #9fb597; }
    .kcac-step[data-state="done"]::before { content: "\\2713"; color: #78c07d; }
    .kcac-elapsed { font-size: 11px; color: #8b93a7; margin: 4px 0 0; display: none; }
    .kcac-elapsed[data-active="true"] { display: block; }
    .kcac-detail, .kcac-meta {
      margin: 2px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .kcac-rail p, .kcac-rail h1, .kcac-device span, .kcac-device code,
    .kcac-kiro-state, .kcac-auth-hint,
    .kcac-kiro-hint {
      cursor: text;
      user-select: text;
    }
    .kcac-dot {
      width: 10px;
      height: 10px;
      flex: 0 0 auto;
      border-radius: 999px;
      background: var(--muted);
      box-shadow: 0 0 0 4px color-mix(in srgb, var(--muted) 18%, transparent);
    }
    .kcac-float[data-tone="progress"] .kcac-dot {
      background: var(--accent);
      box-shadow: 0 0 0 4px var(--accent-subtle);
      animation: kcac-pulse 1.6s ease-in-out infinite;
    }
    .kcac-float[data-tone="success"] .kcac-dot {
      background: var(--ok);
      box-shadow: 0 0 0 4px var(--ok-subtle);
    }
    .kcac-float[data-tone="warning"] .kcac-dot {
      background: var(--warn);
      box-shadow: 0 0 0 4px var(--warn-subtle);
    }
    .kcac-float[data-tone="danger"] .kcac-dot {
      background: var(--danger);
      box-shadow: 0 0 0 4px var(--danger-subtle);
    }
    /* Shrinkable (flex 0 1 auto): a non-shrinking action row would crush
       the title to zero width once it holds four buttons; wrapping onto a
       second row keeps every control visible instead. */
    .kcac-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex: 0 1 auto; flex-wrap: wrap; }
    .kcac-state { flex: 1 1 auto; }
    .kcac-button, .kcac-link {
      min-height: 36px;
      padding: 7px 13px;
      border: 1px solid var(--border-strong, var(--border));
      border-radius: 7px;
      background: var(--bg-elevated, var(--bg));
      color: var(--text-strong, var(--text));
      font: inherit;
      font-size: 13px;
      font-weight: 650;
      line-height: 20px;
      text-decoration: none;
      cursor: pointer;
    }
    .kcac-button:hover, .kcac-link:hover { background: var(--bg-hover); }
    .kcac-button[data-primary="true"] {
      border-color: var(--accent);
      background: var(--accent);
      color: var(--bg);
    }
    .kcac-button[data-primary="true"]:hover { background: var(--accent-hover); }
    .kcac-button:focus-visible, .kcac-link:focus-visible {
      outline: 3px solid var(--accent);
      outline-offset: 2px;
    }
    .kcac-button:disabled { cursor: not-allowed; opacity: .55; }
    .kcac-content { min-height: 100vh; position: relative; }
    .kcac-content[data-blocked="true"]::after {
      content: "";
      position: absolute;
      inset: 0;
      background: var(--bg);
      opacity: .72;
      pointer-events: auto;
    }
    .kcac-auth {
      display: none;
      gap: 10px;
      flex-direction: column;
      padding: 12px 18px;
      background: var(--accent-subtle);
      color: var(--text);
      border-top: 1px solid var(--border);
      font-size: 13px;
    }
    .kcac-auth[data-visible="true"] { display: flex; }
    /* In this column layout the shared input's flex-basis would become
       HEIGHT and inflate each field to ~180px; pin them to one text line. */
    .kcac-auth .kcac-input { flex: none; width: 100%; box-sizing: border-box; }
    /* The two auth modes share the email field and the hint; each group of
       mode-specific rows joins the column via display: contents. */
    .kcac-auth [data-authpart] { display: none; }
    .kcac-auth[data-authmode="sign-in"] [data-authpart="sign-in"],
    .kcac-auth[data-authmode="confirm"] [data-authpart="confirm"],
    .kcac-auth[data-authmode="reset"] [data-authpart="reset"] { display: contents; }
    .kcac-pw { position: relative; }
    .kcac-pw .kcac-input { padding-right: 42px; }
    .kcac-pw-eye {
      position: absolute;
      right: 5px;
      top: 50%;
      transform: translateY(-50%);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 30px;
      height: 26px;
      padding: 0;
      border: none;
      border-radius: 5px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
    }
    .kcac-pw-eye:hover { color: var(--text-strong, var(--text)); background: var(--bg-hover, transparent); }
    .kcac-pw-eye:focus-visible { outline: 3px solid var(--accent); outline-offset: 1px; }
    .kcac-auth-note {
      margin: 0;
      font-size: 11px;
      line-height: 1.4;
      color: var(--muted);
    }
    .kcac-auth-link {
      align-self: flex-start;
      padding: 0;
      border: none;
      background: none;
      color: var(--accent);
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      text-decoration: underline;
    }
    .kcac-auth-link:hover { color: var(--accent-hover, var(--accent)); }
    .kcac-auth-link:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
    .kcac-device {
      display: none;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      padding: 12px 18px;
      background: var(--accent-subtle);
      color: var(--text);
      border-top: 1px solid var(--border);
      font-size: 13px;
    }
    .kcac-device[data-visible="true"] { display: flex; }
    .kcac-device-url {
      flex: 1 1 100%;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
      word-break: break-all;
      cursor: text;
      user-select: text;
    }
    .kcac-code {
      padding: 4px 8px;
      border: 1px solid var(--border-strong, var(--border));
      border-radius: 5px;
      background: var(--bg);
      color: var(--text-strong, var(--text));
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700;
      letter-spacing: .08em;
    }
    .kcac-kiro {
      display: none;
      flex-direction: column;
      gap: 10px;
      padding: 12px 18px;
      border-top: 1px solid var(--border);
      font-size: 13px;
    }
    .kcac-kiro[data-visible="true"] { display: flex; }
    .kcac-info {
      display: none;
      flex-direction: column;
      gap: 6px;
      padding: 10px 18px;
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: var(--muted);
    }
    .kcac-info[data-visible="true"] { display: flex; }
    .kcac-info-toggle {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      align-self: flex-start;
      padding: 0;
      border: none;
      background: none;
      color: var(--text-strong, var(--text));
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      font-weight: 600;
    }
    .kcac-info-caret {
      display: inline-block;
      transition: transform .15s ease;
      font-size: 10px;
      color: var(--muted);
    }
    .kcac-info-toggle[aria-expanded="true"] .kcac-info-caret { transform: rotate(90deg); }
    .kcac-info-uptime { color: var(--text-strong, var(--text)); font-variant-numeric: tabular-nums; }
    .kcac-info-toggle:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
    .kcac-info-body[hidden] { display: none; }
    .kcac-info-title {
      margin: 6px 0 2px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .kcac-info-list { margin: 0; padding: 0; list-style: none; }
    .kcac-info-list li {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 1px 0;
    }
    .kcac-info-list .kcac-info-state { color: var(--text-strong, var(--text)); font-weight: 500; }
    .kcac-info-paths { margin: 0; padding: 0; list-style: none; font-family: ui-monospace, monospace; font-size: 11px; }
    /* Under the panel's height cap the details body is the only section that
       gives up space, so the rail and the sign-in blocks keep their natural
       height and the history and paths lists scroll instead of being cut off. */
    .kcac-rail, .kcac-auth, .kcac-device, .kcac-kiro, .kcac-pill { flex: none; }
    .kcac-info { flex: 0 1 auto; min-height: 0; }
    .kcac-info-body {
      min-height: 0;
      overflow-y: auto;
      overscroll-behavior: contain;
    }
    .kcac-kiro-header { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
    .kcac-kiro-name { font-weight: 700; color: var(--text-strong, var(--text)); }
    .kcac-kiro-state { color: var(--muted); }
    .kcac-kiro-state[data-state="authenticated"] { color: var(--ok); font-weight: 650; }
    .kcac-kiro-state[data-state="required"],
    .kcac-kiro-state[data-state="expired"],
    .kcac-kiro-state[data-state="failed"] { color: var(--warn); font-weight: 650; }
    .kcac-kiro-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .kcac-auth-hint,
    .kcac-kiro-hint { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.4; }
    .kcac-kiro-sso { display: flex; gap: 8px; flex-wrap: wrap; }
    .kcac-input {
      flex: 1 1 180px;
      min-height: 34px;
      padding: 6px 10px;
      border: 1px solid var(--border-strong, var(--border));
      border-radius: 7px;
      background: var(--bg);
      color: var(--text-strong, var(--text));
      font: inherit;
      font-size: 13px;
    }
    .kcac-input:focus-visible { outline: 3px solid var(--accent); outline-offset: 1px; }
    .kcac-input[aria-invalid="true"] { border-color: var(--danger); }
    .kcac-input[data-role="region"] { flex: 0 1 110px; }
    .kcac-sr {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    @keyframes kcac-pulse { 50% { opacity: .45; } }
    @media (prefers-reduced-motion: reduce) {
      .kcac-float[data-tone="progress"] .kcac-dot { animation: none; }
    }
    @media (max-width: 720px) {
      .kcac-float { --kcac-gutter: 8px; }
      .kcac-rail { align-items: flex-start; flex-direction: column; padding: 10px 12px; }
      .kcac-actions { width: 100%; }
      .kcac-button, .kcac-link { flex: 1 1 auto; text-align: center; }
      .kcac-device { align-items: flex-start; padding-inline: 12px; }
    }
  `;
  document.head.append(style);
}

function invoke(action: () => void | Promise<void>): void {
  try {
    const result = action();
    if (result instanceof Promise) {
      result.catch(() => undefined);
    }
  } catch {
    // The controller owns safe error presentation; event handlers never leak details.
  }
}

function shouldBlockContent(view: LifecycleView): boolean {
  // Only states with no usable sandbox behind them block the page. An error
  // or a reconnect must never lock the user out of an upstream app that is
  // still running (an agent may be mid-turn); the panel reports the problem
  // and the page stays interactive.
  return view === "signed-out" || view === "stopped";
}

function needsAttention(view: LifecycleView): boolean {
  return view !== "ready" && view !== "active-response" && view !== "read-only";
}

function isEmphasized(view: LifecycleView): boolean {
  return view === "signed-out" || view === "terminal-error";
}

export function mountBrowserShell(
  root: HTMLElement,
  actions: BrowserShellActions,
): BrowserShellHandle {
  const document = root.ownerDocument;
  installStyles(document);

  const existing = Array.from(root.childNodes);
  const shell = createElement(document, "div", "kcac-shell");
  shell.dataset.design = "floating-status-panel";
  // A plain container keeps the inner <header> in the banner landmark role
  // (header loses it inside article/aside/main/nav/section).
  const float = createElement(document, "div", "kcac-float");
  const header = createElement(document, "header", "kcac-rail");
  header.setAttribute("aria-label", "Sandbox status and controls");
  const state = createElement(document, "div", "kcac-state");
  const dot = createElement(document, "span", "kcac-dot");
  dot.setAttribute("aria-hidden", "true");
  const copy = createElement(document, "div", "kcac-copy");
  const eyebrow = createElement(document, "p", "kcac-eyebrow");
  eyebrow.textContent = "Your sandbox";
  const title = createElement(document, "h1", "kcac-title");
  title.tabIndex = -1;
  const detail = createElement(document, "p", "kcac-detail");
  const metadata = createElement(document, "p", "kcac-meta");
  copy.append(eyebrow, title, detail, metadata);
  const progress = createElement(document, "ol", "kcac-progress");
  progress.setAttribute("aria-label", "Startup progress");
  const progressSteps: HTMLLIElement[] = [];
  for (const stepLabel of [
    "Requesting compute",
    "Restoring workspace",
    "Starting KiroCrew",
  ]) {
    const step = createElement(document, "li", "kcac-step");
    step.textContent = stepLabel;
    progress.append(step);
    progressSteps.push(step);
  }
  const elapsed = createElement(document, "p", "kcac-elapsed");
  copy.append(progress, elapsed);
  state.append(dot, copy);

  const actionGroup = createElement(document, "nav", "kcac-actions");
  actionGroup.setAttribute("aria-label", "Sandbox actions");
  const primary = createElement(document, "button", "kcac-button");
  primary.type = "button";
  primary.dataset.primary = "true";
  const retry = createElement(document, "button", "kcac-button");
  retry.type = "button";
  retry.textContent = "Retry safely";
  const reload = createElement(document, "button", "kcac-button");
  reload.type = "button";
  reload.textContent = "Reload";
  reload.setAttribute(
    "aria-label",
    "Reload the page and reconnect to the sandbox without stopping it",
  );
  const logout = createElement(document, "button", "kcac-button");
  logout.type = "button";
  logout.textContent = "Sign out";
  logout.setAttribute(
    "aria-label",
    "Sign out of this browser without stopping or deleting the sandbox",
  );
  const minimize = createElement(document, "button", "kcac-button");
  minimize.type = "button";
  minimize.textContent = "Minimize";
  minimize.setAttribute("aria-label", "Minimize the status panel");
  actionGroup.append(primary, retry, reload, logout, minimize);
  header.append(state, actionGroup);

  const device = createElement(document, "section", "kcac-device");
  device.setAttribute("aria-label", "Kiro sign-in required");
  const deviceText = createElement(document, "span");
  deviceText.textContent =
    "To connect Kiro, open the verification page and enter:";
  const deviceCode = createElement(document, "code", "kcac-code");
  const deviceLink = createElement(document, "a", "kcac-link");
  deviceLink.target = "_blank";
  deviceLink.rel = "noopener noreferrer";
  deviceLink.textContent = "Open Kiro verification";
  const deviceUrl = createElement(document, "span", "kcac-device-url");
  deviceUrl.setAttribute("aria-label", "Verification page address");
  device.append(deviceText, deviceCode, deviceLink, deviceUrl);

  const auth = createElement(document, "section", "kcac-auth");
  auth.setAttribute("aria-label", "Sign in or create an account");
  auth.dataset.authmode = "sign-in";
  const authEmail = createElement(document, "input", "kcac-input");
  authEmail.type = "email";
  authEmail.autocomplete = "username";
  authEmail.placeholder = "Email";
  authEmail.setAttribute("aria-label", "Email address");

  const eyeIcon = (open: boolean): SVGSVGElement => {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "16");
    svg.setAttribute("height", "16");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const shapes = open
      ? [
          "M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z",
          "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
        ]
      : [
          "M9.88 9.88a3 3 0 1 0 4.24 4.24",
          "M10.73 5.08A10.4 10.4 0 0 1 12 5c7 0 10 7 10 7a13.2 13.2 0 0 1-1.67 2.68",
          "M6.61 6.61A13.5 13.5 0 0 0 2 12s3 7 10 7a9.7 9.7 0 0 0 5.39-1.61",
          "M2 2l20 20",
        ];
    for (const d of shapes) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  };

  const passwordField = (
    autocomplete: string,
    placeholder: string,
    label: string,
  ): { readonly wrap: HTMLDivElement; readonly input: HTMLInputElement } => {
    const wrap = createElement(document, "div", "kcac-pw");
    const input = createElement(document, "input", "kcac-input");
    input.type = "password";
    input.setAttribute("autocomplete", autocomplete);
    input.placeholder = placeholder;
    input.setAttribute("aria-label", label);
    const eye = createElement(document, "button", "kcac-pw-eye");
    eye.type = "button";
    eye.setAttribute("aria-label", "Show password");
    eye.setAttribute("aria-pressed", "false");
    eye.append(eyeIcon(true));
    eye.addEventListener("click", () => {
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      eye.setAttribute("aria-pressed", String(reveal));
      eye.setAttribute(
        "aria-label",
        reveal ? "Hide password" : "Show password",
      );
      eye.replaceChildren(eyeIcon(!reveal));
    });
    wrap.append(input, eye);
    return { wrap, input };
  };

  const passwordNote = (): HTMLParagraphElement => {
    const note = createElement(document, "p", "kcac-auth-note");
    note.textContent =
      "Passwords need at least 8 characters, including a lowercase letter and a number.";
    return note;
  };

  const signinField = passwordField("current-password", "Password", "Password");
  const authPassword = signinField.input;
  const authActions = createElement(document, "div", "kcac-kiro-actions");
  const authSignIn = createElement(document, "button", "kcac-button");
  authSignIn.type = "button";
  authSignIn.dataset.primary = "true";
  authSignIn.dataset.auth = "sign-in";
  authSignIn.textContent = "Sign in";
  const authRegister = createElement(document, "button", "kcac-button");
  authRegister.type = "button";
  authRegister.dataset.auth = "register";
  authRegister.textContent = "Create account";
  authActions.append(authSignIn, authRegister);
  const authForgotLink = createElement(document, "button", "kcac-auth-link");
  authForgotLink.type = "button";
  authForgotLink.textContent = "Forgot password?";
  const signinPart = createElement(document, "div");
  signinPart.dataset.authpart = "sign-in";
  signinPart.append(
    passwordNote(),
    signinField.wrap,
    authActions,
    authForgotLink,
  );

  const confirmNote = createElement(document, "p", "kcac-auth-note");
  confirmNote.textContent =
    "Confirm your email address: enter the code we sent you.";
  const confirmCode = createElement(document, "input", "kcac-input");
  confirmCode.type = "text";
  confirmCode.inputMode = "numeric";
  confirmCode.setAttribute("autocomplete", "one-time-code");
  confirmCode.placeholder = "Code from the email";
  confirmCode.setAttribute("aria-label", "Confirmation code");
  const confirmActions = createElement(document, "div", "kcac-kiro-actions");
  const authConfirm = createElement(document, "button", "kcac-button");
  authConfirm.type = "button";
  authConfirm.dataset.primary = "true";
  authConfirm.textContent = "Confirm email";
  const authResend = createElement(document, "button", "kcac-button");
  authResend.type = "button";
  authResend.textContent = "Resend code";
  confirmActions.append(authConfirm, authResend);
  const authConfirmBack = createElement(document, "button", "kcac-auth-link");
  authConfirmBack.type = "button";
  authConfirmBack.textContent = "Back to sign in";
  const confirmPart = createElement(document, "div");
  confirmPart.dataset.authpart = "confirm";
  confirmPart.append(confirmNote, confirmCode, confirmActions, authConfirmBack);

  const authCode = createElement(document, "input", "kcac-input");
  authCode.type = "text";
  authCode.inputMode = "numeric";
  authCode.setAttribute("autocomplete", "one-time-code");
  authCode.placeholder = "Code from the email";
  authCode.setAttribute("aria-label", "Verification code");
  const resetField = passwordField(
    "new-password",
    "New password",
    "New password",
  );
  const resetActions = createElement(document, "div", "kcac-kiro-actions");
  const authSendCode = createElement(document, "button", "kcac-button");
  authSendCode.type = "button";
  authSendCode.textContent = "Send code";
  const authResetSubmit = createElement(document, "button", "kcac-button");
  authResetSubmit.type = "button";
  authResetSubmit.dataset.primary = "true";
  authResetSubmit.textContent = "Reset password";
  resetActions.append(authSendCode, authResetSubmit);
  const authBackLink = createElement(document, "button", "kcac-auth-link");
  authBackLink.type = "button";
  authBackLink.textContent = "Back to sign in";
  const resetPart = createElement(document, "div");
  resetPart.dataset.authpart = "reset";
  resetPart.append(
    authCode,
    passwordNote(),
    resetField.wrap,
    resetActions,
    authBackLink,
  );

  const authHint = createElement(document, "p", "kcac-auth-hint");
  authHint.setAttribute("role", "alert");
  authHint.hidden = true;
  auth.append(authEmail, signinPart, confirmPart, resetPart, authHint);

  const kiro = createElement(document, "section", "kcac-kiro");
  kiro.setAttribute("aria-label", "Kiro account");
  const kiroHeader = createElement(document, "div", "kcac-kiro-header");
  const kiroName = createElement(document, "span", "kcac-kiro-name");
  kiroName.textContent = "Kiro account";
  const kiroState = createElement(document, "span", "kcac-kiro-state");
  kiroHeader.append(kiroName, kiroState);
  const kiroSso = createElement(document, "div", "kcac-kiro-sso");
  const ssoUrl = createElement(document, "input", "kcac-input");
  ssoUrl.type = "url";
  ssoUrl.placeholder = "https://your-org.awsapps.com/start";
  ssoUrl.setAttribute("aria-label", "Identity Center start URL");
  const ssoRegion = createElement(document, "input", "kcac-input");
  ssoRegion.dataset.role = "region";
  ssoRegion.placeholder = "us-east-1";
  ssoRegion.setAttribute("aria-label", "Identity Center region");
  kiroSso.append(ssoUrl, ssoRegion);
  const kiroActions = createElement(document, "div", "kcac-kiro-actions");
  const kiroSsoLogin = createElement(document, "button", "kcac-button");
  kiroSsoLogin.type = "button";
  kiroSsoLogin.dataset.primary = "true";
  kiroSsoLogin.dataset.kiro = "sso";
  kiroSsoLogin.textContent = "Sign in with SSO";
  const kiroFreeLogin = createElement(document, "button", "kcac-button");
  kiroFreeLogin.type = "button";
  kiroFreeLogin.dataset.kiro = "builder-id";
  kiroFreeLogin.textContent = "Sign in with Builder ID";
  const kiroLogout = createElement(document, "button", "kcac-button");
  kiroLogout.type = "button";
  kiroLogout.dataset.kiro = "logout";
  kiroLogout.textContent = "Sign out of Kiro";
  const kiroCheck = createElement(document, "button", "kcac-button");
  kiroCheck.type = "button";
  kiroCheck.dataset.kiro = "check";
  kiroCheck.textContent = "Check status";
  kiroActions.append(kiroSsoLogin, kiroFreeLogin, kiroLogout, kiroCheck);
  const kiroHint = createElement(document, "p", "kcac-kiro-hint");
  kiroHint.hidden = true;
  kiro.append(kiroHeader, kiroSso, kiroActions, kiroHint);

  // Remember the last used Identity Center settings across visits.
  const storage = ((): Storage | undefined => {
    try {
      return document.defaultView?.localStorage;
    } catch {
      return undefined;
    }
  })();
  try {
    ssoUrl.value = storage?.getItem("kirocrew.kiro.sso.startUrl") ?? "";
    ssoRegion.value = storage?.getItem("kirocrew.kiro.sso.region") ?? "";
  } catch {
    // Storage access is best effort only.
  }

  const pill = createElement(document, "button", "kcac-pill");
  pill.type = "button";
  pill.setAttribute("aria-label", "Show the sandbox status panel");
  const pillDot = createElement(document, "span", "kcac-dot");
  pillDot.setAttribute("aria-hidden", "true");
  const pillLabel = createElement(document, "span", "kcac-pill-label");
  pillLabel.textContent = "Sandbox";
  pill.append(pillDot, pillLabel);

  const info = createElement(document, "section", "kcac-info");
  info.setAttribute("aria-label", "Sandbox details");
  const infoToggle = createElement(document, "button", "kcac-info-toggle");
  infoToggle.type = "button";
  const infoCaret = createElement(document, "span", "kcac-info-caret");
  infoCaret.textContent = "\u25B6";
  infoCaret.setAttribute("aria-hidden", "true");
  const infoLabel = createElement(document, "span");
  infoLabel.textContent = "Sandbox details";
  infoToggle.append(infoCaret, infoLabel);
  infoToggle.setAttribute("aria-expanded", "false");
  const infoBody = createElement(document, "div", "kcac-info-body");
  infoBody.hidden = true;
  const uptimeTitle = createElement(document, "p", "kcac-info-title");
  uptimeTitle.textContent = "MicroVM uptime";
  const uptimeValue = createElement(document, "p", "kcac-info-uptime");
  uptimeValue.textContent = "\u2014";
  uptimeValue.setAttribute("aria-label", "MicroVM uptime");
  const historyTitle = createElement(document, "p", "kcac-info-title");
  historyTitle.textContent = "Recent activity";
  const historyList = createElement(document, "ul", "kcac-info-list");
  historyList.setAttribute("aria-label", "Sandbox state history");
  const pathsTitle = createElement(document, "p", "kcac-info-title");
  pathsTitle.textContent = "Persisted paths";
  const pathsHint = createElement(document, "p", "kcac-auth-note");
  pathsHint.textContent =
    "Files under these paths survive Stop safely and restarts; everything else is ephemeral.";
  const pathsList = createElement(document, "ul", "kcac-info-paths");
  pathsList.setAttribute("aria-label", "Persisted paths");
  infoBody.append(
    uptimeTitle,
    uptimeValue,
    historyTitle,
    historyList,
    pathsTitle,
    pathsHint,
    pathsList,
  );
  info.append(infoToggle, infoBody);
  infoToggle.addEventListener("click", () => {
    infoBody.hidden = !infoBody.hidden;
    infoToggle.setAttribute("aria-expanded", String(!infoBody.hidden));
  });

  float.append(header, auth, device, kiro, info, pill);

  const content = createElement(document, "main", "kcac-content");
  content.id = "kirocrew-upstream-application";
  content.setAttribute("aria-label", "KiroCrew application");
  content.append(...existing);

  const live = createElement(document, "div", "kcac-sr");
  live.setAttribute("role", "status");
  live.setAttribute("aria-live", "polite");
  live.setAttribute("aria-atomic", "true");

  shell.append(float, content, live);
  root.replaceChildren(shell);

  // --- Floating panel mode. The user can always minimize to the pill; a
  // NEW attention event (view change, device code, Kiro state change) pops
  // the panel back open, and returning to calm auto-collapses it.
  let collapsed = false;
  let lastAttentionKey: string | undefined = "";
  let lastKiroState: string | undefined;
  const applyMode = (): void => {
    float.dataset.mode = collapsed ? "pill" : "panel";
  };
  minimize.addEventListener("click", () => {
    collapsed = true;
    applyMode();
  });

  // --- Pointer dragging with click detection. Action buttons and links keep
  // their default behaviour; the rail surface and the pill act as handles.
  let pointerId: number | undefined;
  let dragMoved = false;
  let pressedPill = false;
  let dragSlop = 4;
  let startX = 0;
  let startY = 0;
  let originLeft = 0;
  let originTop = 0;
  const clamp = (value: number, minimum: number, maximum: number): number =>
    Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
  float.addEventListener("pointerdown", (event: PointerEvent) => {
    if (event.button !== 0) {
      return;
    }
    const target = event.target as HTMLElement | null;
    // Text can be selected and controls clicked without moving the panel:
    // only blank panel surface and the pill act as drag handles.
    const blocked = target?.closest("button, a, input, p, h1, code, span");
    if (blocked != null && blocked !== pill && !pill.contains(blocked)) {
      return;
    }
    pointerId = event.pointerId;
    dragMoved = false;
    // Fingers jitter more than mice: with a tight slop a tap on the pill
    // often registered as a tiny drag and refused to open the panel.
    dragSlop = event.pointerType === "mouse" ? 4 : 12;
    pressedPill = target === pill || (target !== null && pill.contains(target));
    const rect = float.getBoundingClientRect();
    originLeft = rect.left;
    originTop = rect.top;
    startX = event.clientX;
    startY = event.clientY;
    float.setPointerCapture?.(event.pointerId);
  });
  float.addEventListener("pointermove", (event: PointerEvent) => {
    if (pointerId !== event.pointerId) {
      return;
    }
    const deltaX = event.clientX - startX;
    const deltaY = event.clientY - startY;
    if (!dragMoved && Math.abs(deltaX) + Math.abs(deltaY) < dragSlop) {
      return;
    }
    dragMoved = true;
    const view = document.defaultView;
    const rect = float.getBoundingClientRect();
    const maxLeft = (view?.innerWidth ?? rect.right + 8) - rect.width - 8;
    const maxTop = (view?.innerHeight ?? rect.bottom + 8) - rect.height - 8;
    const top = clamp(originTop + deltaY, 8, maxTop);
    float.style.left = `${clamp(originLeft + deltaX, 8, maxLeft)}px`;
    float.style.top = `${top}px`;
    // The height cap is measured from wherever the panel now sits, so a
    // panel dragged downwards still cannot grow past the screen.
    float.style.setProperty("--kcac-top", `${top}px`);
    float.style.right = "auto";
  });
  const endDrag = (event: PointerEvent): void => {
    if (pointerId !== event.pointerId) {
      return;
    }
    pointerId = undefined;
    if (float.hasPointerCapture?.(event.pointerId)) {
      float.releasePointerCapture?.(event.pointerId);
    }
    // Pointer capture retargets the click, so a tap on the pill is
    // completed here; a drag never toggles the panel.
    if (event.type === "pointerup" && pressedPill && !dragMoved) {
      collapsed = false;
      applyMode();
    }
    pressedPill = false;
  };
  float.addEventListener("pointerup", endDrag);
  float.addEventListener("pointercancel", endDrag);
  pill.addEventListener("click", (event: MouseEvent) => {
    // Keyboard activation only: pointer taps arrive via pointerup above.
    if (event.detail === 0) {
      collapsed = false;
      applyMode();
    }
  });

  const setAuthMode = (mode: "sign-in" | "confirm" | "reset"): void => {
    auth.dataset.authmode = mode;
    authHint.hidden = true;
  };
  const authNotice = (message: string): void => {
    authHint.hidden = false;
    authHint.textContent = message;
  };
  const isUnverified = (error: unknown): boolean =>
    error instanceof Error &&
    (error as { code?: unknown }).code === "EMAIL_NOT_VERIFIED";

  const submitAuth = (
    action: (credentials: AuthCredentials) => void | Promise<void>,
    kind: "sign-in" | "register",
  ): void => {
    const email = authEmail.value.trim();
    const password = authPassword.value;
    if (!email.includes("@") || password.length === 0) {
      authNotice("Enter your email address and password.");
      return;
    }
    authHint.hidden = true;
    authSignIn.disabled = true;
    authRegister.disabled = true;
    void (async (): Promise<void> => {
      try {
        await action({ email, password });
        if (kind === "register") {
          // The password stays in the field: confirming and resending act
          // with the user's own just-created credentials.
          setAuthMode("confirm");
          authNotice(`We emailed a code to ${email}.`);
        } else {
          authPassword.value = "";
        }
      } catch (error: unknown) {
        if (isUnverified(error)) {
          setAuthMode("confirm");
          authNotice(
            "Confirm your email address first: enter the code we emailed you, or resend it.",
          );
        } else {
          authNotice(
            error instanceof Error
              ? error.message
              : "Sign-in could not be completed. Try again.",
          );
        }
      } finally {
        authSignIn.disabled = false;
        authRegister.disabled = false;
      }
    })();
  };
  authSignIn.addEventListener("click", () =>
    submitAuth(actions.signIn, "sign-in"),
  );
  authRegister.addEventListener("click", () =>
    submitAuth(actions.register, "register"),
  );
  authPassword.addEventListener("keydown", (event: KeyboardEvent) => {
    if (event.key === "Enter") {
      submitAuth(actions.signIn, "sign-in");
    }
  });

  const confirmCredentials = ():
    | { email: string; password: string }
    | undefined => {
    const email = authEmail.value.trim();
    const password = authPassword.value;
    if (!email.includes("@") || password.length === 0) {
      setAuthMode("sign-in");
      authNotice("Enter your email address and password first.");
      return undefined;
    }
    return { email, password };
  };
  authConfirm.addEventListener("click", () => {
    const credentials = confirmCredentials();
    if (credentials === undefined) {
      return;
    }
    const code = confirmCode.value.trim();
    if (code.length === 0) {
      authNotice("Enter the code from the email.");
      return;
    }
    authHint.hidden = true;
    authConfirm.disabled = true;
    authResend.disabled = true;
    void (async (): Promise<void> => {
      try {
        await actions.confirmEmail({ ...credentials, code });
        confirmCode.value = "";
        authPassword.value = "";
        setAuthMode("sign-in");
      } catch (error: unknown) {
        authNotice(
          error instanceof Error
            ? error.message
            : "The code could not be confirmed. Try again.",
        );
      } finally {
        authConfirm.disabled = false;
        authResend.disabled = false;
      }
    })();
  });
  authResend.addEventListener("click", () => {
    const credentials = confirmCredentials();
    if (credentials === undefined) {
      return;
    }
    authHint.hidden = true;
    authResend.disabled = true;
    void (async (): Promise<void> => {
      try {
        await actions.resendCode(credentials);
        authNotice(`A new code is on its way to ${credentials.email}.`);
      } catch (error: unknown) {
        authNotice(
          error instanceof Error
            ? error.message
            : "The code could not be sent. Try again.",
        );
      } finally {
        authResend.disabled = false;
      }
    })();
  });
  authConfirmBack.addEventListener("click", () => setAuthMode("sign-in"));

  authForgotLink.addEventListener("click", () => setAuthMode("reset"));
  authBackLink.addEventListener("click", () => setAuthMode("sign-in"));

  authSendCode.addEventListener("click", () => {
    const email = authEmail.value.trim();
    if (!email.includes("@")) {
      authNotice("Enter your email address first.");
      return;
    }
    authHint.hidden = true;
    authSendCode.disabled = true;
    void (async (): Promise<void> => {
      try {
        await actions.forgotPassword(email);
        authNotice(`If that account exists, a code is on its way to ${email}.`);
      } catch (error: unknown) {
        authNotice(
          error instanceof Error
            ? error.message
            : "The code could not be sent. Try again.",
        );
      } finally {
        authSendCode.disabled = false;
      }
    })();
  });
  authResetSubmit.addEventListener("click", () => {
    const email = authEmail.value.trim();
    const code = authCode.value.trim();
    const password = resetField.input.value;
    if (!email.includes("@") || code.length === 0 || password.length === 0) {
      authNotice("Enter your email, the emailed code, and a new password.");
      return;
    }
    authHint.hidden = true;
    authResetSubmit.disabled = true;
    authSendCode.disabled = true;
    void (async (): Promise<void> => {
      try {
        await actions.resetPassword({ email, code, password });
        authCode.value = "";
        resetField.input.value = "";
        setAuthMode("sign-in");
        authNotice("Password updated. Sign in with your new password.");
      } catch (error: unknown) {
        authNotice(
          error instanceof Error
            ? error.message
            : "The password could not be reset. Try again.",
        );
      } finally {
        authResetSubmit.disabled = false;
        authSendCode.disabled = false;
      }
    })();
  });

  primary.addEventListener("click", () => {
    const action = primary.dataset.action;
    if (action === "start") {
      invoke(actions.start);
    } else if (action === "stop") {
      invoke(actions.stop);
    }
  });
  retry.addEventListener("click", () => invoke(actions.retry));
  reload.addEventListener("click", () => {
    // A stale page (expired session, dead duplex) is fully cured by a
    // reload: bootstrap re-reads the sandbox record and reconnects.
    window.location.reload();
  });
  logout.addEventListener("click", () => invoke(actions.logout));
  kiroCheck.addEventListener("click", () => invoke(actions.kiroCheck));
  kiroLogout.addEventListener("click", () => invoke(actions.kiroLogout));
  kiroFreeLogin.addEventListener("click", () =>
    invoke(() => actions.kiroLogin()),
  );
  kiroSsoLogin.addEventListener("click", () => {
    const startUrl = ssoUrl.value.trim();
    const region = ssoRegion.value.trim() || "us-east-1";
    if (!startUrl.startsWith("https://")) {
      ssoUrl.setAttribute("aria-invalid", "true");
      ssoUrl.focus();
      return;
    }
    ssoUrl.removeAttribute("aria-invalid");
    try {
      storage?.setItem("kirocrew.kiro.sso.startUrl", startUrl);
      storage?.setItem("kirocrew.kiro.sso.region", region);
    } catch {
      // Storage access is best effort only.
    }
    invoke(() => actions.kiroLogin({ method: "sso", startUrl, region }));
  });

  let previousView: LifecycleView | undefined;
  let elapsedTimer: number | undefined;
  const startupJourney = (
    model: LifecycleModel,
  ): { step: 0 | 1 | 2; reconnecting: boolean } | undefined => {
    const fresh =
      model.startedAt !== undefined && Date.now() - model.startedAt < 600_000;
    if (model.view === "starting" && fresh) {
      return {
        step: model.sandbox?.state === "STARTING" ? 1 : 0,
        reconnecting: false,
      };
    }
    if (model.view === "restoring" && fresh) {
      return { step: 2, reconnecting: false };
    }
    if (model.view === "reconnecting" && fresh) {
      // A dropped data-plane connection mid-startup is routine; keep the
      // startup card steady instead of flipping to a warning and back.
      const state = model.sandbox?.state;
      const step = state === "RESTORING" ? 2 : 1;
      return { step, reconnecting: true };
    }
    return undefined;
  };
  const formatElapsed = (startedAt: number): string => {
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return `${String(minutes)}:${String(rest).padStart(2, "0")}`;
  };
  const render = (model: LifecycleModel): void => {
    const presentation = lifecyclePresentation(model.view);
    const journey = startupJourney(model);
    float.dataset.tone = journey === undefined ? presentation.tone : "progress";
    float.dataset.emphasis = String(
      isEmphasized(model.view) ||
        model.deviceFlow !== undefined ||
        model.kiroAuth?.state === "required",
    );
    title.textContent =
      journey === undefined ? presentation.label : "Starting your sandbox";
    pill.setAttribute(
      "aria-label",
      `Show the sandbox status panel. ${String(title.textContent)}`,
    );
    pill.title = String(title.textContent);
    detail.textContent =
      journey === undefined
        ? (model.error?.message ?? presentation.detail)
        : journey.reconnecting
          ? "Connection hiccup - re-establishing without losing progress."
          : journey.step === 2
            ? "Restoring your encrypted workspace, then launching KiroCrew. Large workspaces can take a few minutes."
            : "Allocating isolated AgentCore compute for your sandbox.";
    progress.dataset.active = String(journey !== undefined);
    elapsed.dataset.active = String(
      journey !== undefined && model.startedAt !== undefined,
    );
    if (journey !== undefined) {
      progressSteps.forEach((step, index) => {
        step.dataset.state =
          index < journey.step
            ? "done"
            : index === journey.step
              ? "active"
              : "pending";
      });
      if (model.startedAt !== undefined) {
        elapsed.textContent = `Elapsed ${formatElapsed(model.startedAt)}`;
      }
      if (elapsedTimer === undefined) {
        elapsedTimer = window.setInterval(() => {
          if (model.startedAt !== undefined) {
            elapsed.textContent = `Elapsed ${formatElapsed(model.startedAt)}`;
          }
        }, 1000);
      }
    } else if (elapsedTimer !== undefined) {
      window.clearInterval(elapsedTimer);
      elapsedTimer = undefined;
    }
    metadata.textContent = [model.checkpointFreshness, model.restoreOutcome]
      .filter(
        (value): value is string => value !== undefined && value.length > 0,
      )
      .join(" · ");
    live.textContent = `${presentation.label}. ${detail.textContent}`;

    content.dataset.blocked = String(shouldBlockContent(model.view));
    content.inert = shouldBlockContent(model.view);
    content.setAttribute("aria-hidden", String(shouldBlockContent(model.view)));

    auth.dataset.visible = String(model.view === "signed-out");
    const details = model.details;
    info.dataset.visible = String(
      model.view !== "signed-out" && details !== undefined,
    );
    if (details !== undefined) {
      // The current microVM's birth is the oldest event of the newest
      // unbroken run of live states: brief transitions (STARTING can last
      // under a second) are often missed by the observing poll, so any
      // event before the last STOPPED/ERROR boundary anchors the uptime.
      const live = new Set([
        "STARTING",
        "RESTORING",
        "READY",
        "BUSY",
        "CHECKPOINTING",
        "STOPPING",
      ]);
      let born: string | undefined;
      for (const event of details.events) {
        if (!live.has(event.state.toUpperCase())) {
          break;
        }
        born = event.at;
      }
      const running =
        model.view === "ready" ||
        model.view === "active-response" ||
        model.view === "starting" ||
        model.view === "restoring";
      if (born !== undefined && running) {
        const seconds = Math.max(
          0,
          Math.floor((Date.now() - Date.parse(born)) / 1000),
        );
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        uptimeValue.textContent =
          hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m ${seconds % 60}s`;
      } else {
        uptimeValue.textContent = "\u2014 (not running)";
      }
      historyList.replaceChildren(
        ...details.events.map((event) => {
          const item = createElement(document, "li");
          const state = createElement(document, "span", "kcac-info-state");
          state.textContent = event.state.toLowerCase().replace(/_/gu, " ");
          const when = createElement(document, "span");
          when.textContent = new Date(event.at).toLocaleString();
          item.append(state, when);
          return item;
        }),
      );
      if (details.events.length === 0) {
        const empty = createElement(document, "li");
        empty.textContent = "No activity recorded yet.";
        historyList.append(empty);
      }
      pathsList.replaceChildren(
        ...details.persistedPaths.map((path) => {
          const item = createElement(document, "li");
          item.textContent = path;
          return item;
        }),
      );
    }
    if (model.view !== "signed-out") {
      authHint.hidden = true;
    }
    primary.hidden = false;
    primary.disabled = false;
    if (model.view === "signed-out") {
      primary.hidden = true;
      delete primary.dataset.action;
    } else if (
      model.view === "stopped" ||
      (model.view === "terminal-error" && model.sandbox?.state === "ERROR")
    ) {
      // ERROR is a restartable state (ERROR -> STARTING): a parked sandbox
      // must offer the way out itself instead of dead-ending the user on
      // Reload and Sign out until an operator resets the record.
      primary.textContent = "Start sandbox";
      primary.dataset.action = "start";
    } else if (
      model.view === "ready" ||
      model.view === "active-response" ||
      model.view === "read-only"
    ) {
      primary.textContent =
        model.view === "active-response"
          ? "Response in progress"
          : "Stop safely";
      primary.dataset.action = "stop";
      primary.disabled = model.view === "active-response";
    } else {
      primary.hidden = true;
      delete primary.dataset.action;
    }

    retry.textContent = model.activeRequestAccepted
      ? "Check connection"
      : "Retry safely";
    retry.hidden = !(
      model.view === "reconnecting" ||
      (model.view === "terminal-error" && model.error?.retryable === true)
    );
    // Reload never destroys anything, so offer it whenever the user is
    // signed in: a stale page otherwise dead-ends in opaque 503s with
    // only destructive-looking choices (Stop safely, Sign out) visible.
    reload.hidden = model.view === "signed-out";
    logout.hidden = model.view === "signed-out";

    const flow = model.deviceFlow;
    device.dataset.visible = String(flow !== undefined);
    if (flow !== undefined) {
      deviceCode.textContent = flow.userCode;
      deviceLink.href = flow.verificationUri;
      deviceLink.setAttribute(
        "aria-label",
        `Open Kiro verification page. Code ${flow.userCode}`,
      );
      deviceUrl.textContent = flow.verificationUri;
    } else {
      deviceCode.textContent = "";
      deviceLink.removeAttribute("href");
      deviceUrl.textContent = "";
    }

    const kiroAuth = model.kiroAuth;
    kiro.dataset.visible = String(kiroAuth !== undefined);
    if (kiroAuth !== undefined) {
      kiroState.dataset.state = kiroAuth.state;
      kiroState.textContent = {
        checking: "Checking\u2026",
        authenticated: "Signed in",
        required: "Not signed in",
        expired: "Sign-in expired",
        failed: "Sign-in failed",
      }[kiroAuth.state];
      const busy = kiroAuth.state === "checking";
      const authenticated = kiroAuth.state === "authenticated";
      kiroSsoLogin.hidden = authenticated;
      kiroFreeLogin.hidden = authenticated;
      kiroSso.hidden = authenticated;
      kiroLogout.hidden = !authenticated;
      for (const button of [
        kiroSsoLogin,
        kiroFreeLogin,
        kiroLogout,
        kiroCheck,
      ]) {
        button.disabled = busy;
      }
      if (kiroAuth.state === "failed") {
        kiroHint.hidden = false;
        kiroHint.textContent =
          "Sign-in did not complete. For SSO, check the start URL and region, then try again.";
      } else if (kiroAuth.state === "expired") {
        kiroHint.hidden = false;
        kiroHint.textContent =
          "The verification code expired before it was confirmed. Start the sign-in again.";
      } else if (kiroAuth.state === "checking") {
        kiroHint.hidden = false;
        kiroHint.textContent =
          "Contacting Kiro\u2026 a verification code will appear here.";
      } else {
        kiroHint.hidden = true;
        kiroHint.textContent = "";
      }
    }
    const kiroAttention =
      kiroAuth !== undefined &&
      (kiroAuth.state === "failed" || kiroAuth.state === "expired");
    const attention =
      needsAttention(model.view) || flow !== undefined || kiroAttention;
    const attentionKey = attention
      ? [
          model.view,
          flow?.userCode ?? "",
          kiroAuth?.state ?? "",
          model.error?.code ?? "",
        ].join("|")
      : undefined;
    const becameCalm =
      attentionKey === undefined && lastAttentionKey !== undefined;
    const becameAuthenticated =
      kiroAuth?.state === "authenticated" && lastKiroState !== "authenticated";
    if (attentionKey !== undefined && attentionKey !== lastAttentionKey) {
      // A new reason to look at the panel pops it open, even if the user
      // minimized an earlier one.
      collapsed = false;
    } else if (
      attentionKey === undefined &&
      (becameCalm || becameAuthenticated)
    ) {
      // Settling down (connected, or Kiro just signed in) minimizes on its
      // own; anything else leaves a manually opened panel alone.
      collapsed = true;
    }
    lastAttentionKey = attentionKey;
    lastKiroState = kiroAuth?.state;
    applyMode();

    if (
      previousView !== undefined &&
      previousView !== model.view &&
      (model.view === "terminal-error" || model.view === "signed-out")
    ) {
      title.focus();
    }
    previousView = model.view;
  };

  return {
    render,
    destroy: (): void => {
      root.replaceChildren(...existing);
      shell.remove();
    },
  };
}
