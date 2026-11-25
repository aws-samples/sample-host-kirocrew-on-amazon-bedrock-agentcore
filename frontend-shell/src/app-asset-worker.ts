/**
 * Service-worker half of the app-asset bridge.
 *
 * An installed app's dashboard UI is an ES module the upstream SPA loads with a
 * native `import("/apps/{name}/ui/...")`, and its artwork is a plain `<img>`.
 * Locally the gateway serves both on its own origin. Here the page origin is
 * CloudFront, which cannot reach the sandbox, and a native module load never
 * passes through the shell's patched `fetch`. So this handler, imported into the
 * upstream `/sw.js`, catches those GETs and asks the page that issued them to
 * fetch the file through its remote transport, which carries the Cognito
 * subject binding like every `/api/*` call. When no page can answer (the
 * sandbox is not connected yet) the request goes to the network unchanged,
 * which is exactly what happened before this bridge existed.
 *
 * It answers nothing else: `/apps/{name}/api/*` already travels through the
 * patched `fetch`, and every other path is left to the upstream worker.
 */

export const APP_ASSET_MESSAGE = "kirocrew-app-asset";
const APP_ASSET_ROUTE = /^\/apps\/[a-z0-9]+(?:-[a-z0-9]+)*\/(?:ui|art)\//;
const ENCODED_SEPARATOR = /%(2e|2f|5c)/i;
const PAGE_TIMEOUT_MS = 30_000;

export interface AppAssetReply {
  readonly ok: boolean;
  readonly status?: number;
  readonly contentType?: string;
  readonly body?: ArrayBuffer;
}

interface PortLike {
  onmessage: ((event: { data: unknown }) => void) | null;
}

interface ClientLike {
  postMessage(message: unknown, transfer: unknown[]): void;
}

interface ChannelLike {
  readonly port1: PortLike;
  readonly port2: unknown;
}

interface FetchEventLike {
  readonly request: Request;
  readonly clientId: string;
  respondWith(response: Promise<Response>): void;
}

export interface WorkerScope {
  readonly location: { readonly origin: string };
  readonly clients: {
    get(id: string): Promise<ClientLike | undefined>;
    matchAll(options: { type: "window" }): Promise<readonly ClientLike[]>;
  };
  fetch(request: Request): Promise<Response>;
  addEventListener(
    type: "fetch",
    listener: (event: FetchEventLike) => void,
  ): void;
}

/** The same-origin path this bridge answers, or undefined to leave it alone. */
export function appAssetPath(
  request: Request,
  origin: string,
): string | undefined {
  if (request.method !== "GET") {
    return undefined;
  }
  const url = new URL(request.url);
  if (
    url.origin !== origin ||
    !APP_ASSET_ROUTE.test(url.pathname) ||
    ENCODED_SEPARATOR.test(url.pathname) ||
    url.pathname.includes("\\") ||
    url.pathname.includes("//") ||
    url.pathname.split("/").some((part) => part === "." || part === "..")
  ) {
    return undefined;
  }
  return `${url.pathname}${url.search}`;
}

function askPage(
  client: ClientLike,
  path: string,
  makeChannel: () => ChannelLike,
  timeoutMs: number,
): Promise<Response | undefined> {
  return new Promise((resolve) => {
    const channel = makeChannel();
    const timer = setTimeout(() => resolve(undefined), timeoutMs);
    channel.port1.onmessage = (event): void => {
      clearTimeout(timer);
      const reply = event.data as AppAssetReply | null;
      if (
        reply === null ||
        typeof reply !== "object" ||
        reply.ok !== true ||
        typeof reply.status !== "number" ||
        !(reply.body instanceof ArrayBuffer)
      ) {
        resolve(undefined);
        return;
      }
      resolve(
        new Response(reply.body, {
          status: reply.status,
          headers: {
            "Content-Type": reply.contentType ?? "application/octet-stream",
            "Cache-Control": "no-store",
          },
        }),
      );
    };
    try {
      client.postMessage({ type: APP_ASSET_MESSAGE, path }, [channel.port2]);
    } catch {
      clearTimeout(timer);
      resolve(undefined);
    }
  });
}

export function installAppAssetWorker(
  scope: WorkerScope,
  makeChannel: () => ChannelLike = () =>
    new MessageChannel() as unknown as ChannelLike,
  timeoutMs = PAGE_TIMEOUT_MS,
): void {
  scope.addEventListener("fetch", (event) => {
    const path = appAssetPath(event.request, scope.location.origin);
    if (path === undefined) {
      return;
    }
    event.respondWith(
      (async (): Promise<Response> => {
        const own =
          event.clientId === ""
            ? undefined
            : await scope.clients.get(event.clientId);
        const candidates =
          own === undefined
            ? await scope.clients.matchAll({ type: "window" })
            : [own];
        for (const client of candidates) {
          const response = await askPage(client, path, makeChannel, timeoutMs);
          if (response !== undefined) {
            return response;
          }
        }
        return await scope.fetch(event.request);
      })(),
    );
  });
}
