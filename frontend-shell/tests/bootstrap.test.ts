import { describe, expect, it, vi } from "vitest";

import {
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

    const other = await browser.fetch(`${ORIGIN}/api/chat`, {
      method: "POST",
      body: "{}",
    });
    expect(other.status).toBe(503);
    expect(await other.json()).toMatchObject({
      code: "KIROCREW_UNAVAILABLE",
      retryable: true,
    });
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
    const gated = await browser.fetch(`${ORIGIN}/api/status`);
    expect(gated.status).toBe(503);
    expect(nativeFetch).not.toHaveBeenCalled();
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
