import {
  AgentCoreBrowserChannel,
  type RuntimeConnectionDescriptor,
} from "./agentcore-channel.js";
import type { BootstrapTarget } from "./bootstrap.js";
import {
  LifecycleStore,
  classifyRuntimeError,
  type SandboxDetails,
  type SandboxHistoryEvent,
  type SandboxSnapshot,
  type SandboxState,
} from "./lifecycle.js";
import {
  PasswordAuthClient,
  PasswordAuthError,
  type PasswordAuthConfig,
} from "./auth.js";
import {
  PROTOCOL_VERSION,
  type ProtocolEnvelope,
} from "./generated/protocol.js";
import {
  KiroCrewRemoteTransport,
  type AgentCoreDuplex,
  type AgentCoreInvocation,
} from "./remote-transport.js";
import { mountBrowserShell, type BrowserShellHandle } from "./shell.js";

const CONTROL_PATH = "/control/v1";
const POLL_INTERVAL_MS = 2_000;
// The adapter closes the runtime WebSocket after 40 seconds without client
// traffic (heartbeat timeout), so a keepalive ping must fire well inside that.
const PING_INTERVAL_MS = 20_000;

export interface BrowserApplicationConfig {
  readonly auth: PasswordAuthConfig;
  readonly region: string;
  readonly shellOrigin: string;
  readonly upstreamOrigin: string;
  readonly frontendCompatibilityVersion: string;
}

export interface BrowserApplicationOptions {
  readonly target?: BootstrapTarget;
  readonly storage?: Storage;
  readonly fetch?: typeof fetch;
  readonly crypto?: Crypto;
  readonly now?: () => number;
  readonly setTimeout?: typeof setTimeout;
  readonly clearTimeout?: typeof clearTimeout;
  readonly installTransport?: (
    target: BootstrapTarget,
    transport: KiroCrewRemoteTransport,
  ) => () => void;
}

export class BrowserApplicationError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
    public readonly retryable = false,
  ) {
    super(message);
    this.name = "BrowserApplicationError";
  }
}

