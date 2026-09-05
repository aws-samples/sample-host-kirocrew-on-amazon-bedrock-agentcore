export const START_RESTORE_TOLERANCE_MS = 120_000;

export type SandboxState =
  | "STOPPED"
  | "STARTING"
  | "RESTORING"
  | "READY"
  | "BUSY"
  | "CHECKPOINTING"
  | "STOPPING"
  | "ERROR";

export type LifecycleView =
  | "signed-out"
  | "stopped"
  | "starting"
  | "restoring"
  | "ready"
  | "active-response"
  | "stopping"
  | "reconnecting"
  | "read-only"
  | "terminal-error";

export interface SandboxSnapshot {
  readonly sandboxId: string;
  readonly state: SandboxState;
  readonly stateVersion: number;
  readonly lastCheckpointAt: string | null;
  readonly lastRestore: string | null;
  readonly updatedAt: string;
}

export type KiroAuthState =
  | "checking"
  | "authenticated"
  | "required"
  | "expired"
  | "failed";

export interface KiroAuthPresentation {
  readonly state: KiroAuthState;
}

export interface DeviceFlowPresentation {
  readonly verificationUri: string;
  readonly userCode: string;
  readonly expiresAt: string;
  readonly status: "required" | "expired" | "failed";
}

export interface LifecycleError {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
}

export interface SandboxHistoryEvent {
  readonly at: string;
  readonly state: string;
  readonly stateVersion: number;
}

export interface SandboxDetails {
  readonly events: readonly SandboxHistoryEvent[];
  readonly persistedPaths: readonly string[];
}

export interface LifecycleModel {
  readonly view: LifecycleView;
  readonly sandbox?: SandboxSnapshot;
  readonly activeRequestId?: string;
  readonly activeRequestAccepted: boolean;
  readonly checkpointFreshness?: string;
  readonly restoreOutcome?: string;
  readonly deviceFlow?: DeviceFlowPresentation;
  readonly kiroAuth?: KiroAuthPresentation;
  readonly details?: SandboxDetails;
  readonly error?: LifecycleError;
  readonly startedAt?: number;
}

export type LifecycleEvent =
  | { readonly type: "signed-out" }
  | { readonly type: "sandbox"; readonly sandbox: SandboxSnapshot }
  | { readonly type: "start-requested"; readonly at: number }
  | { readonly type: "connection-lost" }
  | { readonly type: "connection-ready" }
  | { readonly type: "read-only"; readonly message: string }
  | { readonly type: "terminal-error"; readonly error: LifecycleError }
  | { readonly type: "request-submitted"; readonly requestId: string }
  | { readonly type: "request-accepted"; readonly requestId: string }
  | { readonly type: "request-completed"; readonly requestId: string }
  | {
      readonly type: "device-flow";
      readonly presentation: DeviceFlowPresentation;
    }
  | { readonly type: "device-authenticated" }
  | { readonly type: "kiro-status"; readonly state: KiroAuthState }
  | { readonly type: "details"; readonly value: SandboxDetails }
  | { readonly type: "stop-requested" };

const VIEW_COPY: Readonly<
  Record<
    LifecycleView,
    {
      readonly label: string;
      readonly detail: string;
      readonly tone: "neutral" | "progress" | "success" | "warning" | "danger";
    }
  >
> = {
  "signed-out": {
    label: "Sign in to KiroCrew",
    detail: "Your sandbox remains saved while you are signed out.",
    tone: "neutral",
  },
  stopped: {
    label: "Sandbox stopped",
    detail: "Your saved workspace is ready to restore.",
    tone: "neutral",
  },
  starting: {
    label: "Starting sandbox",
    detail:
      "Allocating isolated AgentCore compute. This can take up to two minutes.",
    tone: "progress",
  },
  restoring: {
    label: "Restoring workspace",
    detail: "Restoring the latest encrypted checkpoint before KiroCrew starts.",
    tone: "progress",
  },
  ready: {
    label: "Sandbox ready",
    detail: "KiroCrew is connected and ready.",
    tone: "success",
  },
  "active-response": {
    label: "KiroCrew is responding",
    detail: "Streaming the current response. Duplicate submission is disabled.",
    tone: "progress",
  },
  stopping: {
    label: "Saving and stopping",
    detail: "Creating a durable checkpoint before compute is released.",
    tone: "progress",
  },
  reconnecting: {
    label: "Reconnecting",
    detail: "Recovering the connection without resubmitting your request.",
    tone: "warning",
  },
  "read-only": {
    label: "Workspace is read-only",
    detail:
      "Durable storage is temporarily unavailable. Your existing data is protected.",
    tone: "warning",
  },
  "terminal-error": {
    label: "Sandbox needs attention",
    detail: "The sandbox could not continue safely.",
    tone: "danger",
  },
};

