import { describe, expect, it, vi } from "vitest";

import { installAppAssetBridge } from "../src/app-asset-bridge.js";
import type { ServiceWorkerMessageEventLike } from "../src/app-asset-bridge.js";
import {
  APP_ASSET_MESSAGE,
  appAssetPath,
  installAppAssetWorker,
  type WorkerScope,
} from "../src/app-asset-worker.js";
import {
  appRouteKind,
  classifyKiroCrewRoute,
  isTunnelledPath,
} from "../src/remote-transport.js";

const ORIGIN = "https://shell.example";

describe("installed app routes", () => {
  it("tunnels an app's api, ui and art paths and nothing else outside /api", (): void => {
    expect(appRouteKind("/apps/voice-call/api/tts")).toBe("api");
    expect(appRouteKind("/apps/voice-call/ui/dist/session-control.mjs")).toBe(
      "ui",
    );
    expect(appRouteKind("/apps/issue-radar/art/icon.png")).toBe("art");
    for (const path of [
      "/apps/voice-call",
      "/apps/voice-call/data/kv/x.json",
      "/apps/Voice_Call/ui/x.mjs",
      "/apps/voice-call/ui/%2e%2e/secret",
      "/apps/voice-call/ui/a%2Fb",
      "/apps/voice-call/ui/a%5Cb",
      "/apps/voice-call/ui/a\\b",
      "/assets/index.js",
    ]) {
      expect(appRouteKind(path), path).toBeUndefined();
    }
    expect(isTunnelledPath("/api/chat")).toBe(true);
    expect(isTunnelledPath("/apps/voice-call/api/tts")).toBe(true);
    expect(isTunnelledPath("/logo.png")).toBe(false);
  });

  it("allows any method on an app backend but only reads on its bundle", (): void => {
    expect(
      classifyKiroCrewRoute("POST", "/apps/voice-call/api/tts", ORIGIN),
    ).toBe("allowed");
    expect(
      classifyKiroCrewRoute(
        "GET",
        "/apps/voice-call/ui/dist/index.mjs",
        ORIGIN,
      ),
    ).toBe("allowed");
    expect(
      classifyKiroCrewRoute(
        "POST",
        "/apps/voice-call/ui/dist/index.mjs",
        ORIGIN,
      ),
    ).toBe("denied");
    expect(
      classifyKiroCrewRoute(
        "GET",
        "/apps/voice-call/ui/../../api/token",
        ORIGIN,
      ),
    ).toBe("denied");
    expect(
      classifyKiroCrewRoute(
        "GET",
        "https://evil.example/apps/x/ui/a.js",
        ORIGIN,
      ),
    ).toBe("denied");
  });
});

describe("app-asset service worker", () => {
  it("claims only same-origin GETs of an app's ui and art", (): void => {
    expect(
      appAssetPath(
        new Request(`${ORIGIN}/apps/voice-call/ui/dist/a.mjs?v=1`),
        ORIGIN,
      ),
    ).toBe("/apps/voice-call/ui/dist/a.mjs?v=1");
    expect(
      appAssetPath(new Request(`${ORIGIN}/apps/voice-call/art/i.png`), ORIGIN),
    ).toBe("/apps/voice-call/art/i.png");
    for (const request of [
      new Request(`${ORIGIN}/apps/voice-call/api/tts`),
      new Request(`${ORIGIN}/apps/voice-call/ui/a.mjs`, {
        method: "POST",
        body: "x",
      }),
      new Request(`https://evil.example/apps/voice-call/ui/a.mjs`),
      new Request(`${ORIGIN}/apps/voice-call/ui/%2e%2e/x`),
      new Request(`${ORIGIN}/assets/index.js`),
      new Request(`${ORIGIN}/api/chat`),
    ]) {
      expect(appAssetPath(request, ORIGIN), request.url).toBeUndefined();
    }
  });

  interface Harness {
    dispatch: (url: string, clientId?: string) => Promise<Response | undefined>;
    posted: string[];
    network: ReturnType<typeof vi.fn>;
  }
  interface FakeClient {
    postMessage: (message: { path: string }, transfer: unknown[]) => void;
  }

  function harness(answer: (path: string) => unknown, clients = 1): Harness {
    let listener: ((event: never) => void) | undefined;
    const posted: string[] = [];
    const network = vi.fn(() =>
      Promise.resolve(new Response("network", { status: 404 })),
    );
    const makeClient = (): FakeClient => ({
      postMessage: (message: { path: string }, transfer: unknown[]): void => {
        posted.push(message.path);
        const port = transfer[0] as { reply: (data: unknown) => void };
        queueMicrotask(() => port.reply(answer(message.path)));
      },
    });
    const all = Array.from({ length: clients }, makeClient);
    const scope: WorkerScope = {
      location: { origin: ORIGIN },
      clients: {
        get: (id: string) =>
          Promise.resolve(id === "page" ? all[0] : undefined),
        matchAll: () => Promise.resolve(all),
      },
      fetch: network,
      addEventListener: (_type, handler) => {
        listener = handler;
      },
    };
    const channel = (): {
      port1: { onmessage: ((event: { data: unknown }) => void) | null };
      port2: { reply: (data: unknown) => void };
    } => {
      const port1: { onmessage: ((event: { data: unknown }) => void) | null } =
        { onmessage: null };
      return {
        port1,
        port2: {
          reply: (data: unknown): void => port1.onmessage?.({ data }),
        },
      };
    };
    installAppAssetWorker(scope, channel, 50);
    const dispatch = async (
      url: string,
      clientId = "page",
    ): Promise<Response | undefined> => {
      let responded: Promise<Response> | undefined;
      listener?.({
        request: new Request(url),
        clientId,
        respondWith: (response: Promise<Response>): void => {
          responded = response;
        },
      } as never);
      return responded === undefined ? undefined : await responded;
    };
    return { dispatch, posted, network };
  }

  it("answers a module load with the bytes the page fetched through the sandbox", async (): Promise<void> => {
    const body = new TextEncoder().encode("export default 1").buffer;
    const { dispatch, posted, network } = harness(() => ({
      ok: true,
      status: 200,
      contentType: "text/javascript",
      body,
    }));
    const response = await dispatch(
      `${ORIGIN}/apps/voice-call/ui/dist/session-control.mjs`,
    );
    expect(posted).toEqual(["/apps/voice-call/ui/dist/session-control.mjs"]);
    expect(response?.status).toBe(200);
    expect(response?.headers.get("content-type")).toBe("text/javascript");
    expect(await response?.text()).toBe("export default 1");
    expect(network).not.toHaveBeenCalled();
  });

  it("falls back to the network when no page can answer", async (): Promise<void> => {
    const { dispatch, network } = harness(() => ({ ok: false }), 2);
    const response = await dispatch(`${ORIGIN}/apps/voice-call/ui/a.mjs`, "");
    expect(response?.status).toBe(404);
    expect(network).toHaveBeenCalledTimes(1);
  });

  it("leaves every other request to the upstream worker", async (): Promise<void> => {
    const { dispatch, posted } = harness(() => ({ ok: true }));
    expect(await dispatch(`${ORIGIN}/assets/index.js`)).toBeUndefined();
    expect(await dispatch(`${ORIGIN}/apps/voice-call/api/tts`)).toBeUndefined();
    expect(posted).toEqual([]);
  });
});

