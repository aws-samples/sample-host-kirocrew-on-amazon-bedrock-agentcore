import { describe, expect, it, vi } from "vitest";

import {
  AgentCoreBrowserChannel,
  AgentCoreConnectionError,
  agentCoreWebSocketProtocols,
  validateAgentCoreRuntimeUrl,
  type RuntimeConnectionDescriptor,
} from "../src/agentcore-channel.js";
import { PROTOCOL_VERSION } from "../src/generated/protocol.js";
import type { AgentCoreInvocation } from "../src/remote-transport.js";

const TOKEN = "header.payload.signature";
const BINDING = "binding-token-that-is-at-least-thirty-two-characters";
const REGION = "us-west-2";

const DESCRIPTOR: RuntimeConnectionDescriptor = {
  sandboxId: "sbx_01J00000000000000000000000",
  runtimeSessionId: "00000000-0000-4000-8000-000000000001",
  state: "STARTING",
  runtimeArn:
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/example",
  qualifier: "DEFAULT",
  httpUrl:
    "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/example/invocations",
  webSocketUrl:
    "wss://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/example/ws",
  bindingToken: BINDING,
  protocolVersion: PROTOCOL_VERSION,
  frontendCompatibilityVersion: "0.2.0",
  expiresAt: "2999-01-01T00:00:00.000Z",
};

const INVOCATION: AgentCoreInvocation = {
  version: PROTOCOL_VERSION,
  requestId: "01J00000000000000000000000",
  bindingToken: BINDING,
  operation: "kirocrew.http",
  payload: {},
};

class FakeWebSocket extends EventTarget {
  public static readonly CONNECTING = 0;
  public static readonly OPEN = 1;
  public static readonly CLOSING = 2;
  public static readonly CLOSED = 3;
  public static last: FakeWebSocket | undefined;
  public readonly url: string;
  public readonly protocols: string | readonly string[] | undefined;
  public readyState = FakeWebSocket.CONNECTING;
  public readonly sent: Array<
    string | ArrayBufferLike | Blob | ArrayBufferView
  > = [];

  public constructor(
    url: string | URL,
    protocols?: string | readonly string[],
  ) {
    super();
    this.url = String(url);
    this.protocols = protocols;
    FakeWebSocket.last = this;
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.dispatchEvent(new Event("open"));
    });
  }

  public send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    this.sent.push(data);
  }

  public closeArgs: readonly [number | undefined, string | undefined] = [
    undefined,
    undefined,
  ];

  public close(code?: number, reason?: string): void {
    this.closeArgs = [code, reason];
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatchEvent(new Event("close"));
  }
}

function latestSocket(): FakeWebSocket {
  const socket = FakeWebSocket.last;
  if (socket === undefined) {
    throw new Error("Expected a WebSocket instance.");
  }
  return socket;
}

