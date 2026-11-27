import { describe, expect, it, vi } from "vitest";

import {
  HeldWebSocket,
  KIROCREW_AGENTCORE_CAPABILITIES,
  installKiroCrewBootstrap,
  installPreSessionGate,
  normalizeBrowserApplicationConfig,
  unavailablePresentation,
  type BootstrapTarget,
} from "../src/bootstrap.js";
import {
  KiroCrewRemoteEventSource,
  KiroCrewRemoteTransport,
  KiroCrewRemoteWebSocket,
  RemoteTransportError,
  type AgentCoreChannel,
  type AgentCoreRuntimeEvent,
} from "../src/remote-transport.js";

const ORIGIN = "https://shell.example";
const REQUEST_ID = "01J00000000000000000000001";

class NativeEventSource {
  public static readonly CONNECTING = 0;
  public static readonly OPEN = 1;
  public static readonly CLOSED = 2;
  public readonly url: string;

  public constructor(url: string | URL) {
    this.url = String(url);
  }

  public close(): void {}
}

class NativeWebSocket {
  public static readonly CONNECTING = 0;
  public static readonly OPEN = 1;
  public static readonly CLOSING = 2;
  public static readonly CLOSED = 3;
  public readonly url: string;

  public constructor(url: string | URL) {
    this.url = String(url);
  }

  public close(): void {}
}

class Channel implements AgentCoreChannel {
  public invocations = 0;

  public async *invoke(): AsyncIterable<AgentCoreRuntimeEvent> {
    await Promise.resolve();
    this.invocations += 1;
    yield {
      requestId: REQUEST_ID,
      sequence: 0,
      operation: "request.accepted",
      payload: { status: 200 },
    };
    yield {
      requestId: REQUEST_ID,
      sequence: 1,
      operation: "request.completed",
      payload: { status: 200 },
    };
  }
}

function target(nativeFetch: typeof fetch): BootstrapTarget {
  return {
    fetch: nativeFetch,
    EventSource: NativeEventSource,
    WebSocket: NativeWebSocket,
  };
}

function remote(channel: AgentCoreChannel): KiroCrewRemoteTransport {
  return new KiroCrewRemoteTransport(channel, {
    origin: ORIGIN,
    bindingToken: () => "binding-token-that-is-long-enough-for-the-contract",
    requestId: () => REQUEST_ID,
  });
}

