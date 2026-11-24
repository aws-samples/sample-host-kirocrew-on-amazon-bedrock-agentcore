import type {
  BrowserApplication,
  BrowserApplicationConfig,
} from "./browser-app.js";
import {
  KiroCrewRemoteEventSource,
  KiroCrewRemoteTransport,
  KiroCrewRemoteWebSocket,
  RemoteTransportError,
  syntheticRouteResponse,
} from "./remote-transport.js";
import {
  crewOwnerTwin,
  installOutboxDownloads,
  installSandboxImages,
  isSandboxDocStash,
  stashSandboxDoc,
} from "./local-routes.js";

export interface CapabilityState {
  readonly available: boolean;
  readonly hidden: boolean;
  readonly code?: "FEATURE_UNAVAILABLE_IN_AGENTCORE";
  readonly message?: string;
}

export const KIROCREW_AGENTCORE_CAPABILITIES = Object.freeze({
  chat: { available: true, hidden: false },
  artifacts: { available: true, hidden: false },
  memory: { available: true, hidden: false },
  knowledge: { available: true, hidden: false },
  mcp: { available: true, hidden: false },
  cron: { available: true, hidden: false },
  tasks: { available: true, hidden: false },
  settings: { available: true, hidden: false },
  computerUse: unavailable(true),
  desktop: unavailable(true),
  hostFilePicker: unavailable(false),
  localGatewayToken: unavailable(true),
  shutdown: unavailable(true),
  terminal: unavailable(false),
  tunnel: unavailable(true),
} satisfies Readonly<Record<string, CapabilityState>>);

interface EventSourceLike {
  readonly url: string;
  close(): void;
}

interface WebSocketLike {
  readonly url: string;
  close(code?: number, reason?: string): void;
}

interface EventSourceConstructor {
  readonly CONNECTING: number;
  readonly OPEN: number;
  readonly CLOSED: number;
  new (url: string | URL, options?: EventSourceInit): EventSourceLike;
}

interface WebSocketConstructor {
  readonly CONNECTING: number;
  readonly OPEN: number;
  readonly CLOSING: number;
  readonly CLOSED: number;
  new (
    url: string | URL,
    protocols?: string | readonly string[],
  ): WebSocketLike;
}

export interface BootstrapTarget {
  fetch: typeof fetch;
  EventSource: EventSourceConstructor;
  WebSocket: WebSocketConstructor;
  readonly location?: { readonly origin: string };
  readonly document?: Document;
  alert?: (message: string) => void;
  __KIROCREW_AGENTCORE_CAPABILITIES__?: typeof KIROCREW_AGENTCORE_CAPABILITIES;
  __KIROCREW_REMOTE_TRANSPORT__?: true;
}

function unavailable(hidden: boolean): CapabilityState {
  return {
    available: false,
    hidden,
    code: "FEATURE_UNAVAILABLE_IN_AGENTCORE",
    message: "This host-native feature is unavailable in AgentCore.",
  };
}

function inputUrl(input: RequestInfo | URL): string | URL {
  return input instanceof Request ? input.url : input;
}

/** The same request addressed to *url*; a `Request` keeps its method, headers and body. */
function readdressed(
  input: RequestInfo | URL,
  url: string,
  origin: string,
): RequestInfo | URL {
  return input instanceof Request
    ? new Request(new URL(url, origin), input)
    : url;
}

export function unavailablePresentation(
  error: unknown,
): CapabilityState | undefined {
  return error instanceof RemoteTransportError &&
    error.code === "FEATURE_UNAVAILABLE_IN_AGENTCORE"
    ? unavailable(false)
    : undefined;
}

/**
 * Answer same-origin `/api/*` traffic BEFORE a session exists, so the upstream
 * SPA's boot-time probes (notably `GET /api/kiro-prerequisite`) never fall
 * through to CloudFront's SPA fallback and choke on `text/html`. Synthetic
 * routes get their canonical synthetic bodies. Every other `/api/*` request
 * is HELD, not refused: the SPA loads before the sandbox is connected and
 * does not retry what failed, so a refusal here left "KiroCrew is not
 * connected yet" banners on screen long after the sandbox came up. A held
 * request is replayed through the remote transport the moment it is
 * installed (`__KIROCREW_REMOTE_TRANSPORT__`); while the sandbox is stopped
 * the SPA sits behind the status panel, so waiting is invisible. Aborting
 * the request's signal still rejects it. Once the transport is installed the
 * gate steps aside; if it is later uninstalled, the gate resumes.
 */