function sseResponse(): Response {
  const encoder = new TextEncoder();
  const events = [
    {
      version: PROTOCOL_VERSION,
      messageId: "01J00000000000000000000001",
      requestId: INVOCATION.requestId,
      operation: "request.accepted",
      sequence: 0,
      timestamp: "2026-01-01T00:00:00.000Z",
      correlationId: "01J00000000000000000000002",
      payload: { status: 200 },
    },
    {
      version: PROTOCOL_VERSION,
      messageId: "01J00000000000000000000003",
      requestId: INVOCATION.requestId,
      operation: "output.delta",
      sequence: 1,
      timestamp: "2026-01-01T00:00:01.000Z",
      correlationId: "01J00000000000000000000002",
      payload: { data: "ok" },
    },
    {
      version: PROTOCOL_VERSION,
      messageId: "01J00000000000000000000004",
      requestId: INVOCATION.requestId,
      operation: "request.completed",
      sequence: 2,
      timestamp: "2026-01-01T00:00:02.000Z",
      correlationId: "01J00000000000000000000002",
      payload: {},
    },
  ];
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller): void {
        for (const event of events) {
          controller.enqueue(
            encoder.encode(
              `event: ${event.operation}\ndata: ${JSON.stringify(event)}\n\n`,
            ),
          );
        }
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

describe("AgentCore browser channel", () => {
  it("constructs the exact bearer authorization WebSocket protocols", () => {
    const protocols = agentCoreWebSocketProtocols(TOKEN);

    expect(protocols).toHaveLength(2);
    expect(protocols[0]).toMatch(/^base64UrlBearerAuthorization\.[\w-]+$/u);
    expect(protocols[0]).not.toContain(TOKEN);
    expect(protocols[1]).toBe("base64UrlBearerAuthorization");
  });

  it("rejects alternate origins, schemes, ports, fragments, and traversal", () => {
    const invalid = [
      "ws://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/example/ws",
      "wss://evil.example.test/runtimes/example/ws",
      "wss://bedrock-agentcore.us-west-2.amazonaws.com:443/runtimes/example/ws",
      "wss://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/../admin",
      "wss://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/example/ws#token",
    ];

    for (const value of invalid) {
      expect(() =>
        validateAgentCoreRuntimeUrl(value, REGION, "websocket"),
      ).toThrowError(AgentCoreConnectionError);
    }
  });

  it("sends the binding token in connection.hello as the first WSS message", async () => {
    FakeWebSocket.last = undefined;
    const channel = new AgentCoreBrowserChannel(DESCRIPTOR, {
      region: REGION,
      accessToken: (): Promise<string> => Promise.resolve(TOKEN),
      webSocket: FakeWebSocket as unknown as typeof WebSocket,
      requestId: (): string => "01J00000000000000000000005",
      now: (): Date => new Date("2026-01-01T00:00:00.000Z"),
    });

    await channel.openWebSocket(INVOCATION);

    const socket = latestSocket();
    expect(socket.url).not.toContain(TOKEN);
    expect(socket.protocols).toEqual(agentCoreWebSocketProtocols(TOKEN));
    const frame = socket.sent[0];
    if (typeof frame !== "string") {
      throw new Error("Expected the connection hello text frame.");
    }
    const first = JSON.parse(frame) as {
      operation: string;
      payload: { bindingToken: string };
      sequence: number;
    };
    expect(first).toEqual(
      expect.objectContaining({
        operation: "connection.hello",
        sequence: 0,
        payload: { bindingToken: BINDING },
      }),
    );
  });

  it("uses Authorization only in the native HTTP/SSE fallback request", async () => {
    const fetchMock = vi.fn(
      (
        input: string | URL | Request,
        init?: RequestInit,
      ): Promise<Response> => {
        const url = input instanceof Request ? input.url : input.toString();
        expect(url).not.toContain(TOKEN);
        expect(new Headers(init?.headers).get("authorization")).toBe(
          `Bearer ${TOKEN}`,
        );
        expect(init?.credentials).toBe("omit");
        return Promise.resolve(sseResponse());
      },
    );
    const channel = new AgentCoreBrowserChannel(DESCRIPTOR, {
      region: REGION,
      accessToken: (): Promise<string> => Promise.resolve(TOKEN),
      fetch: fetchMock as typeof fetch,
    });

    const received = [];
    for await (const event of channel.invoke(INVOCATION)) {
      received.push(event.operation);
    }

    expect(received).toEqual([
      "request.accepted",
      "output.delta",
      "request.completed",
    ]);
  });

  it("pins every HTTP invocation to the sandbox runtime session", async () => {
    const fetchMock = vi.fn(
      (
        _input: string | URL | Request,
        init?: RequestInit,
      ): Promise<Response> => {
        expect(
          new Headers(init?.headers).get(
            "x-amzn-bedrock-agentcore-runtime-session-id",
          ),
        ).toBe(DESCRIPTOR.runtimeSessionId);
        return Promise.resolve(sseResponse());
      },
    );
    const channel = new AgentCoreBrowserChannel(DESCRIPTOR, {
      region: REGION,
      accessToken: (): Promise<string> => Promise.resolve(TOKEN),
      fetch: fetchMock as typeof fetch,
    });

    for await (const event of channel.invoke(INVOCATION)) {
      void event;
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("pins the WebSocket connection to the sandbox runtime session", async () => {
    FakeWebSocket.last = undefined;
    const channel = new AgentCoreBrowserChannel(DESCRIPTOR, {
      region: REGION,
      accessToken: (): Promise<string> => Promise.resolve(TOKEN),
      webSocket: FakeWebSocket as unknown as typeof WebSocket,
    });

    await channel.openWebSocket(INVOCATION);

    const url = new URL(latestSocket().url);
    expect(
      url.searchParams.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"),
    ).toBe(DESCRIPTOR.runtimeSessionId);
  });

  it("fails the duplex with the runtime message on a connection-scoped error", async () => {
    FakeWebSocket.last = undefined;
    const envelopes: string[] = [];
    const channel = new AgentCoreBrowserChannel(DESCRIPTOR, {
      region: REGION,
      accessToken: (): Promise<string> => Promise.resolve(TOKEN),
      webSocket: FakeWebSocket as unknown as typeof WebSocket,
      onEnvelope: (envelope): void => {
        envelopes.push(envelope.operation);
      },
    });
    const duplex = await channel.openWebSocket(INVOCATION);
    const socket = latestSocket();
    const closeSpy = vi.spyOn(socket, "close");

    socket.dispatchEvent(
      new MessageEvent("message", {
        data: JSON.stringify({
          version: PROTOCOL_VERSION,
          messageId: "01J00000000000000000000006",
          requestId: null,
          operation: "error",
          sequence: 0,
          timestamp: "2026-01-01T00:00:00.000Z",
          correlationId: "01J00000000000000000000007",
          payload: {
            category: "PERSISTENCE",
            code: "PERSISTENCE_RESTORE_FAILED",
            message:
              "The authoritative sandbox checkpoint could not be restored.",
            retryable: false,
          },
        }),
      }),
    );

    await expect(async () => {
      for await (const event of duplex.events) {
        void event;
      }
    }).rejects.toMatchObject({
      name: "AgentCoreConnectionError",
      message: "The authoritative sandbox checkpoint could not be restored.",
    });
    expect(envelopes).toContain("error");
    expect(closeSpy).toHaveBeenCalledWith(1000, "Runtime connection error");
  });

  it("closes with an application-range code on an invalid runtime message", async () => {
    FakeWebSocket.last = undefined;
    const channel = new AgentCoreBrowserChannel(DESCRIPTOR, {
      region: REGION,
      accessToken: (): Promise<string> => Promise.resolve(TOKEN),
      webSocket: FakeWebSocket as unknown as typeof WebSocket,
    });
    const duplex = await channel.openWebSocket(INVOCATION);
    const socket = latestSocket();
    const closeSpy = vi.spyOn(socket, "close");

    socket.dispatchEvent(
      new MessageEvent("message", { data: "not-a-protocol-envelope" }),
    );

    await expect(async () => {
      for await (const event of duplex.events) {
        void event;
      }
    }).rejects.toMatchObject({ name: "AgentCoreConnectionError" });
    const [code] = closeSpy.mock.calls[0] ?? [];
    expect(
      code === 1000 ||
        (typeof code === "number" && code >= 3000 && code <= 4999),
    ).toBe(true);
  });
});