describe("KiroCrew bootstrap", () => {
  it("intercepts only upstream APIs and restores native browser boundaries", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("control")),
    );
    const browser = target(nativeFetch);
    const channel = new Channel();
    const restore = installKiroCrewBootstrap(browser, remote(channel));

    expect(browser.__KIROCREW_REMOTE_TRANSPORT__).toBe(true);
    expect(browser.__KIROCREW_AGENTCORE_CAPABILITIES__).toBe(
      KIROCREW_AGENTCORE_CAPABILITIES,
    );
    expect(
      await (await browser.fetch(`${ORIGIN}/control/v1/config`)).text(),
    ).toBe("control");
    expect(nativeFetch).toHaveBeenCalledTimes(1);
    expect((await browser.fetch(`${ORIGIN}/api/status`)).status).toBe(200);
    expect(channel.invocations).toBe(1);

    const source = new browser.EventSource(`${ORIGIN}/api/events`);
    expect(source).toBeInstanceOf(KiroCrewRemoteEventSource);
    source.close();
    const nativeSource = new browser.EventSource(
      "https://other.example/events",
    );
    expect(nativeSource).toBeInstanceOf(NativeEventSource);

    const socket = new browser.WebSocket("wss://shell.example/api/ws");
    expect(socket).toBeInstanceOf(KiroCrewRemoteWebSocket);
    const nativeSocket = new browser.WebSocket("wss://other.example/ws");
    expect(nativeSocket).toBeInstanceOf(NativeWebSocket);

    expect(() => installKiroCrewBootstrap(browser, remote(channel))).toThrow(
      "already installed",
    );
    restore();
    expect(browser.fetch).toBe(nativeFetch);
    expect(browser.EventSource).toBe(NativeEventSource);
    expect(browser.WebSocket).toBe(NativeWebSocket);
    expect(browser.__KIROCREW_REMOTE_TRANSPORT__).toBeUndefined();
  });

  it("sends a Crew API Owner route through the channel as its /api twin", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("index.html")),
    );
    const browser = { ...target(nativeFetch), location: { origin: ORIGIN } };
    const transport = remote(new Channel());
    const forwarded = vi.spyOn(transport, "fetch");
    const restore = installKiroCrewBootstrap(browser, transport);

    expect((await browser.fetch("/v1/crew/owner/bots?x=1")).status).toBe(200);
    expect(forwarded.mock.calls[0]?.[0]).toBe("/api/members?x=1");
    const request = new Request(`${ORIGIN}/v1/crew/owner/bots`, {
      headers: { "X-Session-Key": "s" },
    });
    await browser.fetch(request);
    const sent = forwarded.mock.calls[1]?.[0];
    expect(sent).toBeInstanceOf(Request);
    expect((sent as Request).url).toBe(`${ORIGIN}/api/members`);
    expect((sent as Request).headers.get("X-Session-Key")).toBe("s");
    // Other /v1 paths and other origins are not twins.
    await browser.fetch(`${ORIGIN}/v1/crew/owner/other`);
    await browser.fetch("https://other.example/v1/crew/owner/bots");
    await browser.fetch("http://[bad");
    expect(nativeFetch).toHaveBeenCalledTimes(3);
    restore();
  });

  it("publishes hidden and explanatory unavailable capability states", (): void => {
    expect(KIROCREW_AGENTCORE_CAPABILITIES.computerUse).toEqual({
      available: false,
      hidden: true,
      code: "FEATURE_UNAVAILABLE_IN_AGENTCORE",
      message: "This host-native feature is unavailable in AgentCore.",
    });
    expect(KIROCREW_AGENTCORE_CAPABILITIES.hostFilePicker.hidden).toBe(false);
    expect(
      unavailablePresentation(
        new RemoteTransportError(
          "FEATURE_UNAVAILABLE_IN_AGENTCORE",
          "Unavailable.",
          501,
        ),
      ),
    ).toMatchObject({
      available: false,
      code: "FEATURE_UNAVAILABLE_IN_AGENTCORE",
    });
    expect(unavailablePresentation(new Error("other"))).toBeUndefined();
  });
});