describe("app-asset page bridge", () => {
  interface Bridge {
    ask: (data: unknown) => Promise<unknown>;
    uninstall: () => void;
    container: { removeEventListener: ReturnType<typeof vi.fn> };
  }

  function bridge(fetcher: (url: string) => Promise<Response>): Bridge {
    let listener: ((event: ServiceWorkerMessageEventLike) => void) | undefined;
    const container = {
      addEventListener: (
        _t: "message",
        l: (event: ServiceWorkerMessageEventLike) => void,
      ): void => {
        listener = l;
      },
      removeEventListener: vi.fn(),
    };
    const uninstall = installAppAssetBridge(container, fetcher, ORIGIN);
    const ask = (data: unknown): Promise<unknown> =>
      new Promise((resolve) => {
        listener?.({
          data,
          ports: [
            {
              postMessage: (reply: unknown): void => {
                resolve(reply);
              },
            },
          ],
        });
      });
    return { ask, uninstall, container };
  }

  it("fetches an app's static path through the sandbox and returns the bytes", async (): Promise<void> => {
    const fetcher = vi.fn(() =>
      Promise.resolve(
        new Response("x", {
          status: 200,
          headers: { "Content-Type": "text/javascript" },
        }),
      ),
    );
    const { ask } = bridge(fetcher);
    const reply = (await ask({
      type: APP_ASSET_MESSAGE,
      path: "/apps/a/ui/x.mjs",
    })) as {
      ok: boolean;
      status: number;
      contentType: string;
      body: ArrayBuffer;
    };
    expect(fetcher).toHaveBeenCalledWith(`${ORIGIN}/apps/a/ui/x.mjs`);
    expect(reply.ok).toBe(true);
    expect(reply.status).toBe(200);
    expect(reply.contentType).toBe("text/javascript");
    expect(new TextDecoder().decode(reply.body)).toBe("x");
  });

  it("refuses anything that is not an app's ui or art", async (): Promise<void> => {
    const fetcher = vi.fn(() => Promise.resolve(new Response("secret")));
    const { ask, uninstall, container } = bridge(fetcher);
    for (const path of [
      "/api/secrets",
      "/apps/a/api/tts",
      "https://evil.example/apps/a/ui/x.mjs",
      "/apps/a/ui/%2e%2e/x",
      42,
    ]) {
      expect(await ask({ type: APP_ASSET_MESSAGE, path })).toEqual({
        ok: false,
      });
    }
    expect(fetcher).not.toHaveBeenCalled();
    uninstall();
    expect(container.removeEventListener).toHaveBeenCalled();
  });

  it("reports a failed sandbox fetch instead of hanging the worker", async (): Promise<void> => {
    const { ask } = bridge(() => Promise.reject(new Error("offline")));
    expect(
      await ask({ type: APP_ASSET_MESSAGE, path: "/apps/a/art/i.png" }),
    ).toEqual({ ok: false });
  });

  it("does nothing without a service worker", (): void => {
    expect(() =>
      installAppAssetBridge(undefined, vi.fn(), ORIGIN)(),
    ).not.toThrow();
  });
});
