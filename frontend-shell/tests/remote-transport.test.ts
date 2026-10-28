import { describe, expect, it, vi } from "vitest";

import type {
  AgentCoreChannel,
  AgentCoreDuplex,
  AgentCoreInvocation,
  AgentCoreRuntimeEvent,
} from "../src/remote-transport.js";
import {
  KiroCrewRemoteEventSource,
  KiroCrewRemoteTransport,
  KiroCrewRemoteWebSocket,
  RemoteTransportError,
  classifyKiroCrewRoute,
  chunkFrames,
  gateChannel,
  kiroPrerequisiteSnapshot,
} from "../src/remote-transport.js";

const ORIGIN = "https://shell.example";
const REQUEST_ID = "01J00000000000000000000001";

function event(
  invocation: AgentCoreInvocation,
  sequence: number,
  operation: AgentCoreRuntimeEvent["operation"],
  payload: Readonly<Record<string, unknown>>,
): AgentCoreRuntimeEvent {
  return { requestId: invocation.requestId, sequence, operation, payload };
}

class FakeChannel implements AgentCoreChannel {
  public readonly invocations: AgentCoreInvocation[] = [];
  public events: (
    invocation: AgentCoreInvocation,
  ) => AsyncIterable<AgentCoreRuntimeEvent> =
    async function* (): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      for (const event of [] as AgentCoreRuntimeEvent[]) {
        yield event;
      }
    };
  public duplex: AgentCoreDuplex | undefined;

  public async *invoke(
    invocation: AgentCoreInvocation,
  ): AsyncIterable<AgentCoreRuntimeEvent> {
    this.invocations.push(invocation);
    yield* this.events(invocation);
  }

  public openWebSocket(
    invocation: AgentCoreInvocation,
  ): Promise<AgentCoreDuplex> {
    this.invocations.push(invocation);
    if (this.duplex === undefined) {
      return Promise.reject(new Error("No duplex configured."));
    }
    return Promise.resolve(this.duplex);
  }
}

function transport(
  channel: FakeChannel,
  limits: { request?: number; response?: number } = {},
): KiroCrewRemoteTransport {
  return new KiroCrewRemoteTransport(channel, {
    origin: ORIGIN,
    bindingToken: (): string =>
      "binding-token-that-is-long-enough-for-the-contract",
    requestId: (): string => REQUEST_ID,
    ...(limits.request === undefined
      ? {}
      : { maxRequestBodyBytes: limits.request }),
    ...(limits.response === undefined
      ? {}
      : { maxResponseBodyBytes: limits.response }),
  });
}

async function expectRemoteError(
  action: Promise<unknown>,
  code: RemoteTransportError["code"],
): Promise<void> {
  await expect(action).rejects.toMatchObject({ code });
}

