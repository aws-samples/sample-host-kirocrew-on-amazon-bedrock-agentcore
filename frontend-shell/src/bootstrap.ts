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
 * routes get their canonical synthetic bodies; every other `/api/*` request
 * gets a retryable 503 JSON error. Once the real remote transport is
 * installed (`__KIROCREW_REMOTE_TRANSPORT__`), the gate steps aside; if the
 * transport is later uninstalled, the gate resumes automatically.
 */
export function installPreSessionGate(
  target: BootstrapTarget,
  origin: string,
): () => void {
  const nativeFetch = target.fetch;
  const shellOrigin = new URL(origin).origin;
  target.fetch = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    if (target.__KIROCREW_REMOTE_TRANSPORT__ === true) {
      return await nativeFetch.call(target, input, init);
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
    return Response.json(
      {
        code: "KIROCREW_UNAVAILABLE",
        message:
          "KiroCrew is not connected yet. Sign in and start a session first.",
        retryable: true,
      },
      { status: 503 },
    );
  };
  return (): void => {
    target.fetch = nativeFetch;
  };
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

  target.fetch = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> =>
    transport.handles(inputUrl(input))
      ? await transport.fetch(input, init)
      : await nativeFetch.call(target, input, init);

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

  return (): void => {
    target.fetch = nativeFetch;
    target.EventSource = NativeEventSource;
    target.WebSocket = NativeWebSocket;
    delete target.__KIROCREW_AGENTCORE_CAPABILITIES__;
    delete target.__KIROCREW_REMOTE_TRANSPORT__;
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
