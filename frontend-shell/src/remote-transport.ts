import { PROTOCOL_VERSION, type ProtocolError } from "./generated/protocol.js";

export const MAX_REQUEST_BODY_BYTES = 1024 * 1024;
export const MAX_RESPONSE_BODY_BYTES = 8 * 1024 * 1024;

const SAFE_REQUEST_HEADERS = new Set([
  "accept",
  "content-type",
  "if-match",
  "if-none-match",
  "range",
]);
const SAFE_RESPONSE_HEADERS = new Set([
  "cache-control",
  "content-range",
  "content-type",
  "etag",
  "last-modified",
]);
export const KIRO_PREREQUISITE_PATH = "/api/kiro-prerequisite";
const ONBOARDING_SCAN_PATH = "/api/onboarding/import/scan";
const ONBOARDING_STATE_PATH = "/api/onboarding/import/state";
const SYNTHETIC_ROUTES = new Set([
  "GET /api/auth/status",
  "GET /api/auth/local-token",
  "POST /api/auth/refresh",
  "POST /api/auth/logout",
  `GET ${KIRO_PREREQUISITE_PATH}`,
  // A fresh AgentCore microVM has no host setup to import, so an empty scan
  // is the truthful answer; the state write is acknowledged client-side.
  `GET ${ONBOARDING_SCAN_PATH}`,
  `PUT ${ONBOARDING_STATE_PATH}`,
]);
// Host-native families upstream does not register at all; a 501 "unavailable
// here" is truthful and friendlier than a bare 404, and costs no feature.
const UNAVAILABLE_PREFIXES = ["/api/desktop", "/api/files/pick"] as const;
// Mirror of KiroCrewRoutePolicy._DENIED_PREFIXES. Every other upstream route is
// tunnelled: the sandbox is a single-tenant microVM whose tenancy is enforced by
// the Cognito subject binding on each invocation, and /api/chat and
// /api/terminal already grant arbitrary in-sandbox execution, so filtering
// feature routes only breaks the product. These families either hand out the
// gateway's own session token or seize gateway lifecycle from the supervisor,
// and the upstream bundle never calls them.
const DENIED_PREFIXES = [
  "/api/token",
  "/api/auth/token",
  "/api/secrets",
  "/api/config/export",
  "/api/shutdown",
  "/api/restart",
] as const;

export type RouteDisposition =
  | "allowed"
  | "synthetic"
  | "unavailable"
  | "denied";

export interface AgentCoreInvocation {
  readonly version: typeof PROTOCOL_VERSION;
  readonly requestId: string;
  readonly bindingToken: string;
  readonly operation:
    | "kirocrew.http"
    | "kirocrew.ws.send"
    | "kirocrew.ws.close"
    | "kiro.login.start"
    | "kiro.status"
    | "kiro.logout";
  readonly payload: Readonly<Record<string, unknown>>;
}

export interface AgentCoreRuntimeEvent {
  readonly requestId: string;
  readonly sequence: number;
  readonly operation:
    | "request.accepted"
    | "output.delta"
    | "request.completed"
    | "error";
  readonly payload: Readonly<Record<string, unknown>>;
}

export interface AgentCoreDuplex {
  readonly events: AsyncIterable<AgentCoreRuntimeEvent>;
  send(frame: string | Uint8Array): void;
  close(code?: number, reason?: string): void;
}

export interface AgentCoreChannel {
  invoke(
    invocation: AgentCoreInvocation,
    signal?: AbortSignal,
  ): AsyncIterable<AgentCoreRuntimeEvent>;
  openWebSocket?(
    invocation: AgentCoreInvocation,
    signal?: AbortSignal,
  ): Promise<AgentCoreDuplex>;
}

export interface RemoteTransportOptions {
  readonly origin: string;
  readonly bindingToken: () => string;
  readonly requestId?: () => string;
  readonly maxRequestBodyBytes?: number;
  readonly maxResponseBodyBytes?: number;
}

export class RemoteTransportError extends Error {
  public constructor(
    public readonly code: ProtocolError["code"],
    message: string,
    public readonly status: number,
    public readonly retryable = false,
  ) {
    super(message);
    this.name = "RemoteTransportError";
  }
}

function hasPrefix(path: string, prefix: string): boolean {
  return path === prefix || path.startsWith(`${prefix}/`);
}