interface ErrorResponse {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSandboxState(value: unknown): value is SandboxState {
  return (
    value === "STOPPED" ||
    value === "STARTING" ||
    value === "RESTORING" ||
    value === "READY" ||
    value === "BUSY" ||
    value === "CHECKPOINTING" ||
    value === "STOPPING" ||
    value === "ERROR"
  );
}

function parseDetails(value: unknown): SandboxDetails {
  const record = (value ?? {}) as {
    events?: unknown;
    persistedPaths?: unknown;
  };
  const events = Array.isArray(record.events) ? record.events : [];
  const paths = Array.isArray(record.persistedPaths)
    ? record.persistedPaths
    : [];
  return {
    events: events.filter(
      (entry): entry is SandboxHistoryEvent =>
        typeof entry === "object" &&
        entry !== null &&
        typeof (entry as SandboxHistoryEvent).at === "string" &&
        typeof (entry as SandboxHistoryEvent).state === "string" &&
        typeof (entry as SandboxHistoryEvent).stateVersion === "number",
    ),
    persistedPaths: paths.filter(
      (entry): entry is string => typeof entry === "string",
    ),
  };
}

function parseSandbox(value: unknown): SandboxSnapshot {
  if (
    !isRecord(value) ||
    typeof value.sandboxId !== "string" ||
    !isSandboxState(value.state) ||
    !Number.isSafeInteger(value.stateVersion) ||
    (value.lastCheckpointAt !== null &&
      value.lastCheckpointAt !== undefined &&
      typeof value.lastCheckpointAt !== "string") ||
    (value.lastRestore !== null &&
      value.lastRestore !== undefined &&
      typeof value.lastRestore !== "string") ||
    typeof value.updatedAt !== "string"
  ) {
    throw new BrowserApplicationError(
      "INVALID_MESSAGE",
      "The sandbox status response is invalid.",
    );
  }
  return {
    sandboxId: value.sandboxId,
    state: value.state,
    stateVersion: Number(value.stateVersion),
    lastCheckpointAt:
      typeof value.lastCheckpointAt === "string"
        ? value.lastCheckpointAt
        : null,
    lastRestore:
      typeof value.lastRestore === "string" ? value.lastRestore : null,
    updatedAt: value.updatedAt,
  };
}

function parseDescriptor(value: unknown): RuntimeConnectionDescriptor {
  if (
    !isRecord(value) ||
    typeof value.sandboxId !== "string" ||
    typeof value.runtimeSessionId !== "string" ||
    !isSandboxState(value.state) ||
    typeof value.runtimeArn !== "string" ||
    typeof value.qualifier !== "string" ||
    typeof value.httpUrl !== "string" ||
    typeof value.webSocketUrl !== "string" ||
    typeof value.bindingToken !== "string" ||
    value.protocolVersion !== PROTOCOL_VERSION ||
    typeof value.frontendCompatibilityVersion !== "string" ||
    typeof value.expiresAt !== "string"
  ) {
    throw new BrowserApplicationError(
      "INVALID_MESSAGE",
      "The runtime connection response is invalid.",
    );
  }
  return value as unknown as RuntimeConnectionDescriptor;
}

function safeError(value: unknown, status: number): ErrorResponse {
  if (isRecord(value)) {
    return {
      code: typeof value.code === "string" ? value.code : "INTERNAL_ERROR",
      message:
        typeof value.message === "string"
          ? value.message
          : "The request could not be completed.",
      retryable:
        value.retryable === true ||
        status === 409 ||
        status === 429 ||
        status >= 500,
    };
  }
  return {
    code: "INTERNAL_ERROR",
    message: "The request could not be completed.",
    retryable: status === 409 || status === 429 || status >= 500,
  };
}

function ulid(cryptoValue: Crypto, now: number): string {
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const random = cryptoValue.getRandomValues(new Uint8Array(16));
  let timestamp = now;
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

class ControlApiClient {
  readonly #origin: string;
  readonly #accessToken: () => Promise<string>;
  readonly #fetch: typeof fetch;
  readonly #idempotencyKey: () => string;

  public constructor(
    origin: string,
    accessToken: () => Promise<string>,
    fetchValue: typeof fetch,
    idempotencyKey: () => string,
  ) {
    const parsed = new URL(origin);
    if (parsed.protocol !== "https:" || parsed.origin !== origin) {
      throw new BrowserApplicationError(
        "CONFIGURATION",
        "The browser shell origin must be an HTTPS origin.",
      );
    }
    this.#origin = origin;
    this.#accessToken = accessToken;
    this.#fetch = fetchValue;
    this.#idempotencyKey = idempotencyKey;
  }

  public async status(): Promise<SandboxSnapshot> {
    return parseSandbox(await this.#request("GET", "/sandbox"));
  }

  public async start(): Promise<RuntimeConnectionDescriptor> {
    return parseDescriptor(
      await this.#request("POST", "/sandbox/start", undefined, true),
    );
  }

  public async stop(checkpointReceipt: string): Promise<SandboxSnapshot> {
    return parseSandbox(
      await this.#request("POST", "/sandbox/stop", { checkpointReceipt }, true),
    );
  }

  public async history(): Promise<SandboxDetails> {
    return parseDetails(await this.#request("GET", "/sandbox/history"));
  }

  async #request(
    method: "GET" | "POST",
    path: string,
    body?: Readonly<Record<string, unknown>>,
    idempotent = false,
  ): Promise<unknown> {
    const token = await this.#accessToken();
    const headers = new Headers({
      accept: "application/json",
      authorization: `Bearer ${token}`,
    });
    if (body !== undefined) {
      headers.set("content-type", "application/json");
    }
    if (idempotent) {
      headers.set("idempotency-key", this.#idempotencyKey());
    }
    let response: Response;
    try {
      response = await this.#fetch(
        new URL(`${CONTROL_PATH}${path}`, this.#origin),
        {
          method,
          headers,
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
          credentials: "omit",
          referrerPolicy: "same-origin",
        },
      );
    } catch {
      throw new BrowserApplicationError(
        "TRANSPORT_FAILED",
        "The control service could not be reached.",
        true,
      );
    }
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      value = undefined;
    }
    if (!response.ok) {
      const error = safeError(value, response.status);
      throw new BrowserApplicationError(
        error.code,
        error.message,
        error.retryable,
      );
    }
    return value;
  }
}

export class BrowserApplication {
  readonly #config: BrowserApplicationConfig;
  readonly #target: BootstrapTarget;
  readonly #auth: PasswordAuthClient;
  readonly #store: LifecycleStore;
  readonly #control: ControlApiClient;
  readonly #crypto: Crypto;
  readonly #now: () => number;
  readonly #setTimeout: typeof setTimeout;
  readonly #clearTimeout: typeof clearTimeout;
  readonly #installTransport: (
    target: BootstrapTarget,
    transport: KiroCrewRemoteTransport,
  ) => () => void;
  readonly #shell: BrowserShellHandle;
  #duplex: AgentCoreDuplex | undefined;
  #kiroChannel?: AgentCoreBrowserChannel;
  #kiroDescriptor?: RuntimeConnectionDescriptor;
  #reconnectAttempts = 0;
  #startInFlight = false;
  #lastDescriptor: RuntimeConnectionDescriptor | undefined;
  #reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  #autoOpenTimer: ReturnType<typeof setTimeout> | undefined;
  #autoOpenedCode: string | undefined;
  #kiroPollTimer: ReturnType<typeof setTimeout> | undefined;
  #uninstallBootstrap: (() => void) | undefined;
  #pollTimer: ReturnType<typeof setTimeout> | undefined;
  #pingTimer: ReturnType<typeof setTimeout> | undefined;
  #clientSequence = 1;
  #connectionGeneration = 0;
  #pendingStop = false;
  #destroyed = false;

  public constructor(
    root: HTMLElement,
    config: BrowserApplicationConfig,
    options: BrowserApplicationOptions = {},
  ) {
    this.#config = config;
    this.#target = options.target ?? (window as unknown as BootstrapTarget);
    const storage = options.storage ?? sessionStorage;
    const fetchValue = options.fetch ?? fetch;
    this.#crypto = options.crypto ?? crypto;
    this.#now = options.now ?? Date.now;
    // Bare `window.setTimeout` invoked as a method of this class throws
    // "Illegal invocation" in browsers; bind the defaults to globalThis.
    this.#setTimeout = options.setTimeout ?? setTimeout.bind(globalThis);
    this.#clearTimeout = options.clearTimeout ?? clearTimeout.bind(globalThis);
    this.#installTransport =
      options.installTransport ??
      ((): (() => void) => {
        throw new BrowserApplicationError(
          "CONFIGURATION",
          "The remote transport installer is unavailable.",
        );
      });
    this.#auth = new PasswordAuthClient(config.auth, {
      storage,
      fetch: fetchValue,
      now: this.#now,
    });
    this.#store = new LifecycleStore(
      this.#auth.session() !== undefined,
      this.#now,
    );
    this.#control = new ControlApiClient(
      config.shellOrigin,
      (): Promise<string> => this.#auth.accessToken(),
      fetchValue,
      (): string => ulid(this.#crypto, this.#now()),
    );
    this.#shell = mountBrowserShell(root, {
      signIn: (credentials): Promise<void> => this.signIn(credentials),
      register: (credentials): Promise<void> => this.register(credentials),
      confirmEmail: (request): Promise<void> => this.confirmEmail(request),
      resendCode: (credentials): Promise<void> =>
        this.#auth.resendCode(credentials.email, credentials.password),
      forgotPassword: (email): Promise<void> =>
        this.#auth.forgotPassword(email),
      resetPassword: (request): Promise<void> =>
        this.#auth.resetPassword(request.email, request.code, request.password),
      start: (): Promise<void> => this.start(),
      stop: (): void => this.stop(),
      retry: (): Promise<void> => this.retry(),
      logout: (): void => this.logout(),
      kiroCheck: (): void => this.kiroCheck(),
      kiroLogin: (options): void => this.kiroLogin(options),
      kiroLogout: (): void => this.kiroLogout(),
    });
    this.#store.subscribe((model) => this.#shell.render(model));
  }

  public lifecycle(): LifecycleStore {
    return this.#store;
  }

  public async boot(): Promise<void> {
    if (this.#auth.session() === undefined) {
      this.#store.dispatch({ type: "signed-out" });
      return;
    }
    await this.#refreshStatus(true);
  }

  public async signIn(credentials: {
    readonly email: string;
    readonly password: string;
  }): Promise<void> {
    // Failures surface in the sign-in form; the page never becomes locked.
    await this.#auth.signIn(credentials.email, credentials.password);
    await this.#refreshStatus(true);
  }

  public async register(credentials: {
    readonly email: string;
    readonly password: string;
  }): Promise<void> {
    // Registration ends at the confirmation step: the emailed code, entered
    // in the form, both verifies the address and signs the user in.
    await this.#auth.register(credentials.email, credentials.password);
  }

  public async confirmEmail(request: {
    readonly email: string;
    readonly password: string;
    readonly code: string;
  }): Promise<void> {
    await this.#auth.confirmEmail(
      request.email,
      request.password,
      request.code,
    );
    await this.#refreshStatus(true);
  }

  public async start(): Promise<void> {
    if (this.#destroyed || this.#startInFlight) {
      return;
    }
    this.#startInFlight = true;
    this.#store.dispatch({ type: "start-requested", at: this.#now() });
    try {
      const descriptor = await this.#control.start();
      this.#lastDescriptor = descriptor;
      if (
        descriptor.frontendCompatibilityVersion !==
        this.#config.frontendCompatibilityVersion
      ) {
        throw new BrowserApplicationError(
          "UNSUPPORTED_PROTOCOL",
          "The browser and runtime versions are incompatible.",
        );
      }
      await this.#connect(descriptor);
      this.#scheduleStatusPoll();
    } catch (error: unknown) {
      this.#terminal(error);
    } finally {
      this.#startInFlight = false;
    }
  }

  public stop(): void {
    if (this.#duplex === undefined || this.#pendingStop) {
      return;
    }
    this.#pendingStop = true;
    this.#store.dispatch({ type: "stop-requested" });
    this.#send("sandbox.prepare_stop", {}, ulid(this.#crypto, this.#now()));
  }

  #scheduleReconnect(): void {
    if (this.#destroyed || this.#reconnectAttempts >= 5) {
      return;
    }
    if (this.#reconnectTimer !== undefined) {
      this.#clearTimeout(this.#reconnectTimer);
    }
    const delay = Math.min(3000 * 2 ** this.#reconnectAttempts, 45000);
    this.#reconnectAttempts += 1;
    this.#reconnectTimer = this.#setTimeout(() => {
      this.#reconnectTimer = undefined;
      const model = this.#store.snapshot();
      const recoverable =
        model.view === "reconnecting" ||
        (model.view === "terminal-error" && model.error?.retryable === true);
      if (!recoverable) {
        return;
      }
      void this.retry().finally(() => {
        const next = this.#store.snapshot().view;
        if (next === "reconnecting" || next === "terminal-error") {
          this.#scheduleReconnect();
        }
      });
    }, delay);
  }

  public async retry(): Promise<void> {
    if (this.#destroyed) {
      return;
    }
    const active = this.#store.snapshot().activeRequestId;
    if (active !== undefined && this.#store.snapshot().activeRequestAccepted) {
      await this.#refreshStatus(false);
      return;
    }
    const descriptor = this.#lastDescriptor;
    if (
      descriptor !== undefined &&
      Date.parse(descriptor.expiresAt) - this.#now() > 60_000
    ) {
      // Reconnect with the binding we already hold. Re-acquiring the start
      // lease would reset the sandbox state machine underneath a running
      // initialization.
      this.#store.dispatch({ type: "start-requested", at: this.#now() });
      try {
        await this.#connect(descriptor);
        this.#scheduleStatusPoll();
        return;
      } catch {
        this.#lastDescriptor = undefined;
      }
    }
    await this.start();
  }

  public logout(): void {
    this.#disconnect();
    this.#auth.clear();
    this.#store.dispatch({ type: "signed-out" });
  }

  public destroy(): void {
    this.#destroyed = true;
    this.#disconnect();
    this.#shell.destroy();
  }

  async #connect(descriptor: RuntimeConnectionDescriptor): Promise<void> {
    this.#disconnect();
    const connectionGeneration = ++this.#connectionGeneration;
    const channel = new AgentCoreBrowserChannel(descriptor, {
      region: this.#config.region,
      accessToken: (): Promise<string> => this.#auth.accessToken(),
      onEnvelope: (envelope): void => this.#onEnvelope(envelope),
      onClose: (): void => {
        if (
          !this.#destroyed &&
          connectionGeneration === this.#connectionGeneration
        ) {
          this.#store.dispatch({ type: "connection-lost" });
          // Self-heal before asking the user to act: the retry button stays
          // available the whole time and takes over when backoff runs out.
          this.#scheduleReconnect();
        }
      },
    });
    const transport = new KiroCrewRemoteTransport(channel, {
      origin: this.#config.upstreamOrigin,
      bindingToken: (): string => descriptor.bindingToken,
    });
    this.#uninstallBootstrap = this.#installTransport(this.#target, transport);
    const invocation: AgentCoreInvocation = {
      version: PROTOCOL_VERSION,
      requestId: ulid(this.#crypto, this.#now()),
      bindingToken: descriptor.bindingToken,
      operation: "kirocrew.http",
      payload: { method: "GET", path: "/api/events", transport: "websocket" },
    };
    // Registered before the WebSocket opens: connection.ready arrives during
    // the handshake and the resulting kiroCheck() needs the channel in place.
    this.#kiroChannel = channel;
    this.#kiroDescriptor = descriptor;
    this.#duplex = await channel.openWebSocket(invocation);
    this.#schedulePing();
    // The Kiro status probe waits for connection.ready: a second invocation
    // during cold-start session initialization races the initializer and
    // fails the sandbox (ConditionalCheckFailedException).
    void connectionGeneration;
  }

  #scheduleKiroPoll(): void {
    if (this.#kiroPollTimer !== undefined) {
      return;
    }
    this.#kiroPollTimer = this.#setTimeout(() => {
      this.#kiroPollTimer = undefined;
      const model = this.#store.snapshot();
      if (model.deviceFlow === undefined || this.#destroyed) {
        return;
      }
      // Silent probe: no "checking" flash while the banner is up. Once the
      // user authorizes in the browser, this flips the panel to signed in
      // even if the login stream itself was cut short.
      const channel = this.#kiroChannel;
      const descriptor = this.#kiroDescriptor;
      if (channel !== undefined && descriptor !== undefined) {
        void this.#invokeKiro(channel, descriptor, "kiro.status", {}, true);
      }
      this.#scheduleKiroPoll();
    }, 10000);
  }

  public kiroCheck(): void {
    const channel = this.#kiroChannel;
    const descriptor = this.#kiroDescriptor;
    if (channel === undefined || descriptor === undefined) {
      return;
    }
    this.#store.dispatch({ type: "kiro-status", state: "checking" });
    void this.#invokeKiro(channel, descriptor, "kiro.status", {});
  }

  public kiroLogin(options?: {
    readonly method: "sso";
    readonly startUrl: string;
    readonly region: string;
  }): void {
    const channel = this.#kiroChannel;
    const descriptor = this.#kiroDescriptor;
    if (channel === undefined || descriptor === undefined) {
      return;
    }
    this.#store.dispatch({ type: "kiro-status", state: "checking" });
    const payload =
      options === undefined
        ? { method: "builder-id" }
        : {
            method: "sso",
            startUrl: options.startUrl,
            region: options.region,
          };
    void this.#invokeKiro(channel, descriptor, "kiro.login.start", payload);
  }

  public kiroLogout(): void {
    const channel = this.#kiroChannel;
    const descriptor = this.#kiroDescriptor;
    if (channel === undefined || descriptor === undefined) {
      return;
    }
    this.#store.dispatch({ type: "kiro-status", state: "checking" });
    void this.#invokeKiro(channel, descriptor, "kiro.logout", {});
  }

  async #invokeKiro(
    channel: AgentCoreBrowserChannel,
    descriptor: RuntimeConnectionDescriptor,
    operation: "kiro.status" | "kiro.login.start" | "kiro.logout",
    payload: Record<string, string>,
    retried = false,
  ): Promise<void> {
    // Ask the runtime for Kiro CLI auth state. If a login is required the
    // adapter starts the device flow and streams kiro.auth_required /
    // kiro.authenticated, which arrive through onEnvelope and drive the
    // device-code banner. Already-authenticated sessions answer immediately.
    const invocation: AgentCoreInvocation = {
      version: PROTOCOL_VERSION,
      requestId: ulid(this.#crypto, this.#now()),
      bindingToken: descriptor.bindingToken,
      operation,
      payload,
    };
    try {
      for await (const event of channel.invoke(invocation)) {
        void event;
      }
    } catch {
      // Kiro auth failures surface through kiro.auth_status / expiry or a
      // fresh attempt on the next connect; a broken probe must not take the
      // session down. Status probes get one delayed retry because the very
      // first probe can race the sandbox becoming healthy.
      if (operation === "kiro.status" && !retried) {
        this.#setTimeout(() => {
          if (channel === this.#kiroChannel) {
            void this.#invokeKiro(
              channel,
              descriptor,
              operation,
              payload,
              true,
            );
          }
        }, 5000);
        return;
      }
      if (
        operation === "kiro.login.start" &&
        this.#store.snapshot().deviceFlow !== undefined
      ) {
        // The device code is already on screen: a dropped stream is not a
        // sign-in failure. The status poll reports the real outcome.
        this.#scheduleKiroPoll();
        return;
      }
      if (operation === "kiro.status" && retried) {
        // A silent poll retry must not overwrite a live device-flow banner.
        if (this.#store.snapshot().deviceFlow !== undefined) {
          return;
        }
      }
      this.#store.dispatch({ type: "kiro-status", state: "failed" });
    }
  }

  #schedulePing(): void {
    if (this.#pingTimer !== undefined) {
      this.#clearTimeout(this.#pingTimer);
    }
    this.#pingTimer = this.#setTimeout(() => {
      this.#pingTimer = undefined;
      if (this.#destroyed || this.#duplex === undefined) {
        return;
      }
      try {
        this.#send("ping", {});
      } catch {
        return;
      }
      this.#schedulePing();
    }, PING_INTERVAL_MS);
  }

  #send(
    operation: "sandbox.prepare_stop" | "ping",
    payload: Readonly<Record<string, unknown>>,
    requestId?: string,
  ): void {
    if (this.#duplex === undefined) {
      throw new BrowserApplicationError(
        "TRANSPORT_FAILED",
        "The runtime connection is unavailable.",
        true,
      );
    }
    const envelope: ProtocolEnvelope = {
      version: PROTOCOL_VERSION,
      messageId: ulid(this.#crypto, this.#now()),
      ...(requestId === undefined ? {} : { requestId }),
      operation,
      sequence: this.#clientSequence,
      timestamp: new Date(this.#now()).toISOString(),
      correlationId: ulid(this.#crypto, this.#now()),
      payload,
    };
    this.#clientSequence += 1;
    this.#duplex.send(JSON.stringify(envelope));
  }

  #onEnvelope(envelope: ProtocolEnvelope): void {
    const requestId = envelope.requestId;
    if (envelope.operation === "connection.ready") {
      this.#store.dispatch({ type: "connection-ready" });
      this.#reconnectAttempts = 0;
      if (this.#reconnectTimer !== undefined) {
        this.#clearTimeout(this.#reconnectTimer);
        this.#reconnectTimer = undefined;
      }
      this.kiroCheck();
    } else if (envelope.operation === "sandbox.state") {
      void this.#refreshStatus(false);
    } else if (
      envelope.operation === "request.accepted" &&
      typeof requestId === "string"
    ) {
      this.#store.dispatch({ type: "request-accepted", requestId });
    } else if (
      envelope.operation === "request.completed" &&
      typeof requestId === "string"
    ) {
      this.#store.dispatch({ type: "request-completed", requestId });
    } else if (envelope.operation === "kiro.auth_required") {
      const verificationUri =
        envelope.payload.verificationUri ?? envelope.payload.verificationUrl;
      const userCode = envelope.payload.userCode;
      const expiresAt = envelope.payload.expiresAt;
      if (
        typeof verificationUri === "string" &&
        typeof userCode === "string" &&
        typeof expiresAt === "string"
      ) {
        this.#store.dispatch({
          type: "device-flow",
          presentation: {
            verificationUri,
            userCode,
            expiresAt,
            status: "required",
          },
        });
        // Pop the verification page open for the user. Debounced one second
        // so the deep link (which follows the bare start URL immediately)
        // is the page that opens; the banner keeps the visible URL and an
        // explicit open button for blocked pop-ups.
        if (this.#autoOpenedCode !== userCode) {
          if (this.#autoOpenTimer !== undefined) {
            this.#clearTimeout(this.#autoOpenTimer);
          }
          this.#autoOpenTimer = this.#setTimeout(() => {
            this.#autoOpenTimer = undefined;
            const flow = this.#store.snapshot().deviceFlow;
            if (flow === undefined || this.#autoOpenedCode === flow.userCode) {
              return;
            }
            this.#autoOpenedCode = flow.userCode;
            try {
              window.open(flow.verificationUri, "_blank", "noopener");
            } catch {
              // Pop-up blocked: the banner link is the fallback.
            }
          }, 1000);
        }
        this.#scheduleKiroPoll();
      }
    } else if (envelope.operation === "kiro.authenticated") {
      this.#store.dispatch({ type: "device-authenticated" });
    } else if (envelope.operation === "kiro.auth_status") {
      const state = envelope.payload.state;
      if (
        state === "authenticated" ||
        state === "required" ||
        state === "expired" ||
        state === "failed"
      ) {
        this.#store.dispatch({ type: "kiro-status", state });
      }
    } else if (envelope.operation === "checkpoint.committed") {
      const receipt = envelope.payload.checkpointReceipt;
      if (this.#pendingStop && typeof receipt === "string") {
        void this.#completeStop(receipt);
      }
    } else if (envelope.operation === "error") {
      const code =
        typeof envelope.payload.code === "string"
          ? envelope.payload.code
          : "INTERNAL_ERROR";
      const message =
        typeof envelope.payload.message === "string"
          ? envelope.payload.message
          : "The runtime could not continue safely.";
      const disposition = classifyRuntimeError(requestId, code);
      if (disposition === "read-only") {
        this.#store.dispatch({ type: "read-only", message });
      } else if (disposition === "terminal") {
        this.#store.dispatch({
          type: "terminal-error",
          error: {
            code,
            message,
            retryable: envelope.payload.retryable === true,
          },
        });
      }
      // "ignore": a single tunnelled request failed. Its own fetch or
      // EventSource in the upstream page receives that failure and recovers
      // the way it would locally; the sandbox is not in trouble.
    }
  }

  async #completeStop(receipt: string): Promise<void> {
    try {
      const sandbox = await this.#control.stop(receipt);
      this.#store.dispatch({ type: "sandbox", sandbox });
      this.#disconnect();
    } catch (error: unknown) {
      this.#terminal(error);
    } finally {
      this.#pendingStop = false;
    }
  }

  async #refreshStatus(connectActive: boolean): Promise<void> {
    try {
      const sandbox = await this.#control.status();
      this.#store.dispatch({ type: "sandbox", sandbox });
      this.#refreshDetails(sandbox.stateVersion);
      if (
        connectActive &&
        sandbox.state !== "STOPPED" &&
        sandbox.state !== "ERROR"
      ) {
        await this.start();
      }
    } catch (error: unknown) {
      if (
        error instanceof PasswordAuthError &&
        error.code === "SESSION_EXPIRED"
      ) {
        this.#auth.clear();
        this.#store.dispatch({ type: "signed-out" });
      } else {
        this.#terminal(error);
      }
    }
  }

  #detailsVersion: number | undefined;

  #refreshDetails(stateVersion: number): void {
    if (this.#detailsVersion === stateVersion) {
      return;
    }
    this.#detailsVersion = stateVersion;
    void this.#control
      .history()
      .then((details) => {
        if (!this.#destroyed) {
          this.#store.dispatch({ type: "details", value: details });
        }
      })
      .catch(() => {
        // Decorative data: a failed fetch retries on the next transition.
        this.#detailsVersion = undefined;
      });
  }

  #scheduleStatusPoll(): void {
    if (this.#pollTimer !== undefined) {
      this.#clearTimeout(this.#pollTimer);
    }
    this.#pollTimer = this.#setTimeout(() => {
      this.#pollTimer = undefined;
      if (this.#destroyed) {
        return;
      }
      void this.#refreshStatus(false).then(() => {
        const view = this.#store.snapshot().view;
        if (
          view === "starting" ||
          view === "restoring" ||
          view === "reconnecting"
        ) {
          this.#store.tolerateStartOrRestore();
          this.#scheduleStatusPoll();
        }
      });
    }, POLL_INTERVAL_MS);
  }

  #disconnect(): void {
    this.#connectionGeneration += 1;
    if (this.#pollTimer !== undefined) {
      this.#clearTimeout(this.#pollTimer);
      this.#pollTimer = undefined;
    }
    if (this.#pingTimer !== undefined) {
      this.#clearTimeout(this.#pingTimer);
      this.#pingTimer = undefined;
    }
    this.#duplex?.close(1000, "Browser shell disconnected");
    this.#duplex = undefined;
    this.#uninstallBootstrap?.();
    this.#uninstallBootstrap = undefined;
    this.#clientSequence = 1;
  }

  #terminal(error: unknown): void {
    const retryable =
      error instanceof BrowserApplicationError && error.retryable;
    if (retryable) {
      // Recoverable: keep the sandbox in a reconnecting state so self-heal
      // owns it instead of presenting a dead end that also gates the page.
      this.#store.dispatch({ type: "connection-lost" });
      this.#scheduleReconnect();
      return;
    }
    this.#terminalFinal(error);
  }

  #terminalFinal(error: unknown): void {
    const safe =
      error instanceof BrowserApplicationError ||
      error instanceof PasswordAuthError
        ? {
            code: error.code,
            message: error.message,
            retryable:
              error instanceof BrowserApplicationError && error.retryable,
          }
        : {
            code: "INTERNAL_ERROR",
            message: "The browser application could not continue safely.",
            retryable: false,
          };
    this.#store.dispatch({ type: "terminal-error", error: safe });
  }
}
