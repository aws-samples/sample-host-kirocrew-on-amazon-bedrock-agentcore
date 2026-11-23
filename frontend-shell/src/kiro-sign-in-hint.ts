/**
 * Point a new user at Kiro sign-in before their first message fails.
 *
 * A fresh sandbox has no Kiro sign-in: the KiroCrew dashboard opens straight
 * on an empty chat, the first message fails, and the way out is a link in an
 * error dialog to Settings -> Agent Harness. On a laptop the official app can
 * reuse an existing Kiro CLI sign-in, so the gap rarely shows there; in a new
 * microVM it always does.
 *
 * This asks upstream's own `GET /api/kas-login/sources` which sign-in sessions
 * use. When it answers `active: "none"` (neither Kiro Crew's nor kiro-cli's),
 * it shows one dismissible notice whose button opens upstream's sign-in page.
 * Sign-in itself stays entirely upstream's. The notice re-checks while it is
 * shown and disappears once a sign-in exists. Any other answer, or an error,
 * shows nothing: the notice must never cry wolf.
 */

export const SIGN_IN_PATH = "/settings/agent";
const SOURCES_PATH = "/api/kas-login/sources";
const STYLE_ID = "kirocrew-agentcore-sign-in-hint-style";
const NOTICE_ID = "kirocrew-agentcore-sign-in-hint";
export const SIGN_IN_POLL_MS = 10_000;

export interface SignInHintOptions {
  readonly fetch: typeof fetch;
  readonly document: Document;
  readonly navigate: (path: string) => void;
  /** The page's current path; the notice stays out of the way on the sign-in page. */
  readonly currentPath?: () => string;
  readonly setTimeout: (callback: () => void, delay: number) => unknown;
  readonly clearTimeout: (handle: unknown) => void;
  readonly pollMs?: number;
}

type SignInState = "none" | "signed-in" | "unknown";

async function signInState(fetchFn: typeof fetch): Promise<SignInState> {
  try {
    const response = await fetchFn(SOURCES_PATH, { method: "GET" });
    if (!response.ok) {
      return "unknown";
    }
    const body = (await response.json()) as { active?: unknown };
    if (body.active === "none") {
      return "none";
    }
    return body.active === "kiro_crew" || body.active === "kiro_cli"
      ? "signed-in"
      : "unknown";
  } catch {
    return "unknown";
  }
}

function installStyles(doc: Document): void {
  if (doc.getElementById(STYLE_ID) !== null) {
    return;
  }
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    #${NOTICE_ID} {
      position: fixed;
      left: 50%;
      bottom: 112px;
      transform: translateX(-50%);
      z-index: 2147482000;
      display: flex;
      align-items: center;
      gap: 12px;
      max-width: min(560px, calc(100vw - 24px));
      padding: 12px 14px;
      border: 1px solid var(--accent, #7c3aed);
      border-radius: 12px;
      background: var(--card, #fff);
      color: var(--card-fg, var(--text, #111));
      box-shadow: 0 12px 32px color-mix(in srgb, #000 20%, transparent);
      font-size: 13px;
      line-height: 1.45;
    }
    #${NOTICE_ID} strong { display: block; font-size: 14px; }
    #${NOTICE_ID} .kcac-hint-text { flex: 1; min-width: 0; }
    #${NOTICE_ID} .kcac-hint-go {
      flex: none;
      padding: 7px 12px;
      border: 0;
      border-radius: 8px;
      background: var(--accent, #7c3aed);
      color: var(--accent-fg, #fff);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }
    #${NOTICE_ID} .kcac-hint-close {
      flex: none;
      border: 0;
      background: transparent;
      color: inherit;
      opacity: .6;
      font-size: 18px;
      line-height: 1;
      cursor: pointer;
    }
    #${NOTICE_ID} button:focus-visible {
      outline: 3px solid var(--accent, #7c3aed);
      outline-offset: 2px;
    }
  `;
  doc.head.appendChild(style);
}

/** Start checking; returns a function that removes the notice and stops checking. */
export function installKiroSignInHint(options: SignInHintOptions): () => void {
  const doc = options.document;
  const pollMs = options.pollMs ?? SIGN_IN_POLL_MS;
  let stopped = false;
  let dismissed = false;
  let timer: unknown;
  let notice: HTMLElement | undefined;

  const hide = (): void => {
    notice?.remove();
    notice = undefined;
  };
  const show = (): void => {
    if (notice !== undefined || dismissed) {
      return;
    }
    installStyles(doc);
    const element = doc.createElement("div");
    element.id = NOTICE_ID;
    element.setAttribute("role", "status");
    const text = doc.createElement("div");
    text.className = "kcac-hint-text";
    const title = doc.createElement("strong");
    title.textContent = "Sign in to Kiro to start chatting";
    const detail = doc.createElement("span");
    detail.textContent =
      "Your sandbox needs a Kiro sign-in before the first message. You only do this once; it is kept across restarts.";
    text.append(title, detail);
    const go = doc.createElement("button");
    go.type = "button";
    go.className = "kcac-hint-go";
    go.textContent = "Sign in";
    go.addEventListener("click", () => {
      hide();
      options.navigate(SIGN_IN_PATH);
    });
    const close = doc.createElement("button");
    close.type = "button";
    close.className = "kcac-hint-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => {
      dismissed = true;
      hide();
    });
    element.append(text, go, close);
    doc.body.appendChild(element);
    notice = element;
  };
  const check = async (): Promise<void> => {
    const state = await signInState(options.fetch);
    if (stopped) {
      return;
    }
    if (state === "none") {
      if (options.currentPath?.() === SIGN_IN_PATH) {
        hide();
      } else {
        show();
      }
      // Keep looking while the notice is up, so it goes away by itself once
      // the user finishes signing in (or signs in some other way).
      if (!dismissed) {
        timer = options.setTimeout(() => {
          timer = undefined;
          void check();
        }, pollMs);
      }
      return;
    }
    hide();
  };
  void check();
  return (): void => {
    stopped = true;
    if (timer !== undefined) {
      options.clearTimeout(timer);
      timer = undefined;
    }
    hide();
  };
}

/** Client-side navigation the upstream router follows (it listens to popstate). */
export function navigateInPage(target: Window, path: string): void {
  if (target.location.pathname === path) {
    return;
  }
  target.history.pushState({}, "", path);
  target.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
}
