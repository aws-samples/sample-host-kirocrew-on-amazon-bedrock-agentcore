import { describe, expect, it, vi } from "vitest";

import {
  LifecycleStore,
  START_RESTORE_TOLERANCE_MS,
  classifyRuntimeError,
  reduceLifecycle,
  type LifecycleModel,
  type SandboxSnapshot,
  type SandboxState,
} from "../src/lifecycle.js";

function sandbox(
  state: SandboxState,
  overrides: Partial<SandboxSnapshot> = {},
): SandboxSnapshot {
  return {
    sandboxId: "sbx_01J00000000000000000000000",
    state,
    stateVersion: 1,
    lastCheckpointAt: null,
    lastRestore: null,
    updatedAt: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

const INITIAL: LifecycleModel = {
  view: "reconnecting",
  activeRequestAccepted: false,
};

describe("browser lifecycle", () => {
  it.each([
    ["STOPPED", "stopped"],
    ["STARTING", "starting"],
    ["RESTORING", "restoring"],
    ["READY", "ready"],
    ["BUSY", "active-response"],
    ["CHECKPOINTING", "stopping"],
    ["STOPPING", "stopping"],
    ["ERROR", "terminal-error"],
  ] as const)("maps authoritative %s to %s", (state, view) => {
    expect(
      reduceLifecycle(INITIAL, { type: "sandbox", sandbox: sandbox(state) })
        .view,
    ).toBe(view);
  });

  it("provides distinct signed-out, reconnecting, and read-only views", () => {
    expect(reduceLifecycle(INITIAL, { type: "signed-out" }).view).toBe(
      "signed-out",
    );
    expect(reduceLifecycle(INITIAL, { type: "connection-lost" }).view).toBe(
      "reconnecting",
    );
    const readOnly = reduceLifecycle(INITIAL, {
      type: "read-only",
      message: "Saving is paused until durable storage recovers.",
    });
    expect(readOnly.view).toBe("read-only");
    expect(readOnly.error).toEqual(
      expect.objectContaining({ retryable: true }),
    );
  });

  it("shows checkpoint freshness and the last restore outcome", () => {
    const model = reduceLifecycle(
      INITIAL,
      {
        type: "sandbox",
        sandbox: sandbox("READY", {
          lastCheckpointAt: "2026-01-01T00:00:00.000Z",
          lastRestore: "restored from generation 42",
        }),
      },
      Date.parse("2026-01-01T00:05:30.000Z"),
    );

    expect(model.checkpointFreshness).toBe("Saved 5 minutes ago");
    expect(model.restoreOutcome).toBe("restored from generation 42");
  });

  it("prevents duplicate submission until the active request completes", () => {
    const store = new LifecycleStore(true);
    store.dispatch({ type: "sandbox", sandbox: sandbox("READY") });

    expect(store.beginSubmission("request-a")).toBe(true);
    expect(store.beginSubmission("request-b")).toBe(false);
    store.dispatch({ type: "request-accepted", requestId: "request-a" });
    expect(store.snapshot().activeRequestAccepted).toBe(true);
    store.dispatch({ type: "request-completed", requestId: "request-a" });
    expect(store.beginSubmission("request-b")).toBe(true);
  });

  it("allows a safe retry only before adapter acceptance", () => {
    const store = new LifecycleStore(true);
    store.dispatch({ type: "sandbox", sandbox: sandbox("READY") });
    store.beginSubmission("request-a");

    expect(store.canRetrySubmission("request-a")).toBe(true);
    store.dispatch({ type: "request-accepted", requestId: "request-a" });
    expect(store.canRetrySubmission("request-a")).toBe(false);
  });

  it("clears a transient startup error once the connection is ready", () => {
    const store = new LifecycleStore(true);
    store.dispatch({
      type: "terminal-error",
      error: {
        code: "TRANSPORT_FAILED",
        message: "The browser application could not continue safely.",
        retryable: true,
      },
    });
    store.dispatch({ type: "connection-ready" });

    expect(store.snapshot().view).toBe("ready");
    expect(store.snapshot().error).toBeUndefined();
  });

  it("reconnects without completing or replaying an accepted request", () => {
    const store = new LifecycleStore(true);
    const listener = vi.fn();
    store.subscribe(listener);
    store.dispatch({ type: "sandbox", sandbox: sandbox("READY") });
    store.beginSubmission("request-a");
    store.dispatch({ type: "request-accepted", requestId: "request-a" });

    store.dispatch({ type: "connection-lost" });
    store.dispatch({ type: "connection-ready" });

    expect(store.snapshot()).toEqual(
      expect.objectContaining({
        view: "active-response",
        activeRequestId: "request-a",
        activeRequestAccepted: true,
      }),
    );
    expect(
      listener.mock.calls.some(
        (call) =>
          (call[0] as LifecycleModel | undefined)?.activeRequestId ===
          "request-a",
      ),
    ).toBe(true);
  });

  it("does not abandon start or restore before the full 120-second tolerance", () => {
    let now = 1_000;
    const store = new LifecycleStore(true, (): number => now);
    store.dispatch({ type: "start-requested", at: now });

    now += START_RESTORE_TOLERANCE_MS;
    expect(store.tolerateStartOrRestore()).toBe(true);
    expect(store.snapshot().view).toBe("starting");

    now += 1;
    expect(store.tolerateStartOrRestore()).toBe(false);
    expect(store.snapshot().view).toBe("reconnecting");
  });

  it("presents Kiro Device Flow without storing a reusable credential", () => {
    const store = new LifecycleStore(true);
    store.dispatch({
      type: "device-flow",
      presentation: {
        verificationUri: "https://device.example.test/verify",
        userCode: "ABCD-EFGH",
        expiresAt: "2026-01-01T00:10:00.000Z",
        status: "required",
      },
    });

    expect(store.snapshot().deviceFlow).toEqual({
      verificationUri: "https://device.example.test/verify",
      userCode: "ABCD-EFGH",
      expiresAt: "2026-01-01T00:10:00.000Z",
      status: "required",
    });
    expect(JSON.stringify(store.snapshot())).not.toMatch(
      /access_token|refresh_token|bearer/iu,
    );

    store.dispatch({ type: "device-authenticated" });
    expect(store.snapshot().deviceFlow).toBeUndefined();
  });
});

describe("kiro auth state", () => {
  it("tracks status reports and clears the device flow once authenticated", () => {
    const store = new LifecycleStore(true, () => 0);
    store.dispatch({
      type: "device-flow",
      presentation: {
        verificationUri: "https://device.example/verify",
        userCode: "ABCD-EFGH",
        expiresAt: "2026-01-01T00:10:00.000Z",
        status: "required",
      },
    });
    expect(store.snapshot().kiroAuth).toEqual({ state: "required" });
    store.dispatch({ type: "kiro-status", state: "authenticated" });
    expect(store.snapshot().kiroAuth).toEqual({ state: "authenticated" });
    expect(store.snapshot().deviceFlow).toBeUndefined();

    store.dispatch({ type: "kiro-status", state: "failed" });
    expect(store.snapshot().kiroAuth).toEqual({ state: "failed" });

    store.dispatch({ type: "kiro-status", state: "checking" });
    expect(store.snapshot().kiroAuth).toEqual({ state: "checking" });

    store.dispatch({ type: "device-authenticated" });
    expect(store.snapshot().kiroAuth).toEqual({ state: "authenticated" });
  });
});

describe("classifyRuntimeError", () => {
  it("leaves the sandbox alone when one tunnelled request fails", () => {
    // A proxied fetch or EventSource that the adapter gave up on is the
    // upstream page's problem to retry, exactly as it would be locally.
    expect(classifyRuntimeError("01REQ", "KIROCREW_UNAVAILABLE")).toBe(
      "ignore",
    );
    expect(classifyRuntimeError("01REQ", "AUTHORIZATION_FAILED")).toBe(
      "ignore",
    );
    expect(
      classifyRuntimeError("01REQ", "FEATURE_UNAVAILABLE_IN_AGENTCORE"),
    ).toBe("ignore");
    expect(classifyRuntimeError("01REQ", "THROTTLED")).toBe("ignore");
    expect(classifyRuntimeError("01REQ", "INTERNAL_ERROR")).toBe("ignore");
  });

  it("treats connection-scoped errors as sandbox-level", () => {
    expect(classifyRuntimeError(null, "KIROCREW_UNAVAILABLE")).toBe("terminal");
    expect(classifyRuntimeError(undefined, "INTERNAL_ERROR")).toBe("terminal");
  });

  it("escalates codes that mean the sandbox itself cannot continue", () => {
    for (const code of [
      "BINDING_MISMATCH",
      "AUTHENTICATION_FAILED",
      "SANDBOX_CONFLICT",
      "UNSUPPORTED_PROTOCOL",
    ]) {
      expect(classifyRuntimeError("01REQ", code)).toBe("terminal");
    }
  });

  it("maps durability failures to read-only regardless of scope", () => {
    expect(classifyRuntimeError("01REQ", "CHECKPOINT_FAILED")).toBe(
      "read-only",
    );
    expect(classifyRuntimeError(null, "PERSISTENCE_RESTORE_FAILED")).toBe(
      "read-only",
    );
  });
});

it("records sandbox details for the panel", () => {
  const store = new LifecycleStore(true);
  store.dispatch({
    type: "details",
    value: {
      events: [{ at: "2026-09-05T10:00:00Z", state: "READY", stateVersion: 3 }],
      persistedPaths: ["/mnt/workspace/projects"],
    },
  });
  const model = store.snapshot();
  expect(model.details?.events[0]?.state).toBe("READY");
  expect(model.details?.persistedPaths).toEqual(["/mnt/workspace/projects"]);
});
