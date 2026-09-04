import {
  PROTOCOL_VERSION,
  type ProtocolEnvelope,
} from "./generated/protocol.js";
import { validateEnvelope } from "./protocol.js";
import type {
  AgentCoreChannel,
  AgentCoreDuplex,
  AgentCoreInvocation,
  AgentCoreRuntimeEvent,
} from "./remote-transport.js";

const AUTHORIZATION_PROTOCOL = "base64UrlBearerAuthorization";
const AGENTCORE_HOST_PREFIX = "bedrock-agentcore";

export interface RuntimeConnectionDescriptor {
  readonly sandboxId: string;
  readonly runtimeSessionId: string;
  readonly state:
    | "STOPPED"
    | "STARTING"
    | "RESTORING"
    | "READY"
    | "BUSY"
    | "CHECKPOINTING"
    | "STOPPING"
    | "ERROR";
  readonly runtimeArn: string;
  readonly qualifier: string;
  readonly httpUrl: string;
  readonly webSocketUrl: string;
  readonly bindingToken: string;
  readonly protocolVersion: typeof PROTOCOL_VERSION;
  readonly frontendCompatibilityVersion: string;
  readonly expiresAt: string;
}

export interface AgentCoreBrowserChannelOptions {
  readonly region: string;
  readonly accessToken: () => Promise<string>;
  readonly fetch?: typeof fetch;
  readonly webSocket?: typeof WebSocket;
  readonly requestId?: () => string;
  readonly now?: () => Date;
  readonly onEnvelope?: (envelope: ProtocolEnvelope) => void;
  readonly onClose?: () => void;
}

export class AgentCoreConnectionError extends Error {
  public constructor(
    public readonly code:
      | "AUTHENTICATION_FAILED"
      | "INVALID_RUNTIME_URL"
      | "TRANSPORT_FAILED",
    message: string,
    public readonly retryable: boolean,
  ) {
    super(message);
    this.name = "AgentCoreConnectionError";
  }
}

function base64UrlText(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

export function agentCoreWebSocketProtocols(
  accessToken: string,
): readonly [string, string] {
  if (!accessToken || /\s/u.test(accessToken)) {
    throw new AgentCoreConnectionError(
      "AUTHENTICATION_FAILED",
      "A valid access token is required.",
      false,
    );
  }
  return [
    `${AUTHORIZATION_PROTOCOL}.${base64UrlText(accessToken)}`,
    AUTHORIZATION_PROTOCOL,
  ];
}

function dnsSuffix(region: string): string {
  return region.startsWith("cn-") ? "amazonaws.com.cn" : "amazonaws.com";
}

export function validateAgentCoreRuntimeUrl(
  value: string,
  region: string,
  transport: "http" | "websocket",
): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new AgentCoreConnectionError(
      "INVALID_RUNTIME_URL",
      "The runtime endpoint is invalid.",
      false,
    );
  }
  const expectedProtocol = transport === "http" ? "https:" : "wss:";
  const expectedHost = `${AGENTCORE_HOST_PREFIX}.${region}.${dnsSuffix(region)}`;
  const authority = /^[a-z]+:\/\/([^/]+)/iu.exec(value)?.[1];
  const rawPath = value.slice(value.indexOf("/", value.indexOf("//") + 2));
  if (
    !/^[a-z]{2}(?:-gov)?-[a-z]+-\d$/u.test(region) ||
    url.protocol !== expectedProtocol ||
    authority !== expectedHost ||
    url.hostname !== expectedHost ||
    url.port ||
    url.username ||
    url.password ||
    url.hash ||
    !url.pathname.startsWith("/runtimes/") ||
    rawPath.includes("//") ||
    /(?:^|\/)(?:\.|%2e){1,2}(?:\/|$)/iu.test(rawPath)
  ) {
    throw new AgentCoreConnectionError(
      "INVALID_RUNTIME_URL",
      "The runtime endpoint is not permitted.",
      false,
    );
  }
  return url;
}

function defaultUlid(): string {
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const random = crypto.getRandomValues(new Uint8Array(16));
  let timestamp = Date.now();
  let value = "";
  for (let index = 0; index < 10; index += 1) {
    value = alphabet[timestamp % 32] + value;
    timestamp = Math.floor(timestamp / 32);
  }
  for (const byte of random) {
    value += alphabet[byte & 31];
  }
  return value;
}

function runtimeEvent(envelope: ProtocolEnvelope): AgentCoreRuntimeEvent {
  if (
    envelope.operation !== "request.accepted" &&
    envelope.operation !== "output.delta" &&
    envelope.operation !== "request.completed" &&
    envelope.operation !== "error"
  ) {
    throw new AgentCoreConnectionError(
      "TRANSPORT_FAILED",
      "The runtime returned an unexpected event.",
      false,
    );
  }
  if (envelope.requestId === undefined || envelope.requestId === null) {
    throw new AgentCoreConnectionError(
      "TRANSPORT_FAILED",
      "The runtime response is missing a request identifier.",
      false,
    );
  }
  return {
    requestId: envelope.requestId,
    sequence: envelope.sequence,
    operation: envelope.operation,
    payload: envelope.payload,
  };
}