export function installPreSessionGate(
  target: BootstrapTarget,
  origin: string,
): () => void {
  const nativeFetch = target.fetch;
  const shellOrigin = new URL(origin).origin;
  const gate = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    if (target.__KIROCREW_REMOTE_TRANSPORT__ === true) {
      // The SPA can keep its own reference to `fetch` from before the
      // transport existed -- that reference is this gate. Sending its /api
      // traffic to native fetch then hits CloudFront, which answers with the
      // SPA's index.html: "Unexpected token '<' ... is not valid JSON" across
      // the sessions list and composer, on about two page loads in three
      // (whenever the SPA captured fetch before the sandbox connected).
      // So same-origin /api traffic goes to the CURRENT window fetch, the
      // transport's, which routes it into the sandbox. Everything else, and
      // the transport's own fall-through for routes it does not handle,
      // still reaches native fetch.
      if (target.fetch !== gate && routedByTransport(target, input)) {
        return await target.fetch(input, init);
      }
      return await nativeFetch.call(target, input, init);
    }
    const twin = crewOwnerTwin(input, shellOrigin);
    if (twin !== undefined) {
      return await target.fetch(readdressed(input, twin, shellOrigin), init);
    }
    let url: URL;
    try {
      url = new URL(inputUrl(input), shellOrigin);
    } catch {
      return await nativeFetch.call(target, input, init);
    }
    if (url.origin !== shellOrigin || !url.pathname.startsWith("/api/")) {
      return await nativeFetch.call(target, input, init);
    }
    const method =
      init?.method ?? (input instanceof Request ? input.method : "GET");
    const synthetic = syntheticRouteResponse(method, url.pathname);
    if (synthetic !== undefined) {
      return synthetic;
    }
    const signal =
      init?.signal ?? (input instanceof Request ? input.signal : undefined);
    await untilTransport(target, signal ?? undefined);
    return await target.fetch(input, init);
  };
  target.fetch = gate;
  return (): void => {
    target.fetch = nativeFetch;
  };
}

/**
 * Whether the installed transport routes a request itself -- the exact test its
 * own fetch applies, so a stale gate reference never defers a request the
 * transport would hand straight back to native fetch (that would loop).
 */
const transportRoutes = new WeakMap<
  BootstrapTarget,
  (input: RequestInfo | URL) => boolean
>();

function routedByTransport(
  target: BootstrapTarget,
  input: RequestInfo | URL,
): boolean {
  return transportRoutes.get(target)?.(input) ?? false;
}

const pendingTransport = new WeakMap<BootstrapTarget, (() => void)[]>();

function untilTransport(
  target: BootstrapTarget,
  signal: AbortSignal | undefined,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted === true) {
      reject(signal.reason as Error);
      return;
    }
    const waiters = pendingTransport.get(target) ?? [];
    pendingTransport.set(target, waiters);
    const release = (): void => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const abort = (): void => {
      const index = waiters.indexOf(release);
      if (index >= 0) {
        waiters.splice(index, 1);
      }
      reject(signal?.reason as Error);
    };
    signal?.addEventListener("abort", abort, { once: true });
    waiters.push(release);
  });
}

function releasePendingRequests(target: BootstrapTarget): void {
  const waiters = pendingTransport.get(target);
  if (waiters === undefined) {
    return;
  }
  pendingTransport.delete(target);
  for (const release of waiters) {
    release();
  }
}

