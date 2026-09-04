import type { LifecycleModel, LifecycleView } from "./lifecycle.js";
import { lifecyclePresentation } from "./lifecycle.js";

const STYLE_ID = "kirocrew-agentcore-shell-style";

export interface KiroSsoLogin {
  readonly method: "sso";
  readonly startUrl: string;
  readonly region: string;
}

export interface AuthCredentials {
  readonly email: string;
  readonly password: string;
}

export interface BrowserShellActions {
  readonly signIn: (credentials: AuthCredentials) => void | Promise<void>;
  readonly register: (credentials: AuthCredentials) => void | Promise<void>;
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
      position: fixed;
      top: 16px;
      right: 16px;
      /* Above every upstream layer (modals, toasts, tour tooltips). */
      z-index: 2147483000;
      display: flex;
      flex-direction: column;
      max-width: min(460px, calc(100vw - 24px));
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
    .kcac-float[data-mode="pill"] .kcac-kiro { display: none !important; }
    .kcac-float[data-mode="panel"] .kcac-pill { display: none; }
    .kcac-pill {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      height: 40px;
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
    .kcac-actions { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; flex-wrap: wrap; }
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
      .kcac-float { top: 8px; right: 8px; }
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
  actionGroup.append(primary, retry, logout, minimize);
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
  const authEmail = createElement(document, "input", "kcac-input");
  authEmail.type = "email";
  authEmail.autocomplete = "username";
  authEmail.placeholder = "you@amazon.com";
  authEmail.setAttribute("aria-label", "Email address");
  const authPassword = createElement(document, "input", "kcac-input");
  authPassword.type = "password";
  authPassword.autocomplete = "current-password";
  authPassword.placeholder = "Password";
  authPassword.setAttribute("aria-label", "Password");
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
  const authHint = createElement(document, "p", "kcac-auth-hint");
  authHint.setAttribute("role", "alert");
  authHint.hidden = true;
  auth.append(authEmail, authPassword, authActions, authHint);

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

  float.append(header, auth, device, kiro, pill);

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
    if (!dragMoved && Math.abs(deltaX) + Math.abs(deltaY) < 4) {
      return;
    }
    dragMoved = true;
    const view = document.defaultView;
    const rect = float.getBoundingClientRect();
    const maxLeft = (view?.innerWidth ?? rect.right + 8) - rect.width - 8;
    const maxTop = (view?.innerHeight ?? rect.bottom + 8) - rect.height - 8;
    float.style.left = `${clamp(originLeft + deltaX, 8, maxLeft)}px`;
    float.style.top = `${clamp(originTop + deltaY, 8, maxTop)}px`;
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

  const submitAuth = (
    action: (credentials: AuthCredentials) => void | Promise<void>,
  ): void => {
    const email = authEmail.value.trim();
    const password = authPassword.value;
    if (!email.includes("@") || password.length === 0) {
      authHint.hidden = false;
      authHint.textContent = "Enter your email address and password.";
      return;
    }
    authHint.hidden = true;
    authSignIn.disabled = true;
    authRegister.disabled = true;
    void (async (): Promise<void> => {
      try {
        await action({ email, password });
        authPassword.value = "";
      } catch (error: unknown) {
        authHint.hidden = false;
        authHint.textContent =
          error instanceof Error
            ? error.message
            : "Sign-in could not be completed. Try again.";
      } finally {
        authSignIn.disabled = false;
        authRegister.disabled = false;
      }
    })();
  };
  authSignIn.addEventListener("click", () => submitAuth(actions.signIn));
  authRegister.addEventListener("click", () => submitAuth(actions.register));
  authPassword.addEventListener("keydown", (event: KeyboardEvent) => {
    if (event.key === "Enter") {
      submitAuth(actions.signIn);
    }
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
    if (model.view !== "signed-out") {
      authHint.hidden = true;
    }
    primary.hidden = false;
    primary.disabled = false;
    if (model.view === "signed-out") {
      primary.hidden = true;
      delete primary.dataset.action;
    } else if (model.view === "stopped") {
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