describe("KiroCrew route policy", () => {
  it("keeps control APIs native and closes unsafe upstream routes", (): void => {
    const channel = new FakeChannel();
    const remote = transport(channel);
    expect(remote.handles("/control/v1/sandbox")).toBe(false);
    expect(remote.handles("/api/chat")).toBe(true);
    expect(classifyKiroCrewRoute("POST", "/api/chat", ORIGIN)).toBe("allowed");
    expect(classifyKiroCrewRoute("GET", "/api/auth/status", ORIGIN)).toBe(
      "synthetic",
    );
    expect(classifyKiroCrewRoute("GET", "/api/kiro-prerequisite", ORIGIN)).toBe(
      "synthetic",
    );
    expect(
      classifyKiroCrewRoute("GET", "/api/onboarding/import/scan", ORIGIN),
    ).toBe("synthetic");
    expect(
      classifyKiroCrewRoute("PUT", "/api/onboarding/import/state", ORIGIN),
    ).toBe("synthetic");
    expect(
      classifyKiroCrewRoute("POST", "/api/onboarding/import/apply", ORIGIN),
    ).toBe("allowed");
    expect(
      classifyKiroCrewRoute("POST", "/api/kiro-prerequisite", ORIGIN),
    ).toBe("allowed");
    expect(classifyKiroCrewRoute("GET", "/api/ws/terminal/1", ORIGIN)).toBe(
      "allowed",
    );
    expect(
      classifyKiroCrewRoute("POST", "/api/terminal/sessions", ORIGIN),
    ).toBe("allowed");
    // Regression guard for the narrow allowlist that 403'd most of the product.
    for (const [method, path] of [
      ["GET", "/api/crons"],
      ["POST", "/api/crons"],
      ["GET", "/api/crons/history"],
      ["GET", "/api/cron-folders"],
      ["GET", "/api/voice/config"],
      ["GET", "/api/voice/voices"],
      ["GET", "/api/auth/me"],
      ["GET", "/api/computer-use/config"],
      ["GET", "/api/tunnel/status"],
      ["GET", "/api/ws/stt"],
      ["DELETE", "/api/terminal/sessions"],
      ["GET", "/api/system/session-storage"],
      ["POST", "/api/file-raw"],
      ["DELETE", "/api/chat"],
      ["POST", "/api/new-upstream-route"],
    ] as const) {
      expect(classifyKiroCrewRoute(method, path, ORIGIN)).toBe("allowed");
    }
    expect(classifyKiroCrewRoute("GET", "/api/desktop", ORIGIN)).toBe(
      "unavailable",
    );
    expect(classifyKiroCrewRoute("GET", "/api/files/pick", ORIGIN)).toBe(
      "unavailable",
    );
    for (const path of [
      "/api/token",
      "/api/token/local",
      "/api/auth/token",
      "/api/secrets",
      "/api/config/export",
      "/api/shutdown",
      "/api/debug/processes",
      "/api/kas-bundle/install",
    ]) {
      expect(classifyKiroCrewRoute("GET", path, ORIGIN)).toBe("denied");
    }
    expect(classifyKiroCrewRoute("GET", "/api/kas-bundle", ORIGIN)).toBe(
      "allowed",
    );
    expect(
      classifyKiroCrewRoute("GET", "https://evil.example/api/chat", ORIGIN),
    ).toBe("denied");
  });
});