/**
 * How a runtime `error` envelope should affect the sandbox lifecycle.
 *
 * Most errors belong to ONE tunnelled request: a proxied `fetch` that got a
 * 5xx, an EventSource the adapter dropped, a denied route. The upstream page
 * already handles those on its own fetch/EventSource, exactly as it would
 * locally, so escalating them to a sandbox-wide "needs attention" card is
 * wrong: the dashboard underneath keeps working while the card says it is
 * broken. Only connection-scoped errors (no requestId) and errors that mean
 * the sandbox itself cannot continue may change the lifecycle view.
 */
export type RuntimeErrorDisposition = "ignore" | "read-only" | "terminal";

const READ_ONLY_CODES: ReadonlySet<string> = new Set([
  "CHECKPOINT_FAILED",
  "PERSISTENCE_RESTORE_FAILED",
]);

const SANDBOX_FATAL_CODES: ReadonlySet<string> = new Set([
  "BINDING_MISMATCH",
  "AUTHENTICATION_FAILED",
  "SANDBOX_CONFLICT",
  "UNSUPPORTED_PROTOCOL",
]);

export function classifyRuntimeError(
  requestId: string | null | undefined,
  code: string,
): RuntimeErrorDisposition {
  if (READ_ONLY_CODES.has(code)) {
    return "read-only";
  }
  const connectionScoped = requestId === undefined || requestId === null;
  if (connectionScoped || SANDBOX_FATAL_CODES.has(code)) {
    return "terminal";
  }
  return "ignore";
}

export function lifecyclePresentation(
  view: LifecycleView,
): (typeof VIEW_COPY)[LifecycleView] {
  return VIEW_COPY[view];
}

function viewForSandbox(state: SandboxState): LifecycleView {
  switch (state) {
    case "STOPPED":
      return "stopped";
    case "STARTING":
      return "starting";
    case "RESTORING":
      return "restoring";
    case "READY":
      return "ready";
    case "BUSY":
      return "active-response";
    case "CHECKPOINTING":
    case "STOPPING":
      return "stopping";
    case "ERROR":
      return "terminal-error";
  }
}

function checkpointFreshness(
  value: string | null,
  now: number,
): string | undefined {
  if (value === null) {
    return undefined;
  }
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    return undefined;
  }
  const seconds = Math.max(0, Math.floor((now - timestamp) / 1000));
  if (seconds < 60) {
    return "Saved less than a minute ago";
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `Saved ${String(minutes)} minute${minutes === 1 ? "" : "s"} ago`;
  }
  const hours = Math.floor(minutes / 60);
  return `Saved ${String(hours)} hour${hours === 1 ? "" : "s"} ago`;
}

function withOptional<T extends object, K extends string, V>(
  value: T,
  key: K,
  optional: V | undefined,
): T & Partial<Record<K, V>> {
  return optional === undefined ? value : { ...value, [key]: optional };
}

function withoutProperty<T extends object, K extends keyof T>(
  value: T,
  key: K,
): Omit<T, K> {
  const result = { ...value };
  delete result[key];
  return result;
}