describe("pre-session gate", () => {
  it("synthesizes the prerequisite probe before any session exists", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("native")),
    );
    const browser = target(nativeFetch);
    installPreSessionGate(browser, ORIGIN);

    const response = await browser.fetch(`${ORIGIN}/api/kiro-prerequisite`);
    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body).toMatchObject({
      installed: true,
      authenticated: true,
      ready: true,
      operation: { status: "idle" },
    });
    expect(nativeFetch).not.toHaveBeenCalled();
  });

  it("synthesizes auth routes and rejects other upstream APIs with 503", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("native")),
    );
    const browser = target(nativeFetch);
    installPreSessionGate(browser, ORIGIN);

    const auth = await browser.fetch(`${ORIGIN}/api/auth/status`);
    expect(await auth.json()).toEqual({
      authenticated: true,
      mode: "agentcore",
      remoteSession: true,
      token: null,
    });
    const logout = await browser.fetch(`${ORIGIN}/api/auth/logout`, {
      method: "POST",
    });
    expect(await logout.json()).toEqual({
      disconnected: true,
      remoteSession: true,
    });

    const other = browser.fetch(`${ORIGIN}/api/chat`, {
      method: "POST",
      body: "{}",
    });
    let settled = false;
    void other.then(() => {
      settled = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    // Held, not refused: a refusal here is what left stale error banners.
    expect(settled).toBe(false);
    installKiroCrewBootstrap(browser, remote(new Channel()));
    expect((await other).status).toBe(200);
    expect(nativeFetch).not.toHaveBeenCalled();
  });

  it("routes a fetch reference taken before the session opened through the transport", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("index.html")),
    );
    const browser: BootstrapTarget = {
      ...target(nativeFetch),
      location: { origin: ORIGIN },
    };
    installPreSessionGate(browser, ORIGIN);
    // The SPA keeps its own reference to fetch from boot: that is the gate.
    const stale = browser.fetch;
    installKiroCrewBootstrap(browser, remote(new Channel()));

    expect((await stale(`${ORIGIN}/api/status`)).status).toBe(200);
    expect((await stale(`${ORIGIN}/v1/crew/owner/bots`)).status).toBe(200);
    expect(nativeFetch).not.toHaveBeenCalled();
    // Non-API traffic from the stale reference still goes to the network.
    await stale(`${ORIGIN}/assets/app.js`);
    expect(nativeFetch).toHaveBeenCalledTimes(1);
  });

  it("holds a Crew API Owner route as its /api twin until the session opens", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("index.html")),
    );
    const browser = target(nativeFetch);
    installPreSessionGate(browser, ORIGIN);
    const held = browser.fetch(`${ORIGIN}/v1/crew/owner/bots`);
    await new Promise((resolve) => setTimeout(resolve, 10));
    installKiroCrewBootstrap(browser, remote(new Channel()));
    expect((await held).status).toBe(200);
    expect(nativeFetch).not.toHaveBeenCalled();
  });

  it("rejects a held request when its signal aborts", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("native")),
    );
    const browser = target(nativeFetch);
    installPreSessionGate(browser, ORIGIN);
    const controller = new AbortController();
    const held = browser.fetch(`${ORIGIN}/api/status`, {
      signal: controller.signal,
    });
    controller.abort(new DOMException("stop", "AbortError"));
    await expect(held).rejects.toMatchObject({ name: "AbortError" });
    const already = browser.fetch(`${ORIGIN}/api/status`, {
      signal: controller.signal,
    });
    await expect(already).rejects.toMatchObject({ name: "AbortError" });
    expect(nativeFetch).not.toHaveBeenCalled();
  });

  it("passes through non-API and cross-origin requests", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("native")),
    );
    const browser = target(nativeFetch);
    installPreSessionGate(browser, ORIGIN);

    expect(
      await (await browser.fetch(`${ORIGIN}/control/v1/config`)).text(),
    ).toBe("native");
    expect(
      await (await browser.fetch("https://other.example/api/chat")).text(),
    ).toBe("native");
    expect(nativeFetch).toHaveBeenCalledTimes(2);
  });

  it("steps aside while the remote transport is installed and resumes after", async (): Promise<void> => {
    const nativeFetch = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("native")),
    );
    const browser = target(nativeFetch);
    installPreSessionGate(browser, ORIGIN);

    const restore = installKiroCrewBootstrap(browser, remote(new Channel()));
    expect((await browser.fetch(`${ORIGIN}/api/status`)).status).toBe(200);
    expect(nativeFetch).not.toHaveBeenCalled();

    restore();
    const gated = browser.fetch(`${ORIGIN}/api/status`);
    await new Promise((resolve) => setTimeout(resolve, 10));
    installKiroCrewBootstrap(browser, remote(new Channel()));
    expect((await gated).status).toBe(200);
    expect(nativeFetch).not.toHaveBeenCalled();
  });
});

class FakeSocket extends EventTarget {
  public static readonly CONNECTING = 0;
  public static readonly OPEN = 1;
  public static readonly CLOSING = 2;
  public static readonly CLOSED = 3;
  public static made: FakeSocket[] = [];
  public readyState = 0;
  public binaryType: BinaryType = "blob";
  public sent: unknown[] = [];
  public closedWith: [number | undefined, string | undefined] | undefined;
  public readonly url: string;