describe("KiroCrewRemoteTransport fetch", () => {
  it("serializes an approved request and reconstructs ordered output", async (): Promise<void> => {
    const channel = new FakeChannel();
    channel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      yield event(invocation, 0, "request.accepted", {
        status: 201,
        headers: {
          "content-type": "application/json",
          "x-upstream-secret": "must-not-pass",
        },
      });
      yield event(invocation, 1, "output.delta", {
        encoding: "base64",
        data: btoa('{"ok":'),
      });
      yield event(invocation, 2, "output.delta", {
        encoding: "base64",
        data: btoa("true}"),
      });
      yield event(invocation, 3, "request.completed", { status: 201 });
    };
    const response = await transport(channel).fetch("/api/chat?slot=a", {
      method: "POST",
      headers: {
        authorization: "Bearer local-kirocrew-secret",
        "content-type": "application/json",
        "x-browser-secret": "must-not-pass",
      },
      body: '{"text":"hello"}',
    });

    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({ ok: true });
    expect(response.headers.get("x-upstream-secret")).toBeNull();
    expect(channel.invocations).toHaveLength(1);
    expect(channel.invocations[0]).toMatchObject({
      operation: "kirocrew.http",
      payload: {
        method: "POST",
        path: "/api/chat?slot=a",
        transport: "http",
        headers: { "content-type": "application/json" },
      },
    });
    expect(JSON.stringify(channel.invocations[0])).not.toContain(
      "local-kirocrew-secret",
    );
    expect(JSON.stringify(channel.invocations[0])).not.toContain(
      "x-browser-secret",
    );
  });

  it("satisfies browser auth calls without invoking or exposing a token", async (): Promise<void> => {
    const channel = new FakeChannel();
    const remote = transport(channel);
    for (const path of ["/api/auth/status", "/api/auth/local-token"]) {
      const response = await remote.fetch(`${ORIGIN}${path}`);
      expect(await response.json()).toEqual({
        authenticated: true,
        mode: "agentcore",
        remoteSession: true,
        token: null,
      });
    }
    expect(channel.invocations).toEqual([]);
  });

  it("synthesizes the upstream prerequisite probe without invoking", async (): Promise<void> => {
    const channel = new FakeChannel();
    const remote = transport(channel);
    const scan = await remote.fetch(`${ORIGIN}/api/onboarding/import/scan`);
    expect(await scan.json()).toEqual({ sources: [], skipped: [] });
    const state = await remote.fetch(`${ORIGIN}/api/onboarding/import/state`, {
      method: "PUT",
      body: JSON.stringify({ completed: true }),
    });
    expect(await state.json()).toEqual({ completed: true });
    const response = await remote.fetch(`${ORIGIN}/api/kiro-prerequisite`);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("application/json");
    const body = (await response.json()) as Record<string, unknown>;
    expect(body).toEqual(kiroPrerequisiteSnapshot());
    expect(body).toEqual({
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
    });
    expect(channel.invocations).toEqual([]);
  });

  it("returns stable unavailable and denied responses without tunneling", async (): Promise<void> => {
    const channel = new FakeChannel();
    const remote = transport(channel);
    const unavailable = await remote.fetch(`${ORIGIN}/api/desktop/config`);
    const denied = await remote.fetch(`${ORIGIN}/api/shutdown`, {
      method: "POST",
    });
    expect(unavailable.status).toBe(501);
    expect(await unavailable.json()).toMatchObject({
      code: "FEATURE_UNAVAILABLE_IN_AGENTCORE",
    });
    expect(denied.status).toBe(403);
    expect(await denied.json()).toMatchObject({ code: "AUTHORIZATION_FAILED" });
    expect(channel.invocations).toEqual([]);
  });

  it("rejects oversized, incomplete, errored, and out-of-order responses", async (): Promise<void> => {
    const oversizedRequest = transport(new FakeChannel(), { request: 1 });
    await expectRemoteError(
      oversizedRequest.fetch(`${ORIGIN}/api/chat`, {
        method: "POST",
        body: "too large",
      }),
      "FRAME_TOO_LARGE",
    );

    const oversizedChannel = new FakeChannel();
    oversizedChannel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      yield event(invocation, 0, "request.accepted", { status: 200 });
      yield event(invocation, 1, "output.delta", {
        encoding: "base64",
        data: btoa("large"),
      });
    };
    await expectRemoteError(
      transport(oversizedChannel, { response: 2 }).fetch(
        `${ORIGIN}/api/file-raw?path=a`,
      ),
      "FRAME_TOO_LARGE",
    );

    const incompleteChannel = new FakeChannel();
    incompleteChannel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      yield event(invocation, 0, "request.accepted", { status: 200 });
    };
    await expectRemoteError(
      transport(incompleteChannel).fetch(`${ORIGIN}/api/status`),
      "KIROCREW_UNAVAILABLE",
    );

    const errorChannel = new FakeChannel();
    errorChannel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      yield event(invocation, 0, "error", {
        code: "KIROCREW_UNAVAILABLE",
        message: "Upstream unavailable.",
        retryable: true,
        status: 503,
      });
    };
    await expectRemoteError(
      transport(errorChannel).fetch(`${ORIGIN}/api/status`),
      "KIROCREW_UNAVAILABLE",
    );

    const unorderedChannel = new FakeChannel();
    unorderedChannel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      yield event(invocation, 1, "request.accepted", { status: 200 });
    };
    await expectRemoteError(
      transport(unorderedChannel).fetch(`${ORIGIN}/api/status`),
      "SEQUENCE_ERROR",
    );
  });
});