export function installKiroCrewBootstrap(
  target: BootstrapTarget,
  transport: KiroCrewRemoteTransport,
): () => void {
  if (target.__KIROCREW_REMOTE_TRANSPORT__ === true) {
    throw new Error("KiroCrew remote transport is already installed.");
  }
  const nativeFetch = target.fetch;
  const NativeEventSource = target.EventSource;
  const NativeWebSocket = target.WebSocket;
  const origin = target.location?.origin ?? "http://localhost";

  target.fetch = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    if (isSandboxDocStash(input, init, origin)) {
      return await stashSandboxDoc(input, init);
    }
    const twin = crewOwnerTwin(input, origin);
    if (twin !== undefined) {
      return await transport.fetch(readdressed(input, twin, origin), init);
    }
    return transport.handles(inputUrl(input))
      ? await transport.fetch(input, init)
      : await nativeFetch.call(target, input, init);
  };

  const EventSourceBridge = function (
    this: unknown,
    url: string | URL,
    options?: EventSourceInit,
  ): EventSourceLike {
    return transport.handles(url)
      ? transport.eventSource(url)
      : new NativeEventSource(url, options);
  } as unknown as EventSourceConstructor;
  Object.defineProperties(EventSourceBridge, {
    CONNECTING: { value: KiroCrewRemoteEventSource.CONNECTING },
    OPEN: { value: KiroCrewRemoteEventSource.OPEN },
    CLOSED: { value: KiroCrewRemoteEventSource.CLOSED },
  });
  target.EventSource = EventSourceBridge;

  const WebSocketBridge = function (
    this: unknown,
    url: string | URL,
    protocols?: string | readonly string[],
  ): WebSocketLike {
    return transport.handles(url)
      ? transport.webSocket(url, protocols)
      : new NativeWebSocket(url, protocols);
  } as unknown as WebSocketConstructor;
  Object.defineProperties(WebSocketBridge, {
    CONNECTING: { value: KiroCrewRemoteWebSocket.CONNECTING },
    OPEN: { value: KiroCrewRemoteWebSocket.OPEN },
    CLOSING: { value: KiroCrewRemoteWebSocket.CLOSING },
    CLOSED: { value: KiroCrewRemoteWebSocket.CLOSED },
  });
  target.WebSocket = WebSocketBridge;

  target.__KIROCREW_AGENTCORE_CAPABILITIES__ = KIROCREW_AGENTCORE_CAPABILITIES;
  target.__KIROCREW_REMOTE_TRANSPORT__ = true;
  transportRoutes.set(
    target,
    (input) =>
      crewOwnerTwin(input, origin) !== undefined ||
      transport.handles(inputUrl(input)),
  );
  const uninstallDownloads =
    target.document === undefined
      ? (): void => undefined
      : installOutboxDownloads(target.document, {
          fetch: target.fetch,
          origin,
          notify: (message) => {
            target.alert?.(message);
          },
        });
  const uninstallImages =
    target.document === undefined
      ? (): void => undefined
      : installSandboxImages(target.document, { fetch: target.fetch, origin });
  releasePendingRequests(target);

  return (): void => {
    uninstallImages();
    uninstallDownloads();
    target.fetch = nativeFetch;
    target.EventSource = NativeEventSource;
    target.WebSocket = NativeWebSocket;
    delete target.__KIROCREW_AGENTCORE_CAPABILITIES__;
    delete target.__KIROCREW_REMOTE_TRANSPORT__;
    transportRoutes.delete(target);
  };
}

type BrowserBootstrapWindow = Window &
  BootstrapTarget & {
    __KIROCREW_AGENTCORE_CONFIG__?: WireBrowserApplicationConfig;
    __KIROCREW_AGENTCORE_APPLICATION__?: BrowserApplication;
  };

/**
 * The auth shape Terraform injects (`infrastructure/main.tf` public_config):
 * a same-origin base path for the gated auth API and the deployment's
 * allowed email domains (used only for user-facing hints; enforcement is
 * server side).
 */
export interface WireAuthConfig {
  readonly basePath?: string;
  readonly allowedDomains?: readonly string[];
}

export type WireBrowserApplicationConfig = Omit<
  BrowserApplicationConfig,
  "auth"
> & {
  readonly auth?: WireAuthConfig;
};

export function normalizeBrowserApplicationConfig(
  config: WireBrowserApplicationConfig,
): BrowserApplicationConfig {
  return {
    ...config,
    auth: {
      basePath: config.auth?.basePath ?? "/auth/v1",
      ...(config.auth?.allowedDomains === undefined
        ? {}
        : { allowedDomains: config.auth.allowedDomains }),
    },
  };
}

export async function startBrowserApplication(
  target: BrowserBootstrapWindow,
  config: BrowserApplicationConfig,
  root: HTMLElement = target.document.body,
): Promise<BrowserApplication> {
  if (target.__KIROCREW_AGENTCORE_APPLICATION__ !== undefined) {
    return target.__KIROCREW_AGENTCORE_APPLICATION__;
  }
  const { BrowserApplication: Application } = await import("./browser-app.js");
  const application = new Application(root, config, {
    target,
    installTransport: installKiroCrewBootstrap,
  });
  target.__KIROCREW_AGENTCORE_APPLICATION__ = application;
  await application.boot();
  return application;
}

const browserWindow =
  typeof window === "undefined"
    ? undefined
    : (window as unknown as BrowserBootstrapWindow);
if (browserWindow !== undefined) {
  // Installed synchronously at module scope: this module script executes
  // before the upstream SPA bundle, so the gate is in place before any
  // pre-session `/api/*` probe fires.
  installPreSessionGate(browserWindow, browserWindow.location.origin);
}
if (
  browserWindow !== undefined &&
  browserWindow.__KIROCREW_AGENTCORE_CONFIG__ !== undefined
) {
  await startBrowserApplication(
    browserWindow,
    normalizeBrowserApplicationConfig(
      browserWindow.__KIROCREW_AGENTCORE_CONFIG__,
    ),
  );
}