export function reduceLifecycle(
  model: LifecycleModel,
  event: LifecycleEvent,
  now = Date.now(),
): LifecycleModel {
  switch (event.type) {
    case "signed-out":
      return { view: "signed-out", activeRequestAccepted: false };
    case "sandbox": {
      let next: LifecycleModel = {
        ...model,
        view: viewForSandbox(event.sandbox.state),
        sandbox: event.sandbox,
        activeRequestAccepted:
          event.sandbox.state === "BUSY" && model.activeRequestAccepted,
      };
      next = withOptional(
        next,
        "checkpointFreshness",
        checkpointFreshness(event.sandbox.lastCheckpointAt, now),
      );
      next = withOptional(
        next,
        "restoreOutcome",
        event.sandbox.lastRestore ?? undefined,
      );
      if (event.sandbox.state !== "ERROR") {
        return withoutProperty(next, "error");
      }
      return next;
    }
    case "start-requested":
      return {
        ...model,
        view: "starting",
        activeRequestAccepted: false,
        startedAt: event.at,
      };
    case "connection-lost":
      return { ...model, view: "reconnecting" };
    case "connection-ready": {
      // A ready connection supersedes any transient startup error.
      const ready: LifecycleModel = {
        ...model,
        view: model.activeRequestId === undefined ? "ready" : "active-response",
      };
      return withoutProperty(ready, "error");
    }
    case "read-only":
      return {
        ...model,
        view: "read-only",
        error: {
          code: "PERSISTENCE_READ_ONLY",
          message: event.message,
          retryable: true,
        },
      };
    case "terminal-error":
      return { ...model, view: "terminal-error", error: event.error };
    case "request-submitted":
      if (model.activeRequestId !== undefined) {
        return model;
      }
      return {
        ...model,
        view: "active-response",
        activeRequestId: event.requestId,
        activeRequestAccepted: false,
      };
    case "request-accepted":
      if (event.requestId !== model.activeRequestId) {
        return model;
      }
      return { ...model, activeRequestAccepted: true };
    case "request-completed": {
      if (event.requestId !== model.activeRequestId) {
        return model;
      }
      const completed = withoutProperty(
        withoutProperty(model, "activeRequestId"),
        "error",
      );
      return {
        ...completed,
        view: "ready",
        activeRequestAccepted: false,
      };
    }
    case "device-flow":
      return {
        ...model,
        deviceFlow: event.presentation,
        kiroAuth: { state: "required" },
      };
    case "device-authenticated":
      return {
        ...withoutProperty(model, "deviceFlow"),
        kiroAuth: { state: "authenticated" },
      };
    case "kiro-status":
      if (event.state === "authenticated" || event.state === "checking") {
        return {
          ...withoutProperty(model, "deviceFlow"),
          kiroAuth: { state: event.state },
        };
      }
      return { ...model, kiroAuth: { state: event.state } };
    case "details":
      return { ...model, details: event.value };
    case "stop-requested":
      return { ...model, view: "stopping" };
  }
}

export class LifecycleStore {
  #model: LifecycleModel;
  readonly #listeners = new Set<(model: LifecycleModel) => void>();
  readonly #now: () => number;

  public constructor(authenticated: boolean, now: () => number = Date.now) {
    this.#now = now;
    this.#model = {
      view: authenticated ? "reconnecting" : "signed-out",
      activeRequestAccepted: false,
    };
  }

  public snapshot(): LifecycleModel {
    return this.#model;
  }

  public subscribe(listener: (model: LifecycleModel) => void): () => void {
    this.#listeners.add(listener);
    listener(this.#model);
    return (): void => {
      this.#listeners.delete(listener);
    };
  }

  public dispatch(event: LifecycleEvent): LifecycleModel {
    this.#model = reduceLifecycle(this.#model, event, this.#now());
    for (const listener of this.#listeners) {
      listener(this.#model);
    }
    return this.#model;
  }

  public canSubmit(): boolean {
    return (
      this.#model.view === "ready" && this.#model.activeRequestId === undefined
    );
  }

  public beginSubmission(requestId: string): boolean {
    if (!this.canSubmit()) {
      return false;
    }
    this.dispatch({ type: "request-submitted", requestId });
    return true;
  }

  public canRetrySubmission(requestId: string): boolean {
    return (
      this.#model.activeRequestId === requestId &&
      !this.#model.activeRequestAccepted
    );
  }

  public tolerateStartOrRestore(): boolean {
    const startedAt = this.#model.startedAt;
    if (
      startedAt === undefined ||
      (this.#model.view !== "starting" && this.#model.view !== "restoring")
    ) {
      return false;
    }
    if (this.#now() - startedAt <= START_RESTORE_TOLERANCE_MS) {
      return true;
    }
    this.dispatch({ type: "connection-lost" });
    return false;
  }
}