  public constructor(url: string | URL) {
    super();
    this.url = String(url);
    FakeSocket.made.push(this);
  }

  public send(data: unknown): void {
    this.sent.push(data);
  }

  public close(code?: number, reason?: string): void {
    this.closedWith = [code, reason];
  }
}

const tick = (): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, 0));

describe("pre-session socket gate", () => {
  it("holds a same-origin API socket until the session opens", async (): Promise<void> => {
    const native = vi.fn();
    class CountingNative extends NativeWebSocket {
      public constructor(url: string | URL) {
        super(url);
        native(String(url));
      }
    }
    const browser: BootstrapTarget = {
      ...target(vi.fn<typeof fetch>()),
      WebSocket: CountingNative,
    };
    installPreSessionGate(browser, ORIGIN);
    const held = new browser.WebSocket(
      "wss://shell.example/api/ws?caps=x",
    ) as unknown as HeldWebSocket;
    expect(held).toBeInstanceOf(HeldWebSocket);
    expect(held.readyState).toBe(HeldWebSocket.CONNECTING);
    expect(held.url).toBe("wss://shell.example/api/ws?caps=x");
    expect(() => {
      held.send("early");
    }).toThrow(/not open/);
    await tick();
    expect(native).not.toHaveBeenCalled();

    installKiroCrewBootstrap(browser, remote(new Channel()));
    await tick();
    // Connected through the transport's bridge, never the network.
    expect(native).not.toHaveBeenCalled();
    held.close();
  });

  it("re-dispatches the connected socket's events and forwards send and close", async (): Promise<void> => {
    const browser = target(vi.fn<typeof fetch>());
    installPreSessionGate(browser, ORIGIN);
    const held = new browser.WebSocket(
      `${ORIGIN}/api/ws`,
    ) as unknown as HeldWebSocket;
    held.binaryType = "arraybuffer";
    const seen: string[] = [];
    held.onopen = (): void => {
      seen.push("onopen");
    };
    held.onmessage = (event): void => {
      seen.push(`onmessage:${String(event.data)}`);
    };
    held.onerror = (): void => {
      seen.push("onerror");
    };
    held.onclose = (event): void => {
      seen.push(`onclose:${event.code}:${event.reason}:${event.wasClean}`);
    };
    held.addEventListener("message", () => {
      seen.push("listener");
    });

    FakeSocket.made = [];
    installKiroCrewBootstrap(browser, remote(new Channel()));
    // Stand in for the bridge the transport installed.
    browser.WebSocket = FakeSocket;
    await tick();
    const [socket] = FakeSocket.made;
    expect(socket?.url).toBe(`${ORIGIN}/api/ws`);
    expect(socket?.binaryType).toBe("arraybuffer");

    socket!.readyState = FakeSocket.OPEN;
    socket!.dispatchEvent(new Event("open"));
    expect(held.readyState).toBe(HeldWebSocket.OPEN);
    held.send("hello");
    expect(socket!.sent).toEqual(["hello"]);
    socket!.dispatchEvent(new MessageEvent("message", { data: "frame" }));
    socket!.dispatchEvent(new Event("error"));
    socket!.dispatchEvent(
      Object.assign(new Event("close"), {
        code: 4001,
        reason: "gone",
        wasClean: true,
      }),
    );
    held.close(1000, "bye");
    expect(socket!.closedWith).toEqual([1000, "bye"]);
    expect(seen).toEqual([
      "onopen",
      "listener",
      "onmessage:frame",
      "onerror",
      "onclose:4001:gone:true",
    ]);
  });

  it("closes a held socket without connecting it", async (): Promise<void> => {
    const browser = target(vi.fn<typeof fetch>());
    installPreSessionGate(browser, ORIGIN);
    const held = new browser.WebSocket(
      `${ORIGIN}/api/ws`,
    ) as unknown as HeldWebSocket;
    const codes: number[] = [];
    held.onclose = (event): void => {
      codes.push(event.code);
    };
    held.close(4000, "page left");
    held.close();
    expect(held.readyState).toBe(HeldWebSocket.CLOSED);
    FakeSocket.made = [];
    installKiroCrewBootstrap(browser, remote(new Channel()));
    browser.WebSocket = FakeSocket;
    await tick();
    expect(FakeSocket.made).toEqual([]);
    expect(codes).toEqual([4000]);
  });

  it("reports a socket that cannot be constructed as an error and a close", async (): Promise<void> => {
    const browser = target(vi.fn<typeof fetch>());
    installPreSessionGate(browser, ORIGIN);
    const held = new browser.WebSocket(
      `${ORIGIN}/api/ws`,
    ) as unknown as HeldWebSocket;
    const seen: string[] = [];
    held.onerror = (): void => {
      seen.push("error");
    };
    held.onclose = (event): void => {
      seen.push(`close:${event.code}`);
    };
    installKiroCrewBootstrap(browser, remote(new Channel()));
    browser.WebSocket = class {
      public static readonly CONNECTING = 0;
      public static readonly OPEN = 1;
      public static readonly CLOSING = 2;
      public static readonly CLOSED = 3;
      public constructor() {
        throw new Error("refused");
      }
    } as unknown as BootstrapTarget["WebSocket"];
    await tick();
    expect(seen).toEqual(["error", "close:1006"]);
    expect(held.readyState).toBe(HeldWebSocket.CLOSED);
  });

  it("passes cross-origin, non-API and unparsable sockets to the network", (): void => {
    const browser = target(vi.fn<typeof fetch>());
    installPreSessionGate(browser, ORIGIN);
    for (const url of [
      "wss://other.example/api/ws",
      `${ORIGIN}/assets/live`,
      "ws://shell.example/socket",
      "http://[",
    ]) {
      expect(new browser.WebSocket(url)).toBeInstanceOf(NativeWebSocket);
    }
    expect(browser.WebSocket.OPEN).toBe(1);
  });

  it("routes a constructor taken before the session through the transport", (): void => {
    const browser = target(vi.fn<typeof fetch>());
    const restoreGate = installPreSessionGate(browser, ORIGIN);
    const stale = browser.WebSocket;
    installKiroCrewBootstrap(browser, remote(new Channel()));

    const routed = new stale(`${ORIGIN}/api/ws`);
    expect(routed).toBeInstanceOf(KiroCrewRemoteWebSocket);
    routed.close();
    expect(new stale("wss://other.example/api/ws")).toBeInstanceOf(
      NativeWebSocket,
    );
    // The gate is what the bridge falls back to; it must not loop back.
    expect(new browser.WebSocket("wss://other.example/x")).toBeInstanceOf(
      NativeWebSocket,
    );
    restoreGate();
    expect(browser.WebSocket).toBe(NativeWebSocket);
  });
});

describe("wire config normalization", () => {
  const base = {
    region: "us-east-2",
    shellOrigin: ORIGIN,
    upstreamOrigin: ORIGIN,
    frontendCompatibilityVersion: "0.2.0",
  };

  it("adapts the Terraform-injected auth shape to the runtime shape", (): void => {
    const normalized = normalizeBrowserApplicationConfig({
      ...base,
      auth: {
        basePath: "/auth/v1",
        allowedDomains: ["amazon.com"],
      },
    });
    expect(normalized.auth).toEqual({
      basePath: "/auth/v1",
      allowedDomains: ["amazon.com"],
    });
  });

  it("defaults the auth base path when the wire config omits it", (): void => {
    expect(normalizeBrowserApplicationConfig({ ...base }).auth).toEqual({
      basePath: "/auth/v1",
    });
    expect(
      normalizeBrowserApplicationConfig({ ...base, auth: {} }).auth,
    ).toEqual({ basePath: "/auth/v1" });
  });
});