function canonicalOrigin(url: URL): string {
  if (url.protocol === "wss:") {
    return `https://${url.host}`;
  }
  if (url.protocol === "ws:") {
    return `http://${url.host}`;
  }
  return url.origin;
}

function validatedUrl(value: string | URL, origin: string): URL {
  const url = new URL(value, origin);
  if (
    canonicalOrigin(url) !== origin ||
    !url.pathname.startsWith("/api/") ||
    url.pathname.includes("//") ||
    url.pathname.split("/").some((part) => part === "." || part === "..")
  ) {
    throw new RemoteTransportError(
      "AUTHORIZATION_FAILED",
      "The upstream route is not permitted.",
      403,
    );
  }
  return url;
}

export function classifyKiroCrewRoute(
  method: string,
  value: string | URL,
  origin: string,
): RouteDisposition {
  let url: URL;
  try {
    url = validatedUrl(value, origin);
  } catch (error: unknown) {
    if (error instanceof RemoteTransportError) {
      return "denied";
    }
    throw error;
  }
  const normalizedMethod = method.toUpperCase();
  const path = url.pathname;
  if (DENIED_PREFIXES.some((prefix) => hasPrefix(path, prefix))) {
    return "denied";
  }
  if (UNAVAILABLE_PREFIXES.some((prefix) => hasPrefix(path, prefix))) {
    return "unavailable";
  }
  if (SYNTHETIC_ROUTES.has(`${normalizedMethod} ${path}`)) {
    return "synthetic";
  }
  return "allowed";
}

function defaultRequestId(): string {
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const random = crypto.getRandomValues(new Uint8Array(16));
  let timestamp = Date.now();
  let result = "";
  for (let index = 0; index < 10; index += 1) {
    result = alphabet[timestamp % 32] + result;
    timestamp = Math.floor(timestamp / 32);
  }
  for (let index = 0; index < 16; index += 1) {
    result += alphabet[(random[index] ?? 0) & 31];
  }
  return result;
}