describe("stream compatibility", () => {
  it("presents ordered SSE fields through an EventSource-compatible object", async (): Promise<void> => {
    const channel = new FakeChannel();
    channel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      yield event(invocation, 0, "output.delta", {
        encoding: "utf8",
        data: 'event: update\nid: 7\ndata: {"ready":true}\n\n',
      });
      yield event(invocation, 1, "request.completed", { status: 200 });
    };
    const source = transport(channel).eventSource(`${ORIGIN}/api/events`);
    const received = await new Promise<MessageEvent<string>>(
      (resolve, reject) => {
        source.addEventListener("update", (value) => {
          resolve(value as MessageEvent<string>);
        });
        source.onerror = (): void => reject(new Error("Unexpected SSE error."));
      },
    );
    expect(received.data).toBe('{"ready":true}');
    expect(received.lastEventId).toBe("7");
    await vi.waitFor(() => {
      expect(source.readyState).toBe(KiroCrewRemoteEventSource.CLOSED);
    });
  });

  it("opens only after the upstream handshake and relays open-time frames live", async (): Promise<void> => {
    const channel = new FakeChannel();
    const states: number[] = [];
    channel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      if (invocation.operation === "kirocrew.ws.send") {
        yield event(invocation, 0, "request.completed", { delivered: true });
        return;
      }
      yield event(invocation, 0, "request.accepted", {
        status: 101,
        transport: "websocket",
      });
      yield event(invocation, 1, "output.delta", {
        encoding: "utf8",
        data: "reply",
        transport: "websocket",
      });
      yield event(invocation, 2, "request.completed", { status: 101 });
    };
    const socket = transport(channel).webSocket(`wss://shell.example/api/ws`);
    // Native semantics: CONNECTING until the handshake, sends refused before.
    expect(socket.readyState).toBe(KiroCrewRemoteWebSocket.CONNECTING);
    expect(() => socket.send("too early")).toThrow(DOMException);
    const message = await new Promise<MessageEvent>((resolve, reject) => {
      socket.onopen = (): void => {
        states.push(socket.readyState);
        socket.send('{"type":"subscribe_logs"}');
      };
      socket.onmessage = resolve;
      socket.onerror = (): void =>
        reject(new Error("Unexpected WebSocket error."));
    });
    expect(states).toEqual([KiroCrewRemoteWebSocket.OPEN]);
    expect(message.data).toBe("reply");
    await vi.waitFor(() => {
      expect(channel.invocations).toHaveLength(2);
    });
    expect(channel.invocations[0]).toMatchObject({
      operation: "kirocrew.http",
      payload: {
        method: "GET",
        path: "/api/ws",
        transport: "websocket",
        frames: [],
      },
    });
    expect(channel.invocations[1]).toMatchObject({
      operation: "kirocrew.ws.send",
      payload: {
        tunnelId: channel.invocations[0]?.requestId,
        data: '{"type":"subscribe_logs"}',
        encoding: "utf8",
      },
    });
    await vi.waitFor(() => {
      expect(socket.readyState).toBe(KiroCrewRemoteWebSocket.CLOSED);
    });
    socket.close();
  });

  it("delivers frames in send order even when an earlier send is slow", async (): Promise<void> => {
    const channel = new FakeChannel();
    const started: string[] = [];
    let releaseFirst: () => void = () => undefined;
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    channel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      if (invocation.operation === "kirocrew.ws.send") {
        const payload = invocation.payload as {
          data?: string;
          frames?: { data: string }[];
        };
        const data =
          payload.frames === undefined
            ? (payload.data ?? "")
            : payload.frames.map((frame) => frame.data).join("+");
        started.push(data);
        if (data === "a") {
          await firstGate;
        }
        yield event(invocation, 0, "request.completed", { delivered: true });
        return;
      }
      yield event(invocation, 0, "request.accepted", {
        status: 101,
        transport: "websocket",
      });
      await new Promise<never>(() => undefined);
    };
    const socket = transport(channel).webSocket(`wss://shell.example/api/ws`);
    await new Promise<void>((resolve) => {
      socket.onopen = (): void => resolve();
    });
    socket.send("a");
    socket.send("b");
    socket.send("c");
    // While "a" is still in flight, nothing behind it may leave.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(started).toEqual(["a"]);
    releaseFirst();
    // Frames queued behind the in-flight send leave as ONE ordered batch.
    await vi.waitFor(() => {
      expect(started).toEqual(["a", "b+c"]);
    });
    socket.send("d");
    await vi.waitFor(() => {
      expect(started).toEqual(["a", "b+c", "d"]);
    });
    socket.close();
  });

  it("splits a large backlog into batches under the adapter cap", (): void => {
    const big = "x".repeat(400 * 1024);
    expect(chunkFrames(["a", "b"])).toEqual([["a", "b"]]);
    expect(chunkFrames([big, big, "c"])).toEqual([[big], [big, "c"]]);
    const bytes = new Uint8Array(300 * 1024);
    expect(chunkFrames([bytes, bytes]).map((chunk) => chunk.length)).toEqual([
      1, 1,
    ]);
    expect(chunkFrames([])).toEqual([]);
  });

  it("closes without ever opening when the tunnel fails before the handshake", async (): Promise<void> => {
    const channel = new FakeChannel();
    channel.events = async function* (
      invocation,
    ): AsyncIterable<AgentCoreRuntimeEvent> {
      await Promise.resolve();
      yield event(invocation, 0, "error", {
        code: "KIROCREW_UNAVAILABLE",
        category: "KIROCREW",
        message: "The runtime backend is temporarily unavailable.",
        correlationId: "corr",
        retryable: true,
      });
    };
    const socket = transport(channel).webSocket(`wss://shell.example/api/ws`);
    let opened = 0;
    let errored = 0;
    socket.onopen = (): void => {
      opened += 1;
    };
    socket.onerror = (): void => {
      errored += 1;
    };
    const closed = await new Promise<CloseEvent>((resolve) => {
      socket.onclose = resolve;
    });
    // The SPA keys its reconnect backoff off `close`; a phantom `open`
    // would reset it on every failed attempt.
    expect(opened).toBe(0);
    expect(errored).toBe(1);
    expect(closed.code).toBe(1006);
    expect(closed.wasClean).toBe(false);
    expect(socket.readyState).toBe(KiroCrewRemoteWebSocket.CLOSED);
  });

  it("fails closed when a WebSocket channel is unavailable", async (): Promise<void> => {
    const channel: AgentCoreChannel = {
      invoke: async function* (): AsyncIterable<AgentCoreRuntimeEvent> {
        await Promise.resolve();
        for (const event of [] as AgentCoreRuntimeEvent[]) {
          yield event;
        }
      },
    };
    const remote = new KiroCrewRemoteTransport(channel, {
      origin: ORIGIN,
      bindingToken: (): string =>
        "binding-token-that-is-long-enough-for-the-contract",
      requestId: (): string => REQUEST_ID,
    });
    await expectRemoteError(
      remote.openWebSocket(`wss://shell.example/api/ws`),
      "FEATURE_UNAVAILABLE_IN_AGENTCORE",
    );
    expect(() =>
      remote.webSocket(`wss://shell.example/api/ws`, "local-token"),
    ).toThrow(RemoteTransportError);
  });
});