async function* parseSse(
  response: Response,
): AsyncGenerator<ProtocolEnvelope, void, void> {
  if (response.body === null) {
    throw new AgentCoreConnectionError(
      "TRANSPORT_FAILED",
      "The runtime stream is unavailable.",
      true,
    );
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) {
        break;
      }
      buffer += result.value;
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = block
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (data) {
          yield validateEnvelope(JSON.parse(data) as unknown);
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch (error: unknown) {
    if (error instanceof AgentCoreConnectionError) {
      throw error;
    }
    throw new AgentCoreConnectionError(
      "TRANSPORT_FAILED",
      "The runtime stream was interrupted.",
      true,
    );
  } finally {
    reader.releaseLock();
  }
}

class AsyncEventQueue implements AsyncIterable<AgentCoreRuntimeEvent> {
  readonly #values: AgentCoreRuntimeEvent[] = [];
  readonly #waiters: Array<
    (result: IteratorResult<AgentCoreRuntimeEvent, void>) => void
  > = [];
  #closed = false;
  #error: Error | undefined;

  public push(value: AgentCoreRuntimeEvent): void {
    const waiter = this.#waiters.shift();
    if (waiter === undefined) {
      this.#values.push(value);
    } else {
      waiter({ done: false, value });
    }
  }

  public fail(error: Error): void {
    this.#error = error;
    this.close();
  }

  public close(): void {
    this.#closed = true;
    for (const waiter of this.#waiters.splice(0)) {
      waiter({ done: true, value: undefined });
    }
  }

  public [Symbol.asyncIterator](): AsyncIterator<AgentCoreRuntimeEvent> {
    return {
      next: (): Promise<IteratorResult<AgentCoreRuntimeEvent, void>> => {
        const value = this.#values.shift();
        if (value !== undefined) {
          return Promise.resolve({ done: false, value });
        }
        if (this.#error !== undefined) {
          return Promise.reject(this.#error);
        }
        if (this.#closed) {
          return Promise.resolve({ done: true, value: undefined });
        }
        return new Promise((resolve) => this.#waiters.push(resolve));
      },
    };
  }
}

class BrowserAgentCoreDuplex implements AgentCoreDuplex {
  public readonly events: AsyncIterable<AgentCoreRuntimeEvent>;
  readonly #socket: WebSocket;
  readonly #queue: AsyncEventQueue;

  public constructor(socket: WebSocket, queue: AsyncEventQueue) {
    this.#socket = socket;
    this.#queue = queue;
    this.events = queue;
  }

  public send(frame: string | Uint8Array): void {
    if (this.#socket.readyState !== WebSocket.OPEN) {
      throw new AgentCoreConnectionError(
        "TRANSPORT_FAILED",
        "The runtime connection is not open.",
        true,
      );
    }
    this.#socket.send(frame);
  }

  public close(code = 1000, reason = "Browser disconnected"): void {
    this.#socket.close(code, reason);
    this.#queue.close();
  }
}

export class AgentCoreBrowserChannel implements AgentCoreChannel {
  readonly #descriptor: RuntimeConnectionDescriptor;
  readonly #region: string;
  readonly #accessToken: () => Promise<string>;
  readonly #fetch: typeof fetch;
  readonly #webSocket: typeof WebSocket;
  readonly #requestId: () => string;
  readonly #now: () => Date;
  readonly #onEnvelope: (envelope: ProtocolEnvelope) => void;
  readonly #onClose: () => void;

  public constructor(
    descriptor: RuntimeConnectionDescriptor,
    options: AgentCoreBrowserChannelOptions,
  ) {
    if (
      descriptor.protocolVersion !== PROTOCOL_VERSION ||
      Date.parse(descriptor.expiresAt) <= Date.now() ||
      descriptor.bindingToken.length < 32
    ) {
      throw new AgentCoreConnectionError(
        "TRANSPORT_FAILED",
        "The runtime connection descriptor is invalid or expired.",
        false,
      );
    }
    validateAgentCoreRuntimeUrl(descriptor.httpUrl, options.region, "http");
    validateAgentCoreRuntimeUrl(
      descriptor.webSocketUrl,
      options.region,
      "websocket",
    );
    this.#descriptor = descriptor;
    this.#region = options.region;
    this.#accessToken = options.accessToken;
    this.#fetch = options.fetch ?? fetch;
    this.#webSocket = options.webSocket ?? WebSocket;
    this.#requestId = options.requestId ?? defaultUlid;
    this.#now = options.now ?? ((): Date => new Date());
    this.#onEnvelope = options.onEnvelope ?? ((): void => undefined);
    this.#onClose = options.onClose ?? ((): void => undefined);
  }

  public async *invoke(
    invocation: AgentCoreInvocation,
    signal?: AbortSignal,
  ): AsyncGenerator<AgentCoreRuntimeEvent, void, void> {
    const endpoint = validateAgentCoreRuntimeUrl(
      this.#descriptor.httpUrl,
      this.#region,
      "http",
    );
    const token = await this.#accessToken();
    const requestInit: RequestInit = {
      method: "POST",
      headers: {
        accept: "text/event-stream, application/json",
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "x-amzn-bedrock-agentcore-runtime-session-id":
          this.#descriptor.runtimeSessionId,
      },
      body: JSON.stringify(invocation),
      credentials: "omit",
      referrerPolicy: "no-referrer",
    };
    if (signal !== undefined) {
      requestInit.signal = signal;
    }
    let response: Response;
    try {
      response = await this.#fetch(endpoint, requestInit);
    } catch {
      throw new AgentCoreConnectionError(
        "TRANSPORT_FAILED",
        "The runtime could not be reached.",
        true,
      );
    }
    if (response.status === 401 || response.status === 403) {
      throw new AgentCoreConnectionError(
        "AUTHENTICATION_FAILED",
        "Your sign-in has expired. Sign in again.",
        false,
      );
    }
    if (!response.ok) {
      throw new AgentCoreConnectionError(
        "TRANSPORT_FAILED",
        "The runtime request failed. Try again.",
        response.status === 409 ||
          response.status === 429 ||
          response.status >= 500,
      );
    }
    for await (const envelope of parseSse(response)) {
      this.#onEnvelope(envelope);
      if (
        envelope.operation === "request.accepted" ||
        envelope.operation === "output.delta" ||
        envelope.operation === "request.completed" ||
        envelope.operation === "error"
      ) {
        yield runtimeEvent(envelope);
      }
    }
  }

  public async openWebSocket(
    _invocation: AgentCoreInvocation,
    signal?: AbortSignal,
  ): Promise<AgentCoreDuplex> {
    const endpoint = validateAgentCoreRuntimeUrl(
      this.#descriptor.webSocketUrl,
      this.#region,
      "websocket",
    );
    // Browsers cannot set headers on a WebSocket handshake; AgentCore accepts
    // the session pin as a query parameter. Without it every connection lands
    // in a fresh runtime session and restore fails against the sandbox record.
    endpoint.searchParams.set(
      "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
      this.#descriptor.runtimeSessionId,
    );
    const token = await this.#accessToken();
    const socket = new this.#webSocket(endpoint, [
      ...agentCoreWebSocketProtocols(token),
    ]);
    const queue = new AsyncEventQueue();
    const correlationId = this.#requestId();
    const hello: ProtocolEnvelope = {
      version: PROTOCOL_VERSION,
      messageId: this.#requestId(),
      operation: "connection.hello",
      sequence: 0,
      timestamp: this.#now().toISOString(),
      correlationId,
      payload: { bindingToken: this.#descriptor.bindingToken },
    };
    return await new Promise<AgentCoreDuplex>((resolve, reject) => {
      const onAbort = (): void => socket.close(1000, "Connection cancelled");
      signal?.addEventListener("abort", onAbort, { once: true });
      socket.addEventListener(
        "open",
        () => {
          socket.send(JSON.stringify(hello));
          resolve(new BrowserAgentCoreDuplex(socket, queue));
        },
        { once: true },
      );
      socket.addEventListener("message", (event: MessageEvent<unknown>) => {
        try {
          if (typeof event.data !== "string") {
            throw new Error("Text frame required");
          }
          const envelope = validateEnvelope(JSON.parse(event.data) as unknown);
          this.#onEnvelope(envelope);
          if (
            envelope.operation === "error" &&
            (envelope.requestId === undefined || envelope.requestId === null)
          ) {
            // Connection-scoped error (no requestId): surfaced via onEnvelope
            // above; end the duplex cleanly instead of forcing it through the
            // per-request event path, which requires a request identifier.
            const message = envelope.payload.message;
            queue.fail(
              new AgentCoreConnectionError(
                "TRANSPORT_FAILED",
                typeof message === "string"
                  ? message
                  : "The runtime connection failed.",
                envelope.payload.retryable === true,
              ),
            );
            socket.close(1000, "Runtime connection error");
            return;
          }
          if (
            envelope.operation === "request.accepted" ||
            envelope.operation === "output.delta" ||
            envelope.operation === "request.completed" ||
            envelope.operation === "error"
          ) {
            queue.push(runtimeEvent(envelope));
          }
        } catch {
          queue.fail(
            new AgentCoreConnectionError(
              "TRANSPORT_FAILED",
              "The runtime returned an invalid message.",
              false,
            ),
          );
          // 1002 is a reserved code the browser refuses to send; use the
          // application-defined range instead.
          socket.close(3002, "Invalid runtime message");
        }
      });
      socket.addEventListener(
        "close",
        () => {
          queue.close();
          this.#onClose();
        },
        { once: true },
      );
      socket.addEventListener(
        "error",
        () => {
          const error = new AgentCoreConnectionError(
            "TRANSPORT_FAILED",
            "The runtime connection failed.",
            true,
          );
          queue.fail(error);
          reject(error);
        },
        { once: true },
      );
    });
  }
}