function encodeBase64(value: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < value.length; offset += 0x8000) {
    binary += String.fromCharCode(...value.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function safeHeaders(
  headers: Headers,
  allowlist: ReadonlySet<string>,
): Headers {
  const result = new Headers();
  for (const [key, value] of headers.entries()) {
    if (allowlist.has(key.toLowerCase())) {
      result.set(key.toLowerCase(), value);
    }
  }
  return result;
}

export interface KiroPrerequisiteSnapshot {
  readonly platform: string;
  readonly installed: boolean;
  readonly authenticated: boolean;
  readonly ready: boolean;
  readonly repair_required: boolean;
  readonly initial_setup_complete: boolean;
  readonly docs_url: string;
  readonly login_command: string;
  readonly sso_login_command: string;
  readonly sandbox_unavailable: boolean;
  readonly sandbox_failure_kind: string;
  readonly sandbox_detail: string;
  readonly sandbox_remedy: string;
  readonly missing_agent_specs: readonly string[];
  readonly agent_spec_repair_error: string;
  readonly operation: Readonly<Record<string, string>>;
}

/**
 * Synthetic upstream `GET /api/kiro-prerequisite` body. Field-for-field the
 * `asdict(PrerequisiteStatus)` shape plus the legacy `operation` shim
 * (`legacy_idle_operation()`) that pre-upgrade clients poll. `installed` and
 * `ready` hold by construction: kiro-cli is baked into the AgentCore image,
 * and the real Kiro login state is driven by the in-session
 * `kiro.auth_required` protocol event rather than this probe.
 */
export function kiroPrerequisiteSnapshot(): KiroPrerequisiteSnapshot {
  return {
    platform: "linux",
    installed: true,
    authenticated: true,
    ready: true,
    repair_required: false,
    initial_setup_complete: true,
    docs_url: "https://kiro.dev/cli/",
    login_command: "kiro-cli login",
    sso_login_command: "kiro-cli login --use-device-flow --license pro",
    sandbox_unavailable: false,
    sandbox_failure_kind: "",
    sandbox_detail: "",
    sandbox_remedy: "",
    missing_agent_specs: [],
    agent_spec_repair_error: "",
    operation: {
      kind: "",
      status: "idle",
      message: "",
      detail: "",
      url: "",
      error: "",
    },
  };
}

function syntheticRoute(path: string): Response {
  if (path === KIRO_PREREQUISITE_PATH) {
    return Response.json(kiroPrerequisiteSnapshot(), { status: 200 });
  }
  if (path === ONBOARDING_SCAN_PATH) {
    return Response.json({ sources: [], skipped: [] }, { status: 200 });
  }
  if (path === ONBOARDING_STATE_PATH) {
    return Response.json({ completed: true }, { status: 200 });
  }
  const body =
    path === "/api/auth/logout"
      ? { disconnected: true, remoteSession: true }
      : {
          authenticated: true,
          mode: "agentcore",
          remoteSession: true,
          token: null,
        };
  return Response.json(body, { status: 200 });
}

/**
 * Return the synthetic response for a `method path` pair, or `undefined` when
 * the route is not synthetic. Shared by the transport and the pre-session
 * gate so both answer identically.
 */
export function syntheticRouteResponse(
  method: string,
  path: string,
): Response | undefined {
  return SYNTHETIC_ROUTES.has(`${method.toUpperCase()} ${path}`)
    ? syntheticRoute(path)
    : undefined;
}

function featureResponse(disposition: "unavailable" | "denied"): Response {
  const unavailable = disposition === "unavailable";
  return Response.json(
    {
      code: unavailable
        ? "FEATURE_UNAVAILABLE_IN_AGENTCORE"
        : "AUTHORIZATION_FAILED",
      message: unavailable
        ? "This host-native feature is unavailable in AgentCore."
        : "The upstream route is not permitted.",
      retryable: false,
    },
    { status: unavailable ? 501 : 403 },
  );
}

function eventError(
  payload: Readonly<Record<string, unknown>>,
): RemoteTransportError {
  const code =
    typeof payload.code === "string"
      ? (payload.code as ProtocolError["code"])
      : "INTERNAL_ERROR";
  const message =
    typeof payload.message === "string"
      ? payload.message
      : "The remote KiroCrew request failed.";
  const status = typeof payload.status === "number" ? payload.status : 500;
  return new RemoteTransportError(
    code,
    message,
    status,
    payload.retryable === true,
  );
}

export class KiroCrewRemoteTransport {
  readonly #origin: string;
  readonly #channel: AgentCoreChannel;
  readonly #bindingToken: () => string;
  readonly #requestId: () => string;
  readonly #maxRequestBodyBytes: number;
  readonly #maxResponseBodyBytes: number;

  public constructor(
    channel: AgentCoreChannel,
    options: RemoteTransportOptions,
  ) {
    this.#origin = new URL(options.origin).origin;
    this.#channel = channel;
    this.#bindingToken = options.bindingToken;
    this.#requestId = options.requestId ?? defaultRequestId;
    this.#maxRequestBodyBytes =
      options.maxRequestBodyBytes ?? MAX_REQUEST_BODY_BYTES;
    this.#maxResponseBodyBytes =
      options.maxResponseBodyBytes ?? MAX_RESPONSE_BODY_BYTES;
    if (this.#maxRequestBodyBytes <= 0 || this.#maxResponseBodyBytes <= 0) {
      throw new RangeError("Remote transport limits must be positive.");
    }
  }

  public handles(value: string | URL): boolean {
    try {
      const url = new URL(value, this.#origin);
      return (
        canonicalOrigin(url) === this.#origin &&
        url.pathname.startsWith("/api/")
      );
    } catch {
      return false;
    }
  }

  public resolve(value: string | URL): URL {
    return validatedUrl(value, this.#origin);
  }

  public async fetch(
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    const request =
      input instanceof Request
        ? new Request(input, init)
        : new Request(new URL(input, this.#origin), init);
    const url = validatedUrl(request.url, this.#origin);
    const disposition = classifyKiroCrewRoute(
      request.method,
      url,
      this.#origin,
    );
    if (disposition === "synthetic") {
      return syntheticRoute(url.pathname);
    }
    if (disposition !== "allowed") {
      return featureResponse(disposition);
    }
    const body = new Uint8Array(await request.arrayBuffer());
    if (body.byteLength > this.#maxRequestBodyBytes) {
      throw new RemoteTransportError(
        "FRAME_TOO_LARGE",
        "The upstream request body is too large.",
        413,
      );
    }
    return await this.#collect(
      this.#invocation(request.method, url, "http", body, request.headers),
      request.signal,
    );
  }

  public eventSource(url: string | URL): KiroCrewRemoteEventSource {
    return new KiroCrewRemoteEventSource(this, url);
  }

  public webSocket(
    url: string | URL,
    protocols?: string | readonly string[],
  ): KiroCrewRemoteWebSocket {
    return new KiroCrewRemoteWebSocket(this, url, protocols);
  }

  public stream(
    method: string,
    value: string | URL,
    transport: "sse" | "websocket",
    body: Uint8Array,
    headers: Headers,
    signal?: AbortSignal,
    frames: readonly string[] = [],
    onRequestId?: (requestId: string) => void,
  ): AsyncIterable<AgentCoreRuntimeEvent> {
    const url = validatedUrl(value, this.#origin);
    const disposition = classifyKiroCrewRoute(method, url, this.#origin);
    if (disposition !== "allowed") {
      throw new RemoteTransportError(
        disposition === "unavailable"
          ? "FEATURE_UNAVAILABLE_IN_AGENTCORE"
          : "AUTHORIZATION_FAILED",
        disposition === "unavailable"
          ? "This host-native feature is unavailable in AgentCore."
          : "The upstream route is not permitted.",
        disposition === "unavailable" ? 501 : 403,
      );
    }
    const invocation = this.#invocation(
      method,
      url,
      transport,
      body,
      headers,
      frames,
    );
    onRequestId?.(invocation.requestId);
    return this.#channel.invoke(invocation, signal);
  }

  /** Forward a live frame into an open upstream WebSocket tunnel. */
  public async tunnelSend(
    tunnelId: string,
    data: string | Uint8Array,
  ): Promise<void> {
    const binary = typeof data !== "string";
    const invocation: AgentCoreInvocation = {
      version: PROTOCOL_VERSION,
      requestId: this.#requestId(),
      bindingToken: this.#bindingToken(),
      operation: "kirocrew.ws.send",
      payload: {
        tunnelId,
        data: binary ? encodeBase64(data) : data,
        encoding: binary ? "base64" : "utf8",
      },
    };
    for await (const event of this.#channel.invoke(invocation)) {
      if (event.operation === "error") {
        throw eventError(event.payload);
      }
    }
  }

  /** Ask the adapter to close an upstream WebSocket tunnel. */
  public async tunnelClose(tunnelId: string): Promise<void> {
    const invocation: AgentCoreInvocation = {
      version: PROTOCOL_VERSION,
      requestId: this.#requestId(),
      bindingToken: this.#bindingToken(),
      operation: "kirocrew.ws.close",
      payload: { tunnelId },
    };
    for await (const event of this.#channel.invoke(invocation)) {
      void event;
    }
  }

  public async openWebSocket(
    value: string | URL,
    signal?: AbortSignal,
  ): Promise<AgentCoreDuplex> {
    if (this.#channel.openWebSocket === undefined) {
      throw new RemoteTransportError(
        "FEATURE_UNAVAILABLE_IN_AGENTCORE",
        "This WebSocket feature is unavailable in AgentCore.",
        501,
      );
    }
    const url = validatedUrl(value, this.#origin);
    if (classifyKiroCrewRoute("GET", url, this.#origin) !== "allowed") {
      throw new RemoteTransportError(
        "FEATURE_UNAVAILABLE_IN_AGENTCORE",
        "This WebSocket feature is unavailable in AgentCore.",
        501,
      );
    }
    return await this.#channel.openWebSocket(
      this.#invocation(
        "GET",
        url,
        "websocket",
        new Uint8Array(),
        new Headers(),
      ),
      signal,
    );
  }

  #invocation(
    method: string,
    url: URL,
    transport: "http" | "sse" | "websocket",
    body: Uint8Array,
    headers: Headers,
    frames: readonly string[] = [],
  ): AgentCoreInvocation {
    const safe = safeHeaders(headers, SAFE_REQUEST_HEADERS);
    return {
      version: PROTOCOL_VERSION,
      requestId: this.#requestId(),
      bindingToken: this.#bindingToken(),
      operation: "kirocrew.http",
      payload: {
        method: method.toUpperCase(),
        path: `${url.pathname}${url.search}`,
        transport,
        headers: Object.fromEntries(safe.entries()),
        body: encodeBase64(body),
        bodyEncoding: "base64",
        frames: [...frames],
      },
    };
  }

  async #collect(
    invocation: AgentCoreInvocation,
    signal?: AbortSignal,
  ): Promise<Response> {
    let expectedSequence = 0;
    let status = 502;
    let accepted = false;
    let completed = false;
    const headers = new Headers();
    const chunks: Uint8Array[] = [];
    let total = 0;
    for await (const event of this.#channel.invoke(invocation, signal)) {
      if (
        event.requestId !== invocation.requestId ||
        event.sequence !== expectedSequence
      ) {
        throw new RemoteTransportError(
          "SEQUENCE_ERROR",
          "Remote response events are incomplete or out of order.",
          502,
        );
      }
      expectedSequence += 1;
      if (event.operation === "error") {
        throw eventError(event.payload);
      }
      if (event.operation === "request.accepted") {
        accepted = true;
        status =
          typeof event.payload.status === "number" ? event.payload.status : 200;
        const responseHeaders = event.payload.headers;
        if (
          typeof responseHeaders === "object" &&
          responseHeaders !== null &&
          !Array.isArray(responseHeaders)
        ) {
          for (const [key, value] of Object.entries(responseHeaders)) {
            if (
              SAFE_RESPONSE_HEADERS.has(key.toLowerCase()) &&
              typeof value === "string"
            ) {
              headers.set(key.toLowerCase(), value);
            }
          }
        }
      } else if (event.operation === "output.delta") {
        const data = event.payload.data;
        const encoding = event.payload.encoding;
        if (typeof data !== "string") {
          throw new RemoteTransportError(
            "INVALID_MESSAGE",
            "Remote response data is invalid.",
            502,
          );
        }
        const chunk =
          encoding === "base64"
            ? decodeBase64(data)
            : new TextEncoder().encode(data);
        total += chunk.byteLength;
        if (total > this.#maxResponseBodyBytes) {
          throw new RemoteTransportError(
            "FRAME_TOO_LARGE",
            "The upstream response body is too large.",
            413,
          );
        }
        chunks.push(chunk);
      } else if (event.operation === "request.completed") {
        completed = true;
      }
    }
    if (!accepted || !completed) {
      throw new RemoteTransportError(
        "KIROCREW_UNAVAILABLE",
        "The upstream response did not complete.",
        502,
        true,
      );
    }
    const body = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return new Response(body, { status, headers });
  }
}

interface ParsedSseEvent {
  readonly type: string;
  readonly data: string;
  readonly lastEventId: string;
}

function parseSseBlock(block: string): ParsedSseEvent | undefined {
  let type = "message";
  let lastEventId = "";
  const data: string[] = [];
  for (const line of block.split(/\r?\n/u)) {
    if (line.startsWith("event:")) {
      type = line.slice(6).trimStart();
    } else if (line.startsWith("id:")) {
      lastEventId = line.slice(3).trimStart();
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart());
    }
  }
  return data.length === 0
    ? undefined
    : { type, data: data.join("\n"), lastEventId };
}

export class KiroCrewRemoteEventSource extends EventTarget {
  public static readonly CONNECTING = 0;
  public static readonly OPEN = 1;
  public static readonly CLOSED = 2;

  public readonly url: string;
  public readonly withCredentials = false;
  public readyState = KiroCrewRemoteEventSource.CONNECTING;
  public onopen: ((event: Event) => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onerror: ((event: Event) => void) | null = null;
  readonly #abort = new AbortController();
  readonly #transport: KiroCrewRemoteTransport;

  public constructor(transport: KiroCrewRemoteTransport, url: string | URL) {
    super();
    this.#transport = transport;
    this.url = transport.resolve(url).href;
    void this.#connect();
  }

  public close(): void {
    this.#abort.abort();
    this.readyState = KiroCrewRemoteEventSource.CLOSED;
  }

  async #connect(): Promise<void> {
    let buffer = "";
    let expectedSequence = 0;
    try {
      this.readyState = KiroCrewRemoteEventSource.OPEN;
      const open = new Event("open");
      this.dispatchEvent(open);
      this.onopen?.(open);
      const events = this.#transport.stream(
        "GET",
        this.url,
        "sse",
        new Uint8Array(),
        new Headers({ accept: "text/event-stream" }),
        this.#abort.signal,
      );
      for await (const event of events) {
        if (event.sequence !== expectedSequence) {
          throw new RemoteTransportError(
            "SEQUENCE_ERROR",
            "Remote SSE events are out of order.",
            502,
          );
        }
        expectedSequence += 1;
        if (event.operation === "error") {
          throw eventError(event.payload);
        }
        if (event.operation !== "output.delta") {
          continue;
        }
        const data = event.payload.data;
        if (typeof data !== "string") {
          throw new RemoteTransportError(
            "INVALID_MESSAGE",
            "Remote SSE data is invalid.",
            502,
          );
        }
        const chunk =
          event.payload.encoding === "base64"
            ? new TextDecoder().decode(decodeBase64(data))
            : data;
        buffer += chunk;
        let boundary = buffer.search(/\r?\n\r?\n/u);
        while (boundary >= 0) {
          const parsed = parseSseBlock(buffer.slice(0, boundary));
          const separator = buffer.slice(boundary).startsWith("\r\n\r\n")
            ? 4
            : 2;
          buffer = buffer.slice(boundary + separator);
          if (parsed !== undefined) {
            const message = new MessageEvent<string>(parsed.type, {
              data: parsed.data,
              lastEventId: parsed.lastEventId,
              origin: new URL(this.url).origin,
            });
            this.dispatchEvent(message);
            if (parsed.type === "message") {
              this.onmessage?.(message);
            }
          }
          boundary = buffer.search(/\r?\n\r?\n/u);
        }
      }
      this.close();
    } catch (error: unknown) {
      if (this.#abort.signal.aborted) {
        return;
      }
      this.readyState = KiroCrewRemoteEventSource.CLOSED;
      const event = new Event("error");
      Object.defineProperty(event, "error", { value: error });
      this.dispatchEvent(event);
      this.onerror?.(event);
    }
  }
}

export class KiroCrewRemoteWebSocket extends EventTarget {
  public static readonly CONNECTING = 0;
  public static readonly OPEN = 1;
  public static readonly CLOSING = 2;
  public static readonly CLOSED = 3;

  public readonly url: string;
  public readonly protocol = "";
  public readonly extensions = "";
  public binaryType: BinaryType = "blob";
  public readyState = KiroCrewRemoteWebSocket.CONNECTING;
  public bufferedAmount = 0;
  public onopen: ((event: Event) => void) | null = null;
  public onmessage: ((event: MessageEvent) => void) | null = null;
  public onerror: ((event: Event) => void) | null = null;
  public onclose: ((event: CloseEvent) => void) | null = null;
  readonly #abort = new AbortController();
  readonly #transport: KiroCrewRemoteTransport;
  #tunnelId: string | undefined;

  public constructor(
    transport: KiroCrewRemoteTransport,
    url: string | URL,
    protocols?: string | readonly string[],
  ) {
    super();
    this.#transport = transport;
    this.url = transport.resolve(url).href;
    if (
      protocols !== undefined &&
      (typeof protocols === "string" ? protocols : protocols.length > 0)
    ) {
      throw new RemoteTransportError(
        "AUTHORIZATION_FAILED",
        "Upstream WebSocket subprotocols are not permitted.",
        403,
      );
    }
    // Connect on a macrotask so callers can attach handlers first. Like a
    // native socket, `open` fires only once the upstream handshake succeeded
    // (the tunnel's `request.accepted`). Announcing `open` before a tunnel
    // existed made the upstream SPA treat every failed attempt as a fresh
    // connection: it reset its reconnect backoff each time and refetched its
    // whole dashboard, which turned a dead gateway into a request storm.
    setTimeout(() => {
      void this.#connect();
    }, 0);
  }

  public send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    if (this.readyState !== KiroCrewRemoteWebSocket.OPEN) {
      throw new DOMException("WebSocket is not open.", "InvalidStateError");
    }
    const tunnelId = this.#tunnelId;
    if (tunnelId === undefined) {
      // Cannot happen once open, but the tunnel id is what carries frames.
      return;
    }
    const bytes =
      typeof data === "string"
        ? data
        : data instanceof Blob
          ? undefined
          : new Uint8Array(
              data instanceof ArrayBuffer
                ? data
                : ArrayBuffer.isView(data)
                  ? data.buffer.slice(
                      data.byteOffset,
                      data.byteOffset + data.byteLength,
                    )
                  : new ArrayBuffer(0),
            );
    if (bytes === undefined) {
      void (data as Blob).arrayBuffer().then((buffer) => {
        void this.#transport
          .tunnelSend(tunnelId, new Uint8Array(buffer))
          .catch(() => this.#failed());
      });
      return;
    }
    void this.#transport
      .tunnelSend(tunnelId, bytes)
      .catch(() => this.#failed());
  }

  #failed(): void {
    if (this.readyState !== KiroCrewRemoteWebSocket.OPEN) {
      return;
    }
    const event = new Event("error");
    this.dispatchEvent(event);
    this.onerror?.(event);
  }

  public close(code = 1000, reason = ""): void {
    if (this.readyState === KiroCrewRemoteWebSocket.CLOSED) {
      return;
    }
    const tunnelId = this.#tunnelId;
    if (tunnelId !== undefined) {
      void this.#transport.tunnelClose(tunnelId).catch(() => undefined);
    }
    this.#abort.abort();
    this.#closed(code, reason, true);
  }

  #open(): void {
    if (
      this.#abort.signal.aborted ||
      this.readyState !== KiroCrewRemoteWebSocket.CONNECTING
    ) {
      return;
    }
    this.readyState = KiroCrewRemoteWebSocket.OPEN;
    const open = new Event("open");
    this.dispatchEvent(open);
    this.onopen?.(open);
  }

  async #connect(): Promise<void> {
    if (this.#abort.signal.aborted) {
      return;
    }
    let expectedSequence = 0;
    try {
      const events = this.#transport.stream(
        "GET",
        this.url,
        "websocket",
        new Uint8Array(),
        new Headers(),
        this.#abort.signal,
        [],
        (requestId): void => {
          this.#tunnelId = requestId;
        },
      );
      for await (const event of events) {
        if (event.sequence !== expectedSequence) {
          throw new RemoteTransportError(
            "SEQUENCE_ERROR",
            "Remote WebSocket events are out of order.",
            502,
          );
        }
        expectedSequence += 1;
        if (event.operation === "error") {
          throw eventError(event.payload);
        }
        if (event.operation === "request.accepted") {
          // The adapter connected to the upstream socket: now we are open.
          this.#open();
          continue;
        }
        if (event.operation === "output.delta") {
          const raw = event.payload.data;
          if (typeof raw !== "string") {
            throw new RemoteTransportError(
              "INVALID_MESSAGE",
              "Remote WebSocket data is invalid.",
              502,
            );
          }
          const binary = event.payload.encoding === "base64";
          const bytes = binary ? decodeBase64(raw) : undefined;
          const data: string | ArrayBuffer | Blob = binary
            ? this.binaryType === "arraybuffer"
              ? (bytes?.buffer.slice(
                  bytes.byteOffset,
                  bytes.byteOffset + bytes.byteLength,
                ) as ArrayBuffer)
              : new Blob([
                  (bytes ?? new Uint8Array()).buffer.slice(
                    bytes?.byteOffset ?? 0,
                    (bytes?.byteOffset ?? 0) + (bytes?.byteLength ?? 0),
                  ) as ArrayBuffer,
                ])
            : raw;
          const message = new MessageEvent("message", { data });
          this.dispatchEvent(message);
          this.onmessage?.(message);
        } else if (event.operation === "request.completed") {
          break;
        }
      }
      this.#closed(1000, "", true);
    } catch (error: unknown) {
      if (this.#abort.signal.aborted) {
        return;
      }
      const event = new Event("error");
      Object.defineProperty(event, "error", { value: error });
      this.dispatchEvent(event);
      this.onerror?.(event);
      // 1006 when the handshake never completed, 1011 when a live tunnel
      // failed: the SPA's reconnect logic keys its backoff off `close`.
      this.#closed(
        this.readyState === KiroCrewRemoteWebSocket.OPEN ? 1011 : 1006,
        "Remote WebSocket failed.",
        false,
      );
    }
  }

  #closed(code: number, reason: string, clean: boolean): void {
    if (this.readyState === KiroCrewRemoteWebSocket.CLOSED) {
      return;
    }
    this.readyState = KiroCrewRemoteWebSocket.CLOSED;
    const event =
      typeof CloseEvent === "undefined"
        ? Object.assign(new Event("close"), { code, reason, wasClean: clean })
        : new CloseEvent("close", {
            code,
            reason,
            wasClean: clean,
          });
    this.dispatchEvent(event);
    this.onclose?.(event);
  }
}