describe("gateChannel", () => {
  function sample(): AgentCoreInvocation {
    return {
      version: 1,
      requestId: REQUEST_ID,
      bindingToken: "binding",
      operation: "kirocrew.http",
      payload: { method: "GET", path: "/api/status" },
    } as unknown as AgentCoreInvocation;
  }

  it("holds invocations until the duplex is ready, then forwards them", async (): Promise<void> => {
    const channel = new FakeChannel();
    let open: () => void = () => undefined;
    const ready = new Promise<void>((resolve) => {
      open = resolve;
    });
    const gated = gateChannel(channel, ready);
    const drained = (async (): Promise<void> => {
      for await (const value of gated.invoke(sample())) {
        void value;
      }
    })();
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(channel.invocations).toHaveLength(0);
    open();
    await drained;
    expect(channel.invocations).toHaveLength(1);
  });

  it("fails held invocations when the duplex never opens", async (): Promise<void> => {
    const channel = new FakeChannel();
    const failure = new Error("handshake failed");
    const gated = gateChannel(channel, Promise.reject(failure));
    await expect(
      (async (): Promise<void> => {
        for await (const value of gated.invoke(sample())) {
          void value;
        }
      })(),
    ).rejects.toBe(failure);
    await expect(gated.openWebSocket?.(sample())).rejects.toBe(failure);
    expect(channel.invocations).toHaveLength(0);
  });

  it("omits openWebSocket when the wrapped channel has none", (): void => {
    const channel: AgentCoreChannel = {
      invoke: (invocation) => new FakeChannel().invoke(invocation),
    };
    expect("openWebSocket" in gateChannel(channel, Promise.resolve())).toBe(
      false,
    );
  });
});
